package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"lorentzgo/backtest"
	"lorentzgo/indicator"
	"lorentzgo/strategy"
	"math"
	"os"
	"path/filepath"
	"sort"
)

type walkCandidate struct {
	Name   string          `json:"name"`
	Config json.RawMessage `json:"config"`
}
type walkOuter struct {
	Name  string         `json:"name"`
	Start string         `json:"start"`
	End   string         `json:"end"`
	Inner []OptimizeFold `json:"inner"`
}
type walkSelection struct {
	MinTrades                 int     `json:"min_trades"`
	MinPositiveWindows        int     `json:"min_positive_windows"`
	MinNetReturnPct           float64 `json:"min_net_return_pct"`
	MinWinRatePct             float64 `json:"min_win_rate_pct"`
	MaxDrawdownPct            float64 `json:"max_drawdown_pct"`
	RequireNetImprovement     bool    `json:"require_net_improvement"`
	RequireWinRateImprovement bool    `json:"require_win_rate_improvement"`
	MaxDrawdownIncreasePct    float64 `json:"max_drawdown_increase_pct"`
}
type walkPlan struct {
	SchemaVersion   int             `json:"schema_version"`
	DatasetDir      string          `json:"dataset_dir"`
	Interval        string          `json:"interval"`
	HistoryStart    string          `json:"history_start"`
	FeeBPS          float64         `json:"fee_bps"`
	SlippageBPS     float64         `json:"slippage_bps"`
	Baseline        json.RawMessage `json:"baseline"`
	Candidates      []walkCandidate `json:"candidates"`
	Outer           []walkOuter     `json:"outer"`
	Selection       walkSelection   `json:"selection"`
	CostMultipliers []float64       `json:"cost_multipliers"`
	Budget          int             `json:"budget"`
	DataUse         string          `json:"data_use"`
	ExposureFile    string          `json:"exposure_file"`
}
type exposureRange struct {
	Start    string `json:"start"`
	End      string `json:"end"`
	Use      string `json:"use"`
	Evidence string `json:"evidence"`
}
type exposureManifest struct {
	SchemaVersion            int             `json:"schema_version"`
	Symbol                   string          `json:"symbol"`
	KnownCacheEnd            string          `json:"known_cache_end"`
	Ranges                   []exposureRange `json:"ranges"`
	UnusedFinalDataAvailable bool            `json:"unused_final_data_available"`
}
type walkDataset struct {
	plan             walkPlan
	ds               *enhancedDataset
	baseline         strategy.Config
	candidates       []strategy.Config
	outer            []optimizeWindow
	inner            [][]optimizeWindow
	required         int
	exposureIdentity string
}

func walkNormalize(p walkPlan) (walkPlan, OptimizePlan, int, error) {
	fail := func(e error) (walkPlan, OptimizePlan, int, error) { return p, OptimizePlan{}, 0, e }
	if p.SchemaVersion != 1 || p.Interval != "1h" && p.Interval != "15m" || len(p.Candidates) < 1 || len(p.Candidates) > 32 || len(p.Outer) < 1 || len(p.Outer) > 24 {
		return fail(errors.New("invalid walkforward schema/interval/bounded candidates/windows"))
	}
	if p.DataUse != "retrospective" && p.DataUse != "unused-final" {
		return fail(errors.New("explicit retrospective or unused-final data_use required"))
	}
	if p.ExposureFile == "" {
		return fail(errors.New("exposure_file required"))
	}
	s := p.Selection
	for _, v := range []float64{s.MinNetReturnPct, s.MinWinRatePct, s.MaxDrawdownPct, s.MaxDrawdownIncreasePct, p.FeeBPS, p.SlippageBPS} {
		if math.IsNaN(v) || math.IsInf(v, 0) {
			return fail(errors.New("nonfinite selection/cost"))
		}
	}
	if s.MinTrades < 30 || s.MinPositiveWindows < 1 || s.MinPositiveWindows > len(p.Outer) || s.MinNetReturnPct < 0 || s.MinWinRatePct < 0 || s.MinWinRatePct > 100 || s.MaxDrawdownPct <= 0 || s.MaxDrawdownPct >= 100 || s.MaxDrawdownIncreasePct < 0 || s.MaxDrawdownIncreasePct > 100 || !s.RequireNetImprovement && !s.RequireWinRateImprovement {
		return fail(errors.New("invalid predeclared selection criteria"))
	}
	// Cost pressure is deliberately fixed and kept out of candidate selection.
	if len(p.CostMultipliers) != 3 || p.CostMultipliers[0] != 1 || p.CostMultipliers[1] != 1.5 || p.CostMultipliers[2] != 2 || p.FeeBPS != 5 || p.SlippageBPS != 2 {
		return fail(errors.New("cost scenarios must be 5/2 bps per side at 1,1.5,2 multiples"))
	}
	step, _ := marketStep(p.Interval)
	earliest := int64(math.MaxInt64)
	previous := int64(-1)
	names := map[string]bool{"baseline": true}
	for _, c := range p.Candidates {
		if c.Name == "" || names[c.Name] {
			return fail(errors.New("candidate names must be unique and not baseline"))
		}
		names[c.Name] = true
	}
	folds := []OptimizeFold{}
	required := 2*len(p.CostMultipliers) + 2*len(p.Outer)
	for i, o := range p.Outer {
		start, e := optimizeDate(o.Start)
		if e != nil {
			return fail(e)
		}
		end, e := optimizeDate(o.End)
		if e != nil {
			return fail(e)
		}
		if o.Name == "" || start >= end || start%step != 0 || end%step != 0 || i > 0 && start != previous || len(o.Inner) < 2 || len(o.Inner) > 24 || s.MinPositiveWindows > len(o.Inner) {
			return fail(errors.New("outer windows need contiguous aligned dates and at least two inner folds"))
		}
		for j := 0; j < i; j++ {
			if p.Outer[j].Name == o.Name {
				return fail(errors.New("duplicate outer name"))
			}
		}
		prior := int64(-1)
		innerNames := map[string]bool{}
		for j, f := range o.Inner {
			a, e := optimizeDate(f.Start)
			if e != nil {
				return fail(e)
			}
			b, e := optimizeDate(f.End)
			if e != nil {
				return fail(e)
			}
			if f.Name == "" || innerNames[f.Name] || a >= b || b > start || a%step != 0 || b%step != 0 || j > 0 && a != prior {
				return fail(errors.New("inner folds must be ordered contiguous, end before outer start, and aligned"))
			}
			innerNames[f.Name] = true
			prior = b
			earliest = min(earliest, a)
			p.Outer[i].Inner[j].Start, p.Outer[i].Inner[j].End = iso(a), iso(b)
		}
		p.Outer[i].Start, p.Outer[i].End = iso(start), iso(end)
		folds = append(folds, OptimizeFold{o.Name, iso(start), iso(end)})
		previous = end
		required += (len(p.Candidates) + 1) * len(o.Inner)
	}
	if required > 10000 || p.Budget != required {
		return fail(fmt.Errorf("budget must equal %d ledger configuration/window evaluations, including controls, resets and costs", required))
	}
	first, _ := optimizeDate(p.Outer[0].Start)
	folds = append([]OptimizeFold{{"selection-history", iso(earliest), iso(first)}}, folds...)
	base := OptimizePlan{1, p.DatasetDir, p.Interval, p.HistoryStart, folds, p.FeeBPS, p.SlippageBPS, s.MinTrades, 1}
	normalized, _, _, e := optimizeNormalizePlan(base)
	if e != nil {
		return fail(e)
	}
	p.DatasetDir, p.HistoryStart = normalized.DatasetDir, normalized.HistoryStart
	p.ExposureFile, e = filepath.Abs(p.ExposureFile)
	if e != nil {
		return fail(e)
	}
	// Canonical complete configurations enter the contract, never implicit upgrades.
	cfg, e := strategy.DecodeConfig(p.Baseline)
	if e != nil {
		return fail(fmt.Errorf("baseline: %w", e))
	}
	p.Baseline, _ = json.Marshal(cfg)
	for i, c := range p.Candidates {
		v, e := strategy.DecodeConfig(c.Config)
		if e != nil {
			return fail(fmt.Errorf("%s: %w", c.Name, e))
		}
		if v.Risk != cfg.Risk || v.Exit != cfg.Exit {
			return fail(errors.New("continuous candidate switches require one frozen risk and exit contract"))
		}
		p.Candidates[i].Config, _ = json.Marshal(v)
	}
	return p, normalized, required, nil
}
func marketStep(interval string) (int64, error) {
	if interval == "1h" {
		return 3600000, nil
	}
	if interval == "15m" {
		return 900000, nil
	}
	return 0, errors.New("unsupported interval")
}
func walkExposure(raw []byte, p walkPlan) error {
	var m exposureManifest
	if e := optimizeDecode(raw, &m); e != nil {
		return e
	}
	if m.SchemaVersion != 1 || m.Symbol != "ETHUSDT" || len(m.Ranges) == 0 {
		return errors.New("invalid exposure manifest")
	}
	known, e := optimizeDate(m.KnownCacheEnd)
	if e != nil {
		return e
	}
	intervals := make([]optimizeWindow, len(m.Ranges))
	last := int64(-1)
	for i, r := range m.Ranges {
		a, e := optimizeDate(r.Start)
		if e != nil {
			return e
		}
		b, e := optimizeDate(r.End)
		if e != nil {
			return e
		}
		if a >= b || i > 0 && a != last || r.Evidence == "" {
			return errors.New("exposure ranges need contiguous dates and evidence")
		}
		if r.Use != "exposed_history" && r.Use != "development_selection" && r.Use != "exposed_retrospective" && r.Use != "unused_final" {
			return errors.New("unknown exposure use")
		}
		intervals[i] = optimizeWindow{a, b}
		last = b
	}
	if last != known {
		return errors.New("exposure range end differs from known_cache_end")
	}
	if p.DataUse == "unused-final" && !m.UnusedFinalDataAvailable {
		return errors.New("unused final data unavailable; cannot relabel exposed periods")
	}
	hs, _ := optimizeDate(p.HistoryStart)
	end, _ := optimizeDate(p.Outer[len(p.Outer)-1].End)
	if hs < intervals[0].start || end > known {
		return errors.New("exposure manifest does not cover evaluated history")
	}
	if p.DataUse == "unused-final" {
		// Inventory established before this upgrade: all cached history through this
		// exclusive boundary is exposed. A replacement manifest cannot erase it.
		exposedEnd, _ := optimizeDate("2026-10-01")
		for _, o := range p.Outer {
			a, _ := optimizeDate(o.Start)
			if a < exposedEnd {
				return errors.New("known exposed history before 2026-10-01 cannot be independent final data")
			}
		}
		for _, o := range p.Outer {
			a, _ := optimizeDate(o.Start)
			b, _ := optimizeDate(o.End)
			for i, w := range intervals {
				if a < w.end && b > w.start && m.Ranges[i].Use != "unused_final" {
					return errors.New("outer window overlaps exposed data")
				}
			}
		}
	}
	return nil
}
func walkPrepare(p walkPlan, ds *enhancedDataset, required int, exposureIdentity string) (*walkDataset, error) {
	w := &walkDataset{plan: p, ds: ds, required: required, exposureIdentity: exposureIdentity}
	var e error
	w.baseline, e = strategy.DecodeConfig(p.Baseline)
	if e != nil {
		return nil, e
	}
	for _, c := range p.Candidates {
		v, e := strategy.DecodeConfig(c.Config)
		if e != nil {
			return nil, e
		}
		w.candidates = append(w.candidates, v)
	}
	for _, c := range append([]strategy.Config{w.baseline}, w.candidates...) {
		if e := ds.validate(c); e != nil {
			return nil, e
		}
	}
	for _, o := range p.Outer {
		a, _ := optimizeDate(o.Start)
		b, _ := optimizeDate(o.End)
		w.outer = append(w.outer, optimizeWindow{a, b})
		inner := []optimizeWindow{}
		for _, f := range o.Inner {
			a, _ := optimizeDate(f.Start)
			b, _ := optimizeDate(f.End)
			inner = append(inner, optimizeWindow{a, b})
		}
		w.inner = append(w.inner, inner)
	}
	return w, nil
}
func walkLoad(p walkPlan) (*walkDataset, error) {
	p, base, n, e := walkNormalize(p)
	if e != nil {
		return nil, e
	}
	raw, e := os.ReadFile(p.ExposureFile)
	if e != nil {
		return nil, e
	}
	if e := walkExposure(raw, p); e != nil {
		return nil, e
	}
	h := sha256.Sum256(raw)
	ds, e := enhancedLoad(base)
	if e != nil {
		return nil, e
	}
	return walkPrepare(p, ds, n, hex.EncodeToString(h[:]))
}
func (w *walkDataset) ready() map[string]any {
	return map[string]any{"type": "ready", "protocol_version": 1, "plan": w.plan, "required_evaluations": w.required, "data_identity": w.ds.base.identity, "daily_identity": w.ds.dailyIdentity, "exposure_identity": w.exposureIdentity}
}
func walkReasons(s walkSelection, net, win, dd float64, trades, positive int, baselineNet, baselineWin, baselineDD float64) []string {
	reasons := []string{}
	if trades < s.MinTrades {
		reasons = append(reasons, "insufficient_trades")
	}
	if positive < s.MinPositiveWindows {
		reasons = append(reasons, "insufficient_positive_windows")
	}
	if net <= s.MinNetReturnPct {
		reasons = append(reasons, "net_return_below_requirement")
	}
	if win < s.MinWinRatePct {
		reasons = append(reasons, "win_rate_below_requirement")
	}
	if dd > s.MaxDrawdownPct {
		reasons = append(reasons, "absolute_drawdown_exceeded")
	}
	if s.RequireNetImprovement && net <= baselineNet {
		reasons = append(reasons, "no_net_improvement")
	}
	if s.RequireWinRateImprovement && win <= baselineWin {
		reasons = append(reasons, "no_win_rate_improvement")
	}
	if dd > baselineDD+s.MaxDrawdownIncreasePct {
		reasons = append(reasons, "drawdown_tradeoff_exceeded")
	}
	return reasons
}
func (w *walkDataset) innerResult(i int, c strategy.Config) ([]optimizeFoldResult, optimizeAggregate, error) {
	clone := *w.ds
	base := *w.ds.base
	stop := sort.Search(len(base.rows), func(j int) bool { return base.rows[j].OpenTime >= w.inner[i][len(w.inner[i])-1].end })
	base.rows = base.rows[:stop]
	base.windows = w.inner[i]
	base.plan.Folds = w.plan.Outer[i].Inner
	clone.base = &base
	r, e := clone.evaluate(c)
	if e != nil {
		return nil, optimizeAggregate{}, e
	}
	return r["folds"].([]optimizeFoldResult), r["aggregate"].(optimizeAggregate), nil
}
func (w *walkDataset) runTo(end int64, c strategy.Config) (strategy.Result, error) {
	stop := sort.Search(len(w.ds.base.rows), func(i int) bool { return w.ds.base.rows[i].OpenTime >= end })
	start, _ := optimizeDate(c.Daily.HistoryStart)
	begin := sort.Search(len(w.ds.daily), func(i int) bool { return w.ds.daily[i].OpenTime >= start })
	dailyEnd := sort.Search(len(w.ds.daily), func(i int) bool { return w.ds.daily[i].OpenTime >= end })
	return strategy.Run(w.ds.base.rows[:stop], w.ds.daily[begin:dailyEnd], c)
}
func (w *walkDataset) ledger(r strategy.Result, c strategy.Config, start, end int64, multiplier float64, entries []backtest.EntryInfo) (backtest.Report, error) {
	b := c.LedgerConfig()
	b.StartTime, b.EndTime = start, end
	b.FeeBPS = w.plan.FeeBPS * multiplier
	b.SlippageBPS = w.plan.SlippageBPS * multiplier
	if entries == nil {
		entries = strategy.EntryMetadata(r, c)
	}
	return backtest.EvaluateWithRisk(w.ds.base.rows[:len(r.Points)], r.Points, w.ds.base.funding, b, entries, c.Risk, r.RiskATR)
}
func walkAppend(dst *strategy.Result, src strategy.Result, rowsStart int64, rows []int64) {
	for i, at := range rows {
		if at >= rowsStart && i < len(src.Points) {
			dst.Points[i] = src.Points[i]
		}
	}
	// Event decision at end boundary belongs to the previous window's last bar.
	for _, e := range src.Events {
		if e.DecisionTime > rowsStart {
			dst.Events = append(dst.Events, e)
		}
	}
}
func (w *walkDataset) evaluate() (map[string]any, error) {
	n := len(w.ds.base.rows)
	chosen := strategy.Result{Points: make([]indicator.Point, n), Events: []strategy.Event{}}
	control := strategy.Result{Points: make([]indicator.Point, n), Events: []strategy.Event{}}
	chosenEntries, controlEntries := make([]backtest.EntryInfo, n), make([]backtest.EntryInfo, n)
	times := make([]int64, n)
	for i, r := range w.ds.base.rows {
		times[i] = r.OpenTime
	}
	selections := []map[string]any{}
	resetChosen, resetBaseline := []optimizeFoldResult{}, []optimizeFoldResult{}
	used := 0
	for i, o := range w.plan.Outer {
		baseF, baseA, e := w.innerResult(i, w.baseline)
		used += len(w.inner[i])
		if e != nil {
			return nil, e
		}
		best := w.baseline
		name := "baseline"
		bestNet := math.Inf(-1)
		comparisons := []map[string]any{{"name": "baseline", "folds": baseF, "aggregate": baseA}}
		for j, c := range w.candidates {
			folds, a, e := w.innerResult(i, c)
			used += len(w.inner[i])
			if e != nil {
				return nil, e
			}
			reasons := walkReasons(w.plan.Selection, a.NetReturnPct, a.WinRatePct, a.MaxFoldDrawdownPct, a.Trades, a.PositiveFolds, baseA.NetReturnPct, baseA.WinRatePct, baseA.MaxFoldDrawdownPct)
			comparisons = append(comparisons, map[string]any{"name": w.plan.Candidates[j].Name, "config": c, "folds": folds, "aggregate": a, "rejection_reasons": reasons})
			if len(reasons) == 0 && a.NetReturnPct > bestNet {
				best, name, bestNet = c, w.plan.Candidates[j].Name, a.NetReturnPct
			}
		}
		// Candidate choice is final before any candle of this outer window is used.
		selections = append(selections, map[string]any{"outer": o, "selected": name, "config": best, "inner_results": comparisons})
		cr, e := w.runTo(w.outer[i].end, best)
		if e != nil {
			return nil, e
		}
		br, e := w.runTo(w.outer[i].end, w.baseline)
		if e != nil {
			return nil, e
		}
		ci, bi := strategy.EntryMetadata(cr, best), strategy.EntryMetadata(br, w.baseline)
		for j, at := range times {
			if at >= w.outer[i].start && j < len(cr.Points) {
				chosenEntries[j], controlEntries[j] = ci[j], bi[j]
			}
		}
		walkAppend(&chosen, cr, w.outer[i].start, times)
		walkAppend(&control, br, w.outer[i].start, times)
		chosen.RiskATR, control.RiskATR = cr.RiskATR, br.RiskATR
		rr, e := w.ledger(cr, best, w.outer[i].start, w.outer[i].end, 1, nil)
		used++
		if e != nil {
			return nil, e
		}
		resetChosen = append(resetChosen, optimizeFoldResult{o.Name, o.Start, o.End, rr.Metrics})
		rr, e = w.ledger(br, w.baseline, w.outer[i].start, w.outer[i].end, 1, nil)
		used++
		if e != nil {
			return nil, e
		}
		resetBaseline = append(resetBaseline, optimizeFoldResult{o.Name, o.Start, o.End, rr.Metrics})
	}
	start, end := w.outer[0].start, w.outer[len(w.outer)-1].end
	scenarios := []map[string]any{}
	allReasons := []string{}
	for _, multiplier := range w.plan.CostMultipliers {
		sr, e := w.ledger(chosen, w.baseline, start, end, multiplier, chosenEntries)
		used++
		if e != nil {
			return nil, e
		}
		br, e := w.ledger(control, w.baseline, start, end, multiplier, controlEntries)
		used++
		if e != nil {
			return nil, e
		}
		selectedSummary, positive, e := w.summarize(sr, chosen)
		if e != nil {
			return nil, e
		}
		baselineSummary, _, e := w.summarize(br, control)
		if e != nil {
			return nil, e
		}
		reasons := walkReasons(w.plan.Selection, sr.Metrics.NetReturnPct, sr.Metrics.WinRatePct, sr.Metrics.MaxDrawdownPct, sr.Metrics.Trades, positive, br.Metrics.NetReturnPct, br.Metrics.WinRatePct, br.Metrics.MaxDrawdownPct)
		scenarios = append(scenarios, map[string]any{"cost_multiplier": multiplier, "selected": selectedSummary, "baseline": baselineSummary, "adoption_rejection_reasons": reasons})
		for _, r := range reasons {
			allReasons = append(allReasons, fmt.Sprintf("cost_%g:%s", multiplier, r))
		}
	}
	status := "meets_adoption_criteria"
	if len(allReasons) > 0 {
		status = "keep_disabled"
	} else if w.plan.DataUse != "unused-final" {
		status = "insufficient_evidence"
	}
	if w.plan.DataUse != "unused-final" {
		allReasons = append(allReasons, "exposed_retrospective_data; independent final validation pending")
	}
	if used != w.required {
		return nil, fmt.Errorf("evaluation accounting mismatch %d/%d", used, w.required)
	}
	return map[string]any{"type": "result", "protocol_version": 1, "plan": w.plan, "data_identity": w.ds.base.identity, "daily_identity": w.ds.dailyIdentity, "exposure_identity": w.exposureIdentity, "evaluations_used": used, "selections": selections, "continuous_scenarios": scenarios, "legacy_fold_reset": map[string]any{"selected": resetChosen, "baseline": resetBaseline, "selected_aggregate": optimizeSummarize(resetChosen), "baseline_aggregate": optimizeSummarize(resetBaseline)}, "decision": map[string]any{"status": status, "reasons": allReasons, "data_use": w.plan.DataUse}, "assumptions": []string{"Budget counts each inner configuration/fold ledger call, both flat outer controls, and both continuous accounts for every cost scenario. Failed batches reserve their full budget; no automatic retry.", "Selection uses only inner windows ending before each outer start. Tied eligible returns retain candidate declaration order; if none qualifies the frozen baseline is used.", "Parameter switches use signals from the selected causal prefix at the first close inside each outer window. The previous window's final decision still executes at the boundary open. Classifier state is recomputed from the fixed history start, while position, funding, risk line and cash remain continuous.", "Only the final end flattens the continuous account. Legacy fold-reset results remain separately labeled. Window drawdown uses bar-close equity; full-account drawdown also samples execution boundaries.", "Forward online labels are admitted only after four-bar maturity. Retrospective nesting cannot remove previous data exposure or establish independent final performance."}}, nil
}
func (w *walkDataset) summarize(r backtest.Report, events strategy.Result) (map[string]any, int, error) {
	windows := []backtest.WindowMetrics{}
	positive := 0
	for _, o := range w.outer {
		m, e := backtest.SummarizeWindow(r, o.start, o.end)
		if e != nil {
			return nil, 0, e
		}
		windows = append(windows, m)
		if m.NetReturnPct > 0 {
			positive++
		}
	}
	entries, exits := map[string]int{}, map[string]int{}
	for _, t := range r.Trades {
		entries[t.EntryKind]++
		exits[t.Reason]++
	}
	d, e := backtest.SummarizeDiagnostics(r)
	if e != nil {
		return nil, 0, e
	}
	return map[string]any{"metrics": r.Metrics, "windows": windows, "diagnostics": d, "signal_events": strategy.Counts(events.Events), "event_reasons": strategy.ReasonCounts(events.Events), "completed_entries": entries, "fills": map[string]any{"entry": len(r.Trades), "exit": len(r.Trades), "total": 2 * len(r.Trades), "basis": "single full entry and full exit per completed trade; final liquidation included; no partial fills"}, "exit_reasons": exits, "assumptions": r.Assumptions}, positive, nil
}
func walkforwardEval(args []string) error {
	f := flag.NewFlagSet("walkforward-eval", flag.ContinueOnError)
	path := f.String("plan", "", "complete nested time/budget/selection plan")
	inspect := f.Bool("inspect", false, "validate contract/data without a strategy evaluation")
	if e := f.Parse(args); e != nil {
		return e
	}
	if *path == "" || f.NArg() != 0 {
		return errors.New("walkforward-eval requires -plan and no positional args")
	}
	raw, e := os.ReadFile(*path)
	if e != nil {
		return e
	}
	var p walkPlan
	if e := optimizeDecode(raw, &p); e != nil {
		return e
	}
	w, e := walkLoad(p)
	if e != nil {
		return e
	}
	enc := json.NewEncoder(os.Stdout)
	if *inspect {
		return enc.Encode(w.ready())
	}
	r, e := w.evaluate()
	if e != nil {
		return e
	}
	return enc.Encode(r)
}

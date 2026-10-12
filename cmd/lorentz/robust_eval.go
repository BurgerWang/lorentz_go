package main

import (
	"bufio"
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"lorentzgo/backtest"
	"lorentzgo/market"
	"lorentzgo/strategy"
	"math"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"time"
)

const robustVersion = "robust-eval-v1"

type RobustPlan struct {
	Version          string         `json:"version"`
	BundleDir        string         `json:"bundle_dir"`
	Interval         string         `json:"interval"`
	HistoryStart     string         `json:"history_start"`
	Windows          []OptimizeFold `json:"windows"`
	AccountMode      string         `json:"account_mode"`
	FundingMode      string         `json:"funding_mode"`
	FundingScenarios []string       `json:"funding_scenarios"`
	CostMultipliers  []float64      `json:"cost_multipliers"`
}
type robustInsufficientError struct{ Message string }

func (e robustInsufficientError) Error() string {
	return "configuration initialization insufficient: " + e.Message
}

type robustFile struct {
	Path   string `json:"path"`
	SHA256 string `json:"sha256"`
}
type robustSource struct {
	Path     string `json:"path"`
	SHA256   string `json:"sha256"`
	Snapshot string `json:"snapshot,omitempty"`
}
type robustBundle struct {
	Version           string                  `json:"version"`
	SchemaVersion     int                     `json:"schema_version"`
	Symbol            string                  `json:"symbol"`
	Source            string                  `json:"source"`
	Interval          string                  `json:"interval"`
	HistoryStart      string                  `json:"history_start"`
	End               string                  `json:"history_end"`
	DailyHistoryStart string                  `json:"daily_history_start"`
	FundingMode       string                  `json:"funding_mode"`
	Files             map[string]robustFile   `json:"files"`
	Sources           map[string]robustSource `json:"sources"`
	Synthetic         bool                    `json:"synthetic"`
}
type robustStress struct {
	Time        int64   `json:"time"`
	Lower       float64 `json:"lower_mark"`
	Upper       float64 `json:"upper_mark"`
	Available   int64   `json:"available_time"`
	BucketOpen  int64   `json:"source_bucket_open"`
	BucketClose int64   `json:"source_bucket_close"`
}
type robustDataset struct {
	plan        RobustPlan
	bundle      robustBundle
	identity    string
	rows, daily []market.Candle
	funding     []market.Funding
	stress      map[int64]robustStress
	windows     []optimizeWindow
	aggregation market.DailyAggregation
}

func robustLoad(p RobustPlan) (*robustDataset, error) {
	if p.Version != robustVersion || (p.AccountMode != "fold-reset-v1" && p.AccountMode != "fixed-continuous-v1") || len(p.Windows) < 1 || len(p.Windows) > 6 || (p.AccountMode == "fixed-continuous-v1" && len(p.Windows) != 1) {
		return nil, errors.New("invalid robust version/account mode/windows")
	}
	step, e := market.IntervalMillis(p.Interval)
	if e != nil {
		return nil, e
	}
	if p.FundingMode != "exact" && p.FundingMode != "proxy-stress-v1" {
		return nil, errors.New("explicit exact or proxy-stress-v1 required")
	}
	if len(p.FundingScenarios) < 1 || len(p.FundingScenarios) > 2 || p.FundingScenarios[0] != "central" || (len(p.FundingScenarios) == 2 && p.FundingScenarios[1] != "proxy-adverse") || (p.FundingMode == "exact" && len(p.FundingScenarios) != 1) {
		return nil, errors.New("invalid funding scenarios")
	}
	if p.FundingMode == "proxy-stress-v1" && len(p.FundingScenarios) != 2 {
		return nil, errors.New("proxy-stress requires central and adverse ledgers")
	}
	if len(p.CostMultipliers) < 1 || len(p.CostMultipliers) > 3 {
		return nil, errors.New("invalid cost scenarios")
	}
	for i, m := range p.CostMultipliers {
		if m != 1 && m != 1.5 && m != 2 {
			return nil, errors.New("unsupported cost multiplier")
		}
		if i > 0 && m <= p.CostMultipliers[i-1] {
			return nil, errors.New("cost scenarios must be unique ordered")
		}
	}
	p.BundleDir, e = filepath.Abs(p.BundleDir)
	if e != nil {
		return nil, e
	}
	hs, e := optimizeDate(p.HistoryStart)
	if e != nil {
		return nil, e
	}
	if hs%step != 0 {
		return nil, errors.New("history boundary")
	}
	p.HistoryStart = iso(hs)
	windows := make([]optimizeWindow, len(p.Windows))
	names := map[string]bool{}
	for i, w := range p.Windows {
		s, e := optimizeDate(w.Start)
		if e != nil {
			return nil, e
		}
		end, e := optimizeDate(w.End)
		if e != nil {
			return nil, e
		}
		if w.Name == "" || names[w.Name] || s >= end || s%86400000 != 0 || end%86400000 != 0 || (i > 0 && windows[i-1].end != s) {
			return nil, errors.New("robust windows require contiguous complete UTC days and unique names")
		}
		names[w.Name] = true
		windows[i] = optimizeWindow{s, end}
		p.Windows[i].Start = iso(s)
		p.Windows[i].End = iso(end)
	}
	if hs >= windows[0].start {
		return nil, errors.New("initialization must precede scoring")
	}
	raw, e := os.ReadFile(filepath.Join(p.BundleDir, "bundle.json"))
	if e != nil {
		return nil, e
	}
	var b robustBundle
	if e = json.Unmarshal(raw, &b); e != nil {
		return nil, e
	}
	if b.Version != "robust-bundle-v1" || b.SchemaVersion != 1 || b.Symbol != "ETHUSDT" || b.Source != "https://fapi.binance.com (USD-M perpetual REST)" || b.Interval != p.Interval || (b.FundingMode != "exact" && b.FundingMode != "proxy-stress-v1") || (p.FundingMode == "proxy-stress-v1" && b.FundingMode != p.FundingMode) {
		return nil, errors.New("bundle identity/mode differs")
	}
	hash := sha256.New()
	hash.Write(raw)
	paths := map[string]string{}
	snap := map[string]string{}
	for _, key := range []string{"candles", "daily", "funding", "stress"} {
		f, ok := b.Files[key]
		if !ok || f.Path == "" || filepath.IsAbs(f.Path) || filepath.Clean(f.Path) != f.Path || filepath.Base(f.Path) != f.Path {
			return nil, errors.New("missing or unsafe bundle file")
		}
		path := filepath.Join(p.BundleDir, f.Path)
		data, e := os.ReadFile(path)
		if e != nil {
			return nil, e
		}
		h := sha256.Sum256(data)
		sha := hex.EncodeToString(h[:])
		if sha != f.SHA256 {
			return nil, fmt.Errorf("bundle %s hash differs", key)
		}
		info, err := os.Lstat(path)
		if err != nil || !info.Mode().IsRegular() {
			return nil, errors.New("bundle file must be regular, no symlink")
		}
		if key != "stress" && b.Sources[key].SHA256 != sha {
			return nil, errors.New("bundle differs from immutable source snapshot")
		}
		paths[key] = path
		snap[path] = sha
		fmt.Fprintf(hash, "%s:%s:", key, sha)
	}
	rows, _, e := market.LoadCandles(paths["candles"], p.Interval)
	if e != nil {
		return nil, e
	}
	daily, _, e := market.LoadCandles(paths["daily"], "1d")
	if e != nil {
		return nil, e
	}
	agg, e := market.ValidateDailyOverlap(rows, daily, p.Interval)
	if e != nil {
		return nil, e
	}
	bs, e := optimizeDate(b.HistoryStart)
	if e != nil {
		return nil, e
	}
	be, e := optimizeDate(b.End)
	if e != nil {
		return nil, e
	}
	ds, e := optimizeDate(b.DailyHistoryStart)
	if e != nil {
		return nil, e
	}
	if rows[0].OpenTime != bs || rows[len(rows)-1].CloseTime+1 != be || daily[0].OpenTime != ds || bs > hs || be < windows[len(windows)-1].end {
		return nil, errors.New("bundle range/initialization mismatch")
	}
	funding, e := market.LoadFunding(paths["funding"])
	if e != nil {
		return nil, e
	}
	if e = fundingCoverage(funding, windows[0].start, windows[len(windows)-1].end); e != nil {
		return nil, e
	}
	var bounds []robustStress
	sr, e := os.ReadFile(paths["stress"])
	if e != nil {
		return nil, e
	}
	if e = optimizeDecode(sr, &bounds); e != nil {
		return nil, e
	}
	stress := map[int64]robustStress{}
	for _, s := range bounds {
		if _, ok := stress[s.Time]; ok {
			return nil, errors.New("duplicate stress row")
		}
		stress[s.Time] = s
	}
	if e = robustVerifyStressSource(p.BundleDir, b, funding, stress, snap); e != nil {
		return nil, e
	}

	for _, f := range funding {
		if f.Time < windows[0].start || f.Time >= windows[len(windows)-1].end {
			continue
		}
		if f.MarkPriceSource == market.FundingMarkKlineOpen8h {
			if p.FundingMode == "exact" {
				return nil, errors.New("proxy funding in exact scoring range")
			}
			s, ok := stress[f.Time]
			if !ok || !finiteRobust(s.Lower) || !finiteRobust(s.Upper) || s.Lower <= 0 || s.Lower > f.MarkPrice || s.Upper < f.MarkPrice || s.Available < 0 || s.Available > f.Time || s.BucketOpen < 0 || s.BucketClose != s.BucketOpen+28800000 || s.BucketOpen != f.MarkPriceTime-28800000 || s.BucketClose > f.Time {
				return nil, errors.New("proxy stress missing or future/inconsistent bounds")
			}
		} else if s, ok := stress[f.Time]; ok && (s.Lower != f.MarkPrice || s.Upper != f.MarkPrice || s.Available > f.Time) {
			return nil, errors.New("exact funding bounds changed")
		}
	}
	begin := sort.Search(len(rows), func(i int) bool { return rows[i].OpenTime >= hs })
	end := windows[len(windows)-1].end
	stop := sort.Search(len(rows), func(i int) bool { return rows[i].OpenTime >= end })
	rows = rows[begin:stop]
	for path, sha := range snap {
		v, e := os.ReadFile(path)
		if e != nil {
			return nil, e
		}
		h := sha256.Sum256(v)
		if hex.EncodeToString(h[:]) != sha {
			return nil, errors.New("bundle changed while loading")
		}
	}
	now, e := os.ReadFile(filepath.Join(p.BundleDir, "bundle.json"))
	if e != nil {
		return nil, e
	}
	if string(raw) != string(now) {
		return nil, errors.New("bundle manifest changed while loading")
	}
	return &robustDataset{plan: p, bundle: b, identity: hex.EncodeToString(hash.Sum(nil)), rows: rows, daily: daily, funding: funding, stress: stress, windows: windows, aggregation: agg}, nil
}

// Reconstruct every stress mark from the frozen causal mark cache. File hashes
// alone would allow re-labelling a source or inventing self-declared bounds.
func robustVerifyStressSource(dir string, b robustBundle, funding []market.Funding, stress map[int64]robustStress, snap map[string]string) error {
	type markBucket struct{ open, high, low float64 }
	buckets := map[int64]markBucket{}
	source, hasSource := b.Sources["marks"]
	if hasSource {
		if source.Snapshot == "" || filepath.Base(source.Snapshot) != source.Snapshot {
			return errors.New("missing safe mark source snapshot")
		}
		path := filepath.Join(dir, source.Snapshot)
		info, err := os.Lstat(path)
		if err != nil || !info.Mode().IsRegular() {
			return errors.New("invalid mark source snapshot")
		}
		raw, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		h := sha256.Sum256(raw)
		sha := hex.EncodeToString(h[:])
		if sha != source.SHA256 {
			return errors.New("mark source snapshot hash differs")
		}
		snap[path] = sha
		var rows [][]any
		dec := json.NewDecoder(bytes.NewReader(raw))
		dec.UseNumber()
		if err = dec.Decode(&rows); err != nil {
			return err
		}
		var extra any
		if dec.Decode(&extra) != io.EOF {
			return errors.New("trailing mark source JSON")
		}
		number := func(v any) (float64, error) {
			switch x := v.(type) {
			case string:
				return strconv.ParseFloat(x, 64)
			case json.Number:
				return x.Float64()
			default:
				return 0, errors.New("invalid mark bucket number")
			}
		}
		last := int64(-1)
		for _, r := range rows {
			if len(r) != 12 {
				return errors.New("invalid official mark bucket schema")
			}
			v := make([]float64, 7)
			for _, i := range []int{0, 1, 2, 3, 4, 6} {
				v[i], err = number(r[i])
				if err != nil || !finiteRobust(v[i]) {
					return errors.New("invalid mark bucket values")
				}
			}
			t := int64(v[0])
			if float64(t) != v[0] || t < 0 || t%28800000 != 0 || v[6] != v[0]+28800000-1 || (last >= 0 && t != last+28800000) || v[3] <= 0 || v[2] < v[3] || v[1] < v[3] || v[1] > v[2] || v[4] < v[3] || v[4] > v[2] {
				return errors.New("invalid completed mark bucket")
			}
			last = t
			buckets[t] = markBucket{v[1], v[2], v[3]}
		}
	}
	for i, f := range funding {
		s, ok := stress[f.Time]
		if f.MarkPriceSource == "" {
			if !ok || s.Lower != f.MarkPrice || s.Upper != f.MarkPrice || s.Available != f.Time || s.BucketOpen != f.Time || s.BucketClose != f.Time {
				return errors.New("exact funding stress differs")
			}
			continue
		}
		if !hasSource {
			return errors.New("proxy needs frozen mark source")
		}
		current, okCurrent := buckets[f.MarkPriceTime]
		prior, okPrior := buckets[f.MarkPriceTime-28800000]
		if !okCurrent || current.open != f.MarkPrice {
			return errors.New("proxy differs from source opening")
		}
		if !okPrior {
			if i == 0 && !ok {
				continue
			}
			return errors.New("missing prior complete mark bucket")
		}
		if !ok || s.Lower != math.Min(prior.low, f.MarkPrice) || s.Upper != math.Max(prior.high, f.MarkPrice) || s.Available != f.MarkPriceTime || s.BucketOpen != f.MarkPriceTime-28800000 || s.BucketClose != f.MarkPriceTime {
			return errors.New("stress differs from causal source")
		}
	}
	if len(stress) > len(funding) {
		return errors.New("stress row without funding")
	}
	return nil
}
func finiteRobust(x float64) bool { return !math.IsNaN(x) && !math.IsInf(x, 0) }
func (ds *robustDataset) validate(c strategy.Config) error {
	if e := c.Validate(); e != nil {
		return e
	}
	warm := sort.Search(len(ds.rows), func(i int) bool { return ds.rows[i].OpenTime >= ds.windows[0].start })
	if warm < optimizeWarmup(c.Classic) {
		return robustInsufficientError{"target initialization"}
	}
	if c.Classifier.Family == "aligned-extended" && (warm-4)/c.Classic.SampleStride < c.Classic.Neighbors {
		return robustInsufficientError{"mature aligned samples"}
	}
	start, e := optimizeDate(c.Daily.HistoryStart)
	if e != nil {
		return e
	}
	if start%86400000 != 0 || start < ds.daily[0].OpenTime || start > ds.rows[0].OpenTime {
		return errors.New("invalid D1 initialization")
	}
	if c.NeedsDaily() && (ds.windows[0].start-start)/86400000 < int64(c.Daily.X+3) {
		return robustInsufficientError{"D1 initialization"}
	}
	step, _ := market.IntervalMillis(ds.plan.Interval)
	firstVector := optimizeWarmup(c.Classic) - 1
	if c.NeedsEnvelope() {
		firstVector = max(firstVector, c.Envelope.X+2+c.Envelope.ATRLength)
	}
	for _, f := range c.Classifier.FeatureGroup.Features {
		if f.Normalization != "rolling-z" {
			continue
		}
		first := f.A + f.Window
		if f.Name == "KERNEL_DEVIATION" {
			first = c.Envelope.X + 2 + c.Envelope.ATRLength + f.Window
		}
		if f.Name == "D1_SLOPE" {
			// Two finite daily centers precede the target-bar rolling-z window.
			available := start + int64(c.Daily.X+3)*86400000
			first = max(0, int((available-ds.rows[0].OpenTime+step-1)/step)-1) + f.Window - 1
		}
		firstVector = max(firstVector, first)
	}
	required := firstVector
	if c.Classifier.Family == "aligned-extended" {
		stride := c.Classic.SampleStride
		required = (firstVector+stride-1)/stride*stride + (c.Classic.Neighbors-1)*stride + 4
		if c.Classic.MaxBarsBack < c.Classic.Neighbors*stride+3 {
			return robustInsufficientError{"mature neighbor lookback"}
		}
	}
	if warm < required {
		return robustInsufficientError{fmt.Sprintf("chained feature/mature model: have %d bars need %d", warm, required)}
	}

	if c.Risk.Enabled && warm < c.Risk.ATRPeriod {
		return robustInsufficientError{"risk initialization"}
	}
	return nil
}
func (ds *robustDataset) ready() map[string]any {
	return map[string]any{"type": "ready", "protocol_version": 3, "version": robustVersion, "plan": ds.plan, "bundle_identity": ds.identity, "default_config": strategy.DefaultConfig(), "ledger_evaluations": len(ds.windows) * len(ds.plan.FundingScenarios) * len(ds.plan.CostMultipliers), "daily_aggregation": ds.aggregation, "synthetic": ds.bundle.Synthetic, "evaluation_calls": 0}
}
func (ds *robustDataset) evaluate(c strategy.Config, traceDir string) (map[string]any, error) {
	if e := ds.validate(c); e != nil {
		return nil, e
	}
	started := time.Now()
	start, _ := optimizeDate(c.Daily.HistoryStart)
	begin := sort.Search(len(ds.daily), func(i int) bool { return ds.daily[i].OpenTime >= start })
	end := ds.windows[len(ds.windows)-1].end
	stop := sort.Search(len(ds.daily), func(i int) bool { return ds.daily[i].OpenTime >= end })
	strategyStart := time.Now()
	r, e := strategy.RunRobust(ds.rows, ds.daily[begin:stop], c)
	if e != nil {
		return nil, e
	}
	strategySeconds := time.Since(strategyStart).Seconds()
	metadata := strategy.EntryMetadata(r, c)
	accounts := []map[string]any{}
	ledgerStart := time.Now()
	for wi, w := range ds.windows {
		limit := sort.Search(len(ds.rows), func(i int) bool { return ds.rows[i].OpenTime >= w.end })
		exact, proxy := 0, 0
		for _, f := range ds.funding {
			if f.Time >= w.start && f.Time < w.end {
				if f.MarkPriceSource == "" {
					exact++
				} else {
					proxy++
				}
			}
		}
		for _, scenario := range ds.plan.FundingScenarios {
			for _, mult := range ds.plan.CostMultipliers {
				b := c.LedgerConfig()
				b.StartTime = w.start
				b.EndTime = w.end
				b.FeeBPS = 5 * mult
				b.SlippageBPS = 2 * mult
				opts := backtest.EvaluationOptions{}
				opts.FundingMark = func(f market.Funding, d int, q float64) (float64, error) {
					if scenario == "proxy-adverse" && f.MarkPriceSource != "" {
						s, ok := ds.stress[f.Time]
						if !ok {
							return 0, errors.New("missing scenario funding")
						}
						if float64(d)*q*f.Rate > 0 {
							return s.Upper, nil
						}
						return s.Lower, nil
					}
					return f.MarkPrice, nil
				}
				tracePath, traceHash := "", ""
				var file *os.File
				h := sha256.New()
				if traceDir != "" {
					if e = os.MkdirAll(traceDir, 0755); e != nil {
						return nil, e
					}
					tracePath = filepath.Join(traceDir, fmt.Sprintf("window-%d-%s-cost-%g.jsonl", wi, scenario, mult))
					file, e = os.OpenFile(tracePath, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0644)
					if e != nil {
						return nil, e
					}
					enc := json.NewEncoder(io.MultiWriter(file, h))
					opts.Trace = func(event backtest.TraceEvent) error { return enc.Encode(event) }
				}
				var atr []float64
				if r.RiskATR != nil {
					atr = r.RiskATR[:limit]
				}
				report, err := backtest.EvaluateWithOptions(ds.rows[:limit], r.Points[:limit], ds.funding, b, metadata[:limit], c.Risk, atr, opts)
				if file != nil {
					syncErr := file.Sync()
					closeErr := file.Close()
					if err == nil {
						err = syncErr
					}
					if err == nil {
						err = closeErr
					}
					traceHash = hex.EncodeToString(h.Sum(nil))
				}
				if err != nil {
					return nil, err
				}
				accounts = append(accounts, map[string]any{"window": ds.plan.Windows[wi], "funding_scenario": scenario, "cost_multiplier": mult, "metrics": report.Metrics, "ledger_config": b, "trace_path": tracePath, "trace_sha256": traceHash, "funding_disclosure": map[string]any{"mode": ds.plan.FundingMode, "exact_rows": exact, "proxy_rows": proxy, "signed_cash": report.Metrics.Funding, "adverse_is_proven_bound": false}, "account_mode": ds.plan.AccountMode})
			}
		}
	}
	return map[string]any{"type": "result", "protocol_version": 3, "version": robustVersion, "plan": ds.plan, "bundle_identity": ds.identity, "config": c, "accounts": accounts, "ledger_evaluations": len(accounts), "timing": map[string]float64{"strategy_seconds": strategySeconds, "ledger_seconds": time.Since(ledgerStart).Seconds(), "request_seconds": time.Since(started).Seconds()}, "data_use": "exposed_history unless separately future-frozen; no adoption claim"}, nil
}
func robustEval(args []string) error {
	flags := flag.NewFlagSet("robust-eval", flag.ContinueOnError)
	pp := flags.String("plan", "", "robust-eval-v1 plan")
	cp := flags.String("config", "", "complete schema5 configuration; omit for protocol3 server")
	inspect := flags.Bool("inspect", false, "validate data and count ledgers without strategy evaluation")
	td := flags.String("trace-dir", "", "exclusive streaming trace directory")
	if e := flags.Parse(args); e != nil {
		return e
	}
	if *pp == "" || flags.NArg() != 0 {
		return errors.New("robust-eval requires -plan")
	}
	raw, e := os.ReadFile(*pp)
	if e != nil {
		return e
	}
	var p RobustPlan
	if e = optimizeDecode(raw, &p); e != nil {
		return e
	}
	loadStart := time.Now()
	ds, e := robustLoad(p)
	if e != nil {
		return e
	}
	ready := ds.ready()
	ready["load_seconds"] = time.Since(loadStart).Seconds()
	if *cp != "" {
		raw, e = os.ReadFile(*cp)
		if e != nil {
			return e
		}
		c, e := strategy.DecodeConfig(raw)
		if e != nil {
			return e
		}
		if e = ds.validate(c); e != nil {
			return e
		}
		if *inspect {
			return json.NewEncoder(os.Stdout).Encode(ready)
		}
		result, e := ds.evaluate(c, *td)
		if e != nil {
			return e
		}
		result["load_seconds"] = ready["load_seconds"]
		return json.NewEncoder(os.Stdout).Encode(result)
	}
	if *inspect {
		return json.NewEncoder(os.Stdout).Encode(ready)
	}
	enc := json.NewEncoder(os.Stdout)
	if e = enc.Encode(ready); e != nil {
		return e
	}
	scan := bufio.NewScanner(os.Stdin)
	scan.Buffer(make([]byte, 65536), 4*1024*1024)
	for scan.Scan() {
		var req struct {
			ID       int             `json:"id"`
			Config   json.RawMessage `json:"config"`
			TraceDir string          `json:"trace_dir"`
		}
		kind := "request"
		e = optimizeDecode(scan.Bytes(), &req)
		var c strategy.Config
		if e == nil {
			kind = "config"
			c, e = strategy.DecodeConfig(req.Config)
			if e == nil {
				e = ds.validate(c)
			}
		}
		var result map[string]any
		if e == nil {
			kind = "evaluation"
			result, e = ds.evaluate(c, req.TraceDir)
		}
		if e != nil {
			var insufficient robustInsufficientError
			if errors.As(e, &insufficient) {
				kind = "configuration_insufficient"
			}
			var protection backtest.EquityProtectionError
			if errors.As(e, &protection) {
				kind = "equity_protection"
			}
			result = map[string]any{"type": "error", "error": e.Error(), "error_kind": kind}
		}
		result["id"] = req.ID
		if e = enc.Encode(result); e != nil {
			return e
		}
	}
	return scan.Err()
}

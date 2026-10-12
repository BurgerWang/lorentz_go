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
	"math"
	"os"
	"path/filepath"
	"sort"
	"time"

	"lorentzgo/backtest"
	"lorentzgo/indicator"
	"lorentzgo/internal/strictjson"
	"lorentzgo/market"
)

type OptimizeFold struct {
	Name  string `json:"name"`
	Start string `json:"start"`
	End   string `json:"end"`
}
type OptimizePlan struct {
	SchemaVersion    int            `json:"schema_version"`
	DatasetDir       string         `json:"dataset_dir"`
	Interval         string         `json:"interval"`
	HistoryStart     string         `json:"history_start"`
	Folds            []OptimizeFold `json:"folds"`
	FeeBPS           float64        `json:"fee_bps"`
	SlippageBPS      float64        `json:"slippage_bps"`
	MinTrades        int            `json:"min_trades"`
	MinPositiveFolds int            `json:"min_positive_folds"`
}
type optimizeWindow struct{ start, end int64 }
type optimizeDataset struct {
	plan     OptimizePlan
	rows     []market.Candle
	funding  []market.Funding
	windows  []optimizeWindow
	identity string
	metadata datasetMetadata
}
type optimizeFoldResult struct {
	Name    string           `json:"name"`
	Start   string           `json:"start"`
	End     string           `json:"end"`
	Metrics backtest.Metrics `json:"metrics"`
}
type optimizeAggregate struct {
	NetReturnPct          float64 `json:"net_return_pct"`
	WinRatePct            float64 `json:"win_rate_pct"`
	Trades                int     `json:"trades"`
	Wins                  int     `json:"wins"`
	PositiveFolds         int     `json:"positive_folds"`
	WorstFoldNetReturnPct float64 `json:"worst_fold_net_return_pct"`
	MaxFoldDrawdownPct    float64 `json:"max_fold_drawdown_pct"`
}

// Require complete schemas, exact keys and no duplicate object fields.
func optimizeDecode(raw []byte, dst any) error {
	return strictjson.Decode(raw, dst)
}
func optimizeDate(s string) (int64, error) {
	t, e := time.Parse("2006-01-02", s)
	if e != nil {
		t, e = time.Parse(time.RFC3339, s)
	}
	if e != nil {
		return 0, fmt.Errorf("invalid UTC date %q", s)
	}
	_, offset := t.Zone()
	if offset != 0 || t.Nanosecond() != 0 || t.UnixMilli() < 0 {
		return 0, fmt.Errorf("date must be nonnegative UTC with whole seconds: %q", s)
	}
	return t.UnixMilli(), nil
}
func optimizeNormalizePlan(p OptimizePlan) (OptimizePlan, []optimizeWindow, int64, error) {
	fail := func(e error) (OptimizePlan, []optimizeWindow, int64, error) { return p, nil, 0, e }
	if p.SchemaVersion != 1 || p.DatasetDir == "" || len(p.Folds) < 2 || p.MinTrades < 30 || p.MinPositiveFolds < 1 || p.MinPositiveFolds > len(p.Folds) {
		return fail(errors.New("invalid plan version, dataset, folds, or minimum constraints"))
	}
	step, e := market.IntervalMillis(p.Interval)
	if e != nil {
		return fail(e)
	}
	hs, e := optimizeDate(p.HistoryStart)
	if e != nil {
		return fail(e)
	}
	if hs%step != 0 {
		return fail(errors.New("history_start is not a candle boundary"))
	}
	p.DatasetDir, e = filepath.Abs(p.DatasetDir)
	if e != nil {
		return fail(e)
	}
	p.HistoryStart = iso(hs)
	windows := make([]optimizeWindow, len(p.Folds))
	names := map[string]bool{}
	for i, f := range p.Folds {
		s, e := optimizeDate(f.Start)
		if e != nil {
			return fail(e)
		}
		end, e := optimizeDate(f.End)
		if e != nil {
			return fail(e)
		}
		if f.Name == "" || names[f.Name] || s >= end || s%step != 0 || end%step != 0 || (i > 0 && windows[i-1].end != s) {
			return fail(errors.New("folds must have unique names, aligned nonempty dates, and contiguous ordered windows"))
		}
		names[f.Name] = true
		windows[i] = optimizeWindow{s, end}
		p.Folds[i].Start = iso(s)
		p.Folds[i].End = iso(end)
	}
	if hs >= windows[0].start {
		return fail(errors.New("history_start must precede the first fold"))
	}
	b := backtest.DefaultConfig()
	b.FeeBPS = p.FeeBPS
	b.SlippageBPS = p.SlippageBPS
	b.StartTime = windows[0].start
	b.EndTime = windows[len(windows)-1].end
	if e := b.Validate(); e != nil {
		return fail(e)
	}
	return p, windows, hs, nil
}
func optimizeDefaultConfig() indicator.Config {
	c := indicator.DefaultConfig()
	c.Algorithm = "original-online"
	return c
}

// This is a conservative initialization requirement, not a claim that a finite
// prefix reproduces an indicator initialized at the instrument's listing.
func optimizeWarmup(c indicator.Config) int {
	n := 200
	add := func(a, b int) int {
		limit := int(^uint(0) >> 1)
		if a > limit-b {
			return limit
		}
		return a + b
	}
	for _, f := range c.Features[:c.FeatureCount] {
		switch f.Name {
		case "RSI":
			n = max(n, add(f.A, 1))
		case "CCI", "ADX":
			n = max(n, f.A)
		case "WT":
			n = max(n, add(add(f.A, f.B), 4))
		}
	}
	if c.UseEMAFilter {
		n = max(n, c.EMAPeriod)
	}
	if c.UseSMAFilter {
		n = max(n, c.SMAPeriod)
	}
	if c.UseRegimeFilter {
		n = max(n, 200)
	}
	if c.UseVolatilityFilter {
		n = max(n, 10)
	}
	if c.UseADXFilter {
		n = max(n, 14)
	}
	if c.UseKernelFilter || c.UseDynamicExits {
		n = max(n, c.KernelX+2)
	}
	return n
}
func optimizePrepare(p OptimizePlan, all []market.Candle, funding []market.Funding) (*optimizeDataset, error) {
	p, windows, hs, e := optimizeNormalizePlan(p)
	if e != nil {
		return nil, e
	}
	q, e := market.ValidateCandles(all, p.Interval)
	if e != nil {
		return nil, e
	}
	end := windows[len(windows)-1].end
	if q.Start > hs || q.End < end {
		return nil, errors.New("dataset does not cover history and all folds")
	}
	begin := sort.Search(len(all), func(i int) bool { return all[i].OpenTime >= hs })
	stop := sort.Search(len(all), func(i int) bool { return all[i].OpenTime >= end })
	rows := all[begin:stop]
	if e := fundingCoverage(funding, windows[0].start, end); e != nil {
		return nil, e
	}
	// Validate every funding record even if no trial enters a position.
	b := backtest.DefaultConfig()
	b.PositionFraction = 0
	b.StartTime = windows[0].start
	b.EndTime = end
	if _, e := backtest.Evaluate(rows, make([]indicator.Point, len(rows)), funding, b); e != nil {
		return nil, e
	}
	ds := &optimizeDataset{plan: p, rows: rows, funding: funding, windows: windows}
	if e := ds.validateConfig(optimizeDefaultConfig()); e != nil {
		return nil, e
	}
	return ds, nil
}
func optimizeLoad(p OptimizePlan) (*optimizeDataset, error) {
	dir, e := filepath.Abs(p.DatasetDir)
	if e != nil {
		return nil, e
	}
	names := []string{"metadata.json", p.Interval + ".csv", "funding.json"}
	snapshots := make([][]byte, len(names))
	for i, name := range names {
		snapshots[i], e = os.ReadFile(filepath.Join(dir, name))
		if e != nil {
			return nil, e
		}
	}
	meta, e := verifiedDataset(dir)
	if e != nil {
		return nil, e
	}
	rows, q, e := market.LoadCandles(filepath.Join(dir, p.Interval+".csv"), p.Interval)
	if e != nil {
		return nil, e
	}
	if meta.Candles[p.Interval] != q {
		return nil, errors.New("candle coverage differs from dataset metadata")
	}
	funding, e := market.LoadFunding(filepath.Join(dir, "funding.json"))
	if e != nil {
		return nil, e
	}
	if len(funding) != meta.FundingRows {
		return nil, errors.New("funding row count differs from dataset metadata")
	}
	estimated := 0
	for _, f := range funding {
		if f.MarkPriceSource == market.FundingMarkKlineOpen8h {
			estimated++
		}
	}
	if estimated != meta.FundingEstimatedRows {
		return nil, errors.New("funding estimated row count differs from dataset metadata")
	}
	ds, e := optimizePrepare(p, rows, funding)
	if e != nil {
		return nil, e
	}
	ds.metadata = meta
	h := sha256.New()
	for i, name := range names {
		now, e := os.ReadFile(filepath.Join(dir, name))
		if e != nil {
			return nil, e
		}
		if !bytes.Equal(now, snapshots[i]) {
			return nil, errors.New("dataset changed while loading")
		}
		fmt.Fprintf(h, "%s:%d:", name, len(now))
		h.Write(now)
	}
	planJSON, e := json.Marshal(ds.plan)
	if e != nil {
		return nil, e
	}
	fmt.Fprintf(h, "plan:%d:", len(planJSON))
	h.Write(planJSON)
	ds.identity = hex.EncodeToString(h.Sum(nil))
	return ds, nil
}
func (ds *optimizeDataset) validateConfig(c indicator.Config) error {
	if e := c.Validate(); e != nil {
		return e
	}
	if c.Algorithm != "original-online" || c.MinVoteFraction != 0 || c.SampleStride != 4 {
		return errors.New("optimization requires original-online, min_vote_fraction=0, sample_stride=4")
	}
	warm := sort.Search(len(ds.rows), func(i int) bool { return ds.rows[i].OpenTime >= ds.windows[0].start })
	if warm < optimizeWarmup(c) {
		return fmt.Errorf("insufficient initialization history: %d candles, require %d", warm, optimizeWarmup(c))
	}
	return nil
}
func optimizeSummarize(folds []optimizeFoldResult) optimizeAggregate {
	a := optimizeAggregate{}
	growth := 1.0
	for i, f := range folds {
		m := f.Metrics
		growth *= 1 + m.NetReturnPct/100
		a.Trades += m.Trades
		a.Wins += m.Wins
		if m.NetReturnPct > 0 {
			a.PositiveFolds++
		}
		if i == 0 || m.NetReturnPct < a.WorstFoldNetReturnPct {
			a.WorstFoldNetReturnPct = m.NetReturnPct
		}
		a.MaxFoldDrawdownPct = math.Max(a.MaxFoldDrawdownPct, m.MaxDrawdownPct)
	}
	a.NetReturnPct = 100 * (growth - 1)
	if a.Trades > 0 {
		a.WinRatePct = 100 * float64(a.Wins) / float64(a.Trades)
	}
	return a
}
func (ds *optimizeDataset) evaluate(c indicator.Config) ([]optimizeFoldResult, optimizeAggregate, error) {
	points, e := indicator.Run(ds.rows, c)
	if e != nil {
		return nil, optimizeAggregate{}, e
	}
	folds := make([]optimizeFoldResult, 0, len(ds.windows))
	for i, w := range ds.windows {
		stop := sort.Search(len(ds.rows), func(i int) bool { return ds.rows[i].OpenTime >= w.end })
		b := backtest.DefaultConfig()
		b.StartTime = w.start
		b.EndTime = w.end
		b.FeeBPS = ds.plan.FeeBPS
		b.SlippageBPS = ds.plan.SlippageBPS
		r, e := backtest.Evaluate(ds.rows[:stop], points[:stop], ds.funding, b)
		if e != nil {
			return nil, optimizeAggregate{}, e
		}
		f := ds.plan.Folds[i]
		folds = append(folds, optimizeFoldResult{f.Name, f.Start, f.End, r.Metrics})
	}
	a := optimizeSummarize(folds)
	if math.IsNaN(a.NetReturnPct) || math.IsInf(a.NetReturnPct, 0) {
		return nil, a, errors.New("nonfinite compounded fold return")
	}
	return folds, a, nil
}
func optimizeServe(ds *optimizeDataset, in io.Reader, out io.Writer) error {
	enc := json.NewEncoder(out)
	ready := map[string]any{"type": "ready", "protocol_version": 1, "plan": ds.plan, "default_config": optimizeDefaultConfig(), "data_identity": ds.identity, "history_candles": len(ds.rows), "metadata": ds.metadata, "history_start": iso(ds.rows[0].OpenTime), "history_end": iso(ds.rows[len(ds.rows)-1].CloseTime + 1)}
	if e := enc.Encode(ready); e != nil {
		return e
	}
	scan := bufio.NewScanner(in)
	scan.Buffer(make([]byte, 65536), 4*1024*1024)
	for scan.Scan() {
		started := time.Now()
		var req struct {
			ID     int             `json:"id"`
			Config json.RawMessage `json:"config"`
		}
		var config indicator.Config
		kind := "request"
		e := optimizeDecode(scan.Bytes(), &req)
		if e == nil {
			kind = "config"
			config, e = indicator.DecodeConfig(req.Config)
			if e == nil {
				e = ds.validateConfig(config)
			}
		}
		var folds []optimizeFoldResult
		var aggregate optimizeAggregate
		if e == nil {
			kind = "evaluation"
			folds, aggregate, e = ds.evaluate(config)
		}
		if e != nil {
			if e = enc.Encode(map[string]any{"type": "error", "id": req.ID, "error": e.Error(), "error_kind": kind}); e != nil {
				return e
			}
			continue
		}
		if e := enc.Encode(map[string]any{"type": "result", "id": req.ID, "config": config, "folds": folds, "aggregate": aggregate, "elapsed_seconds": time.Since(started).Seconds()}); e != nil {
			return e
		}
	}
	return scan.Err()
}
func optimizeEval(args []string) error {
	f := flag.NewFlagSet("optimize-eval", flag.ContinueOnError)
	path := f.String("plan", "", "optimization plan JSON file")
	configPath := f.String("config", "", "evaluate one complete indicator JSON and exit")
	if e := f.Parse(args); e != nil {
		return e
	}
	if *path == "" || f.NArg() != 0 {
		return errors.New("optimize-eval requires -plan <JSON file> and no positional arguments")
	}
	raw, e := os.ReadFile(*path)
	if e != nil {
		return e
	}
	var p OptimizePlan
	if e := optimizeDecode(raw, &p); e != nil {
		return fmt.Errorf("plan: %w", e)
	}
	if _, _, _, e := optimizeNormalizePlan(p); e != nil {
		return e
	}
	ds, e := optimizeLoad(p)
	if e != nil {
		return e
	}
	if *configPath != "" {
		raw, e := os.ReadFile(*configPath)
		if e != nil {
			return e
		}
		c, e := indicator.DecodeConfig(raw)
		if e != nil {
			return e
		}
		if e := ds.validateConfig(c); e != nil {
			return e
		}
		started := time.Now()
		folds, aggregate, e := ds.evaluate(c)
		if e != nil {
			return e
		}
		return json.NewEncoder(os.Stdout).Encode(map[string]any{"type": "result", "id": 0, "config": c, "folds": folds, "aggregate": aggregate, "elapsed_seconds": time.Since(started).Seconds()})
	}
	return optimizeServe(ds, os.Stdin, os.Stdout)
}

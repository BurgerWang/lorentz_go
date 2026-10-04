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
	"os"
	"path/filepath"
	"sort"
	"time"
)

type enhancedDataset struct {
	base             *optimizeDataset
	daily            []market.Candle
	dailyIdentity    string
	dailyAggregation market.DailyAggregation
}

func enhancedLoad(p OptimizePlan) (*enhancedDataset, error) {
	if p.Interval != "1h" && p.Interval != "15m" {
		return nil, errors.New("enhanced evaluation supports 1h/15m only")
	}
	ds, e := optimizeLoad(p)
	if e != nil {
		return nil, e
	}
	path := filepath.Join(ds.plan.DatasetDir, "1d.csv")
	raw, e := os.ReadFile(path)
	if e != nil {
		return nil, e
	}
	daily, q, e := market.LoadCandles(path, "1d")
	if e != nil {
		return nil, e
	}
	if ds.metadata.Candles["1d"] != q {
		return nil, errors.New("D1 coverage differs from market metadata")
	}
	lowAll, _, e := market.LoadCandles(filepath.Join(ds.plan.DatasetDir, p.Interval+".csv"), p.Interval)
	if e != nil {
		return nil, e
	}
	aggregation, e := market.ValidateDailyAggregation(lowAll, daily, p.Interval)
	if e != nil {
		return nil, e
	}
	now, e := os.ReadFile(path)
	if e != nil {
		return nil, e
	}
	if !bytes.Equal(raw, now) {
		return nil, errors.New("D1 changed while loading")
	}
	h := sha256.Sum256(raw)
	return &enhancedDataset{base: ds, daily: daily, dailyIdentity: hex.EncodeToString(h[:]), dailyAggregation: aggregation}, nil
}
func (ds *enhancedDataset) validate(c strategy.Config) error {
	if e := c.Validate(); e != nil {
		return e
	}
	if c.Classifier.Family == "aligned-extended" {
		warm := sort.Search(len(ds.base.rows), func(i int) bool { return ds.base.rows[i].OpenTime >= ds.base.windows[0].start })
		if warm < optimizeWarmup(c.Classic) {
			return fmt.Errorf("insufficient aligned initialization")
		}
	} else if e := ds.base.validateConfig(c.Classic); e != nil {
		return e
	}
	if c.SchemaVersion >= 3 {
		warm := sort.Search(len(ds.base.rows), func(i int) bool { return ds.base.rows[i].OpenTime >= ds.base.windows[0].start })
		for _, f := range c.Classifier.FeatureGroup.Features {
			required := f.A + f.Window
			if f.Name == "KERNEL_DEVIATION" {
				required = c.Envelope.X + 2 + c.Envelope.ATRLength + f.Window
			}
			if f.Normalization == "rolling-z" && warm < required {
				return fmt.Errorf("insufficient feature warmup for %s", f.Name)
			}
		}
	}
	if c.SchemaVersion >= 4 && c.Risk.Enabled {
		warm := sort.Search(len(ds.base.rows), func(i int) bool { return ds.base.rows[i].OpenTime >= ds.base.windows[0].start })
		if warm < c.Risk.ATRPeriod {
			return fmt.Errorf("insufficient risk ATR initialization")
		}
	}
	start, e := optimizeDate(c.Daily.HistoryStart)
	if e != nil {
		return e
	}
	const day int64 = 86400000
	if start%day != 0 || start < ds.daily[0].OpenTime || start >= ds.base.rows[0].OpenTime {
		return errors.New("invalid D1 initialization coverage")
	}
	if c.NeedsDaily() {
		first := ds.base.windows[0].start
		if (first-start)/day < int64(c.Daily.X+3) {
			return errors.New("insufficient D1 initialization before scoring")
		}
	}
	return nil
}
func (ds *enhancedDataset) evaluate(c strategy.Config) (map[string]any, error) {
	if e := ds.validate(c); e != nil {
		return nil, e
	}
	start, _ := optimizeDate(c.Daily.HistoryStart)
	begin := sort.Search(len(ds.daily), func(i int) bool { return ds.daily[i].OpenTime >= start })
	end := ds.base.windows[len(ds.base.windows)-1].end
	stop := sort.Search(len(ds.daily), func(i int) bool { return ds.daily[i].OpenTime >= end })
	started := time.Now()
	r, e := strategy.Run(ds.base.rows, ds.daily[begin:stop], c)
	if e != nil {
		return nil, e
	}
	strategySeconds := time.Since(started).Seconds()
	ledgerStarted := time.Now()
	folds := make([]optimizeFoldResult, 0, len(ds.base.windows))
	executionCounts := map[string]int{}
	exitReasons := map[string]int{}
	metadata := strategy.EntryMetadata(r, c)
	for i, w := range ds.base.windows {
		stop := sort.Search(len(ds.base.rows), func(i int) bool { return ds.base.rows[i].OpenTime >= w.end })
		b := c.LedgerConfig()
		b.StartTime = w.start
		b.EndTime = w.end
		b.FeeBPS = ds.base.plan.FeeBPS
		b.SlippageBPS = ds.base.plan.SlippageBPS
		var atr []float64
		if r.RiskATR != nil {
			atr = r.RiskATR[:stop]
		}
		report, e := backtest.EvaluateWithRisk(ds.base.rows[:stop], r.Points[:stop], ds.base.funding, b, metadata[:stop], c.Risk, atr)
		if e != nil {
			return nil, e
		}
		for _, trade := range report.Trades {
			executionCounts[trade.EntryKind]++
			exitReasons[trade.Reason]++
		}
		f := ds.base.plan.Folds[i]
		folds = append(folds, optimizeFoldResult{f.Name, f.Start, f.End, report.Metrics})
	}
	events := []strategy.Event{}
	for _, event := range r.Events {
		if event.DecisionTime > ds.base.windows[0].start && event.DecisionTime <= end {
			events = append(events, event)
		}
	}
	return map[string]any{"type": "result", "config": c, "plan": ds.base.plan, "data_identity": ds.base.identity, "daily_identity": ds.dailyIdentity, "daily_aggregation": ds.dailyAggregation, "folds": folds, "aggregate": optimizeSummarize(folds), "event_counts": strategy.Counts(events), "event_reasons": strategy.ReasonCounts(events), "executed_entries": executionCounts, "exit_reasons": exitReasons, "elapsed_seconds": time.Since(started).Seconds(), "timing": map[string]float64{"strategy_seconds": strategySeconds, "ledger_seconds": time.Since(ledgerStarted).Seconds()}, "model_score_summary": modelScoreSummary(r), "data_use": "exposed_development; not independent final validation"}, nil
}
func enhancedServe(ds *enhancedDataset, in io.Reader, out io.Writer) error {
	enc := json.NewEncoder(out)
	if e := enc.Encode(map[string]any{"type": "ready", "protocol_version": 2, "strategy_schema_version": 5, "supported_strategy_schemas": []int{1, 2, 3, 4, 5}, "plan": ds.base.plan, "default_config": strategy.DefaultConfig(), "data_identity": ds.base.identity, "daily_identity": ds.dailyIdentity, "daily_aggregation": ds.dailyAggregation}); e != nil {
		return e
	}
	scan := bufio.NewScanner(in)
	scan.Buffer(make([]byte, 65536), 4*1024*1024)
	for scan.Scan() {
		var req struct {
			ID     int             `json:"id"`
			Config json.RawMessage `json:"config"`
		}
		kind := "request"
		e := optimizeDecode(scan.Bytes(), &req)
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
			result, e = ds.evaluate(c)
		}
		if e != nil {
			result = map[string]any{"type": "error", "error": e.Error(), "error_kind": kind}
		}
		result["id"] = req.ID
		if e := enc.Encode(result); e != nil {
			return e
		}
	}
	return scan.Err()
}
func enhancedEval(args []string) error {
	f := flag.NewFlagSet("enhanced-eval", flag.ContinueOnError)
	planPath := f.String("plan", "", "existing version 1 evaluation plan")
	configPath := f.String("config", "", "complete strategy schema1-5 config; omit for JSON-line server")
	if e := f.Parse(args); e != nil {
		return e
	}
	if *planPath == "" || f.NArg() != 0 {
		return fmt.Errorf("enhanced-eval requires -plan and no positional args")
	}
	raw, e := os.ReadFile(*planPath)
	if e != nil {
		return e
	}
	var p OptimizePlan
	if e := optimizeDecode(raw, &p); e != nil {
		return e
	}
	ds, e := enhancedLoad(p)
	if e != nil {
		return e
	}
	if *configPath == "" {
		return enhancedServe(ds, os.Stdin, os.Stdout)
	}
	raw, e = os.ReadFile(*configPath)
	if e != nil {
		return e
	}
	c, e := strategy.DecodeConfig(raw)
	if e != nil {
		return e
	}
	r, e := ds.evaluate(c)
	if e != nil {
		return e
	}
	return json.NewEncoder(os.Stdout).Encode(r)
}

func modelScoreSummary(r strategy.Result) map[string]any {
	n := 0
	low, high := 1.0, -1.0
	for _, s := range r.Scores {
		if s.Ready {
			n++
			if s.Score < low {
				low = s.Score
			}
			if s.Score > high {
				high = s.Score
			}
		}
	}
	if n == 0 {
		low, high = 0, 0
	}
	return map[string]any{"ready_bars": n, "min": low, "max": high, "unit": "signed vote strength; not probability"}
}

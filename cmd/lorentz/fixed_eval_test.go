package main

import (
	"encoding/json"
	"lorentzgo/backtest"
	"lorentzgo/market"
	"lorentzgo/strategy"
	"math"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

func fixedFixture(t *testing.T) (walkPlan, *walkDataset) {
	t.Helper()
	p, original := walkFixture(t)
	p.Candidates = []walkCandidate{}
	p.Budget = 3
	p, base, required, e := fixedNormalize(p)
	if e != nil {
		t.Fatal(e)
	}
	original.ds.base.plan = base
	w, e := fixedPrepare(p, original.ds, required, "synthetic-exposure")
	if e != nil {
		t.Fatal(e)
	}
	return p, w
}

func TestFixedContinuousSameLedgerAndReplay(t *testing.T) {
	for _, risk := range []bool{false, true} {
		_, w := fixedFixture(t)
		if risk {
			w.baseline.Risk.Enabled = true
			w.plan.Baseline, _ = json.Marshal(w.baseline)
			if e := w.ds.validate(w.baseline); e != nil {
				t.Fatal(e)
			}
		}
		// Place the artificial outer boundary inside an observed fixture trade
		// so continuity is exercised under both risk contracts, rather than
		// relying on a coincidental position at candle 500.
		previewSignals, e := w.runTo(w.outer[1].end, w.baseline)
		if e != nil {
			t.Fatal(e)
		}
		preview, e := w.ledger(previewSignals, w.baseline, w.outer[0].start, w.outer[1].end, 1, nil)
		if e != nil {
			t.Fatal(e)
		}
		boundary := int64(0)
		for _, trade := range preview.Trades {
			candidate := trade.EntryTime + 3600000
			if candidate >= w.outer[1].start && candidate < trade.ExitTime {
				boundary = candidate
				break
			}
		}
		if boundary == 0 {
			t.Fatal("fixture lacks a trade suitable for a continuous boundary")
		}
		w.outer[0].end, w.outer[1].start = boundary, boundary
		w.plan.Outer[0].End, w.plan.Outer[1].Start = iso(boundary), iso(boundary)
		if _, _, _, e := fixedNormalize(w.plan); e != nil {
			t.Fatal(e)
		}
		ready := fixedReady(w)
		if ready["required_evaluations"] != 3 || ready["config_identity"] != fixedIdentity(w.baseline) || ready["plan_identity"] != fixedIdentity(w.plan) {
			t.Fatal("inspect did not bind exact budget and complete contract")
		}
		a, e := fixedEvaluate(w)
		if e != nil {
			t.Fatal(e)
		}
		b, e := fixedEvaluate(w)
		if e != nil || !reflect.DeepEqual(a, b) {
			t.Fatalf("fixed replay changed: %v", e)
		}
		if a["evaluations_used"] != 3 || len(a["continuous_scenarios"].([]map[string]any)) != 3 {
			t.Fatal("fixed budget charged hidden selection or reset accounts")
		}
		start, end := w.outer[0].start, w.outer[len(w.outer)-1].end
		signals, e := w.runTo(end, w.baseline)
		if e != nil {
			t.Fatal(e)
		}
		var previous backtest.Metrics
		for i, scenario := range a["continuous_scenarios"].([]map[string]any) {
			config := w.baseline.LedgerConfig()
			config.StartTime, config.EndTime = start, end
			config.FeeBPS = 5 * w.plan.CostMultipliers[i]
			config.SlippageBPS = 2 * w.plan.CostMultipliers[i]
			// Independently call the existing single-account ledger with the full
			// points, funding, risk ATR and entry metadata, including the boundary.
			report, e := backtest.EvaluateWithRisk(w.ds.base.rows[:len(signals.Points)], signals.Points, w.ds.base.funding, config, strategy.EntryMetadata(signals, w.baseline), w.baseline.Risk, signals.RiskATR)
			if e != nil {
				t.Fatal(e)
			}
			summary := scenario["fixed"].(map[string]any)
			if !reflect.DeepEqual(summary["metrics"], report.Metrics) || report.Metrics.Trades == 0 {
				t.Fatal("fixed account differs from original ledger or fixture made no trades")
			}
			if report.Metrics.Funding == 0 {
				t.Fatal("fixed fixture did not exercise funding")
			}
			crossed := false
			for _, trade := range report.Trades {
				if trade.EntryTime < w.outer[1].start && trade.ExitTime > w.outer[1].start {
					crossed = true
				}
			}
			if !crossed {
				t.Fatal("fixed fixture did not carry a position across the outer boundary")
			}
			windows := summary["windows"].([]backtest.WindowMetrics)
			for j, window := range w.outer {
				actual, e := backtest.SummarizeWindow(report, window.start, window.end)
				if e != nil || !reflect.DeepEqual(windows[j], actual) {
					t.Fatal("outer summary did not preserve continuous equity")
				}
			}
			if i > 0 && report.Metrics.NetReturnPct > previous.NetReturnPct {
				t.Fatal("higher transaction costs improved identical fixed signal account")
			}
			previous = report.Metrics
		}
		if a["decision"].(map[string]any)["status"] != "fixed_evaluation_only" {
			t.Fatal("fixed retrospective account claimed adoption")
		}
	}
}

func TestFixedContinuousRejectsInvalidContract(t *testing.T) {
	p, _ := fixedFixture(t)
	raw, _ := json.Marshal(p)
	for _, change := range []func(*walkPlan){
		func(q *walkPlan) { q.Budget = 0 },
		func(q *walkPlan) { q.Budget = 4 },
		func(q *walkPlan) { q.Candidates = []walkCandidate{{"unexpected", q.Baseline}} },
		func(q *walkPlan) { q.CostMultipliers[1] = 1 },
		func(q *walkPlan) { q.Outer[1].Start = q.Outer[0].Start },
		func(q *walkPlan) { q.Outer[0].Inner[1].End = q.Outer[0].End },
		func(q *walkPlan) { q.HistoryStart = q.Outer[0].Start },
		func(q *walkPlan) { q.ExposureFile = "" },
		func(q *walkPlan) { q.DataUse = "independent" },
		func(q *walkPlan) {
			c, _ := strategy.DecodeConfig(q.Baseline)
			c.SchemaVersion = 4
			q.Baseline, _ = json.Marshal(c)
		},
	} {
		var q walkPlan
		json.Unmarshal(raw, &q)
		change(&q)
		if _, _, _, e := fixedNormalize(q); e == nil {
			t.Fatal("invalid fixed contract accepted")
		}
	}
	for _, bad := range []string{
		strings.Replace(string(raw), `"budget":3`, `"budget":3,"budget":3`, 1),
		strings.Replace(string(raw), `"candidates":[],`, "", 1),
		strings.Replace(string(raw), `"risk":`, `"unknown_risk":`, 1),
	} {
		var q walkPlan
		if e := optimizeDecode([]byte(bad), &q); e == nil {
			if _, _, _, e := fixedNormalize(q); e == nil {
				t.Fatal("duplicate, incomplete or unknown contract accepted")
			}
		}
	}
}

// Only disposable synthetic OHLC/D1/funding files are loaded; no cached market
// dataset or prior study is evaluated by these tests.
func fixedDiskFixture(t *testing.T) walkPlan {
	t.Helper()
	p, w := fixedFixture(t)
	dir := t.TempDir()
	p.DatasetDir = dir
	first := w.ds.base.rows[0].OpenTime - 86400000
	rows := make([]market.Candle, 24)
	for i := range rows {
		at := first + int64(i)*3600000
		rows[i] = market.Candle{OpenTime: at, CloseTime: at + 3600000 - 1, Open: 100, High: 101, Low: 99, Close: 100, Volume: 100}
	}
	rows = append(rows, w.ds.base.rows...)
	daily := []market.Candle{}
	for i := 0; i < len(rows); i += 24 {
		v := rows[i]
		v.CloseTime = v.OpenTime + 86400000 - 1
		v.Volume = 0
		for _, r := range rows[i : i+24] {
			v.High, v.Low, v.Close = math.Max(v.High, r.High), math.Min(v.Low, r.Low), r.Close
			v.Volume += r.Volume
		}
		daily = append(daily, v)
	}
	q, e := market.ValidateCandles(rows, "1h")
	if e != nil {
		t.Fatal(e)
	}
	d, e := market.ValidateCandles(daily, "1d")
	if e != nil {
		t.Fatal(e)
	}
	for path, values := range map[string][]market.Candle{"1h.csv": rows, "1d.csv": daily} {
		if e := market.SaveCandles(filepath.Join(dir, path), values); e != nil {
			t.Fatal(e)
		}
	}
	if e := market.SaveFunding(filepath.Join(dir, "funding.json"), w.ds.base.funding); e != nil {
		t.Fatal(e)
	}
	meta := datasetMetadata{Source: "https://fapi.binance.com (USD-M perpetual REST)", Symbol: "ETHUSDT", Candles: map[string]market.Quality{"1h": q, "1d": d}, FundingRows: len(w.ds.base.funding)}
	if e := writeJSON(filepath.Join(dir, "metadata.json"), meta); e != nil {
		t.Fatal(e)
	}
	c, _ := strategy.DecodeConfig(p.Baseline)
	c.Daily.HistoryStart = iso(first)
	p.Baseline, _ = json.Marshal(c)
	p.ExposureFile = filepath.Join(dir, "exposure.json")
	m := exposureManifest{1, "ETHUSDT", iso(d.End), []exposureRange{{iso(d.Start), iso(d.End), "exposed_retrospective", "artificial engineering fixture"}}, false}
	if e := writeJSON(p.ExposureFile, m); e != nil {
		t.Fatal(e)
	}
	return p
}

func TestFixedContinuousDiskIdentityExposureAndWarmup(t *testing.T) {
	p := fixedDiskFixture(t)
	w, e := fixedLoad(p)
	if e != nil {
		t.Fatal(e)
	}
	ready := fixedReady(w)
	for _, field := range []string{"plan_identity", "config_identity", "data_identity", "daily_identity", "exposure_identity"} {
		if len(ready[field].(string)) != 64 {
			t.Fatalf("missing exact %s", field)
		}
	}
	result, e := fixedEvaluate(w)
	if e != nil {
		t.Fatal(e)
	}
	for _, field := range []string{"plan", "config", "plan_identity", "config_identity", "data_identity", "daily_identity", "exposure_identity"} {
		if !reflect.DeepEqual(ready[field], result[field]) {
			t.Fatalf("execution changed inspected %s", field)
		}
	}
	p.DataUse = "unused-final"
	if _, e := fixedLoad(p); e == nil {
		t.Fatal("fixed path erased known data exposure")
	}
	p.DataUse = "retrospective"
	c, _ := strategy.DecodeConfig(p.Baseline)
	c.Daily.HistoryStart = p.HistoryStart
	p.Baseline, _ = json.Marshal(c)
	if _, e := fixedLoad(p); e == nil {
		t.Fatal("fixed path bypassed D1 initialization")
	}
	c.Daily.HistoryStart = iso(w.ds.daily[0].OpenTime)
	c.Classic.UseEMAFilter = true
	c.Classic.EMAPeriod = 500
	p.Baseline, _ = json.Marshal(c)
	if _, e := fixedLoad(p); e == nil {
		t.Fatal("fixed path bypassed feature/classifier warmup")
	}
	c.Classic.UseEMAFilter = false
	c.Classic.EMAPeriod = strategy.DefaultConfig().Classic.EMAPeriod
	p.Baseline, _ = json.Marshal(c)
	if e := os.WriteFile(filepath.Join(p.DatasetDir, "1d.csv"), []byte("invalid\n"), 0600); e != nil {
		t.Fatal(e)
	}
	if _, e := fixedLoad(p); e == nil {
		t.Fatal("fixed path bypassed D1 integrity")
	}
}

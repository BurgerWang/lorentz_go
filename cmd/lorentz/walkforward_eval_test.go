package main

import (
	"encoding/json"
	"lorentzgo/backtest"
	"lorentzgo/market"
	"lorentzgo/strategy"
	"math"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

func walkFixture(t *testing.T) (walkPlan, *walkDataset) {
	ds := enhancedFixture(t)
	c := strategy.DefaultConfig()
	c.Classic.UseVolatilityFilter = false
	c.Classic.UseRegimeFilter = false
	c.Classic.UseKernelFilter = false
	raw, _ := json.Marshal(c)
	start := ds.base.rows[0].OpenTime
	step := int64(3600000)
	at := func(i int) string { return iso(start + int64(i)*step) }
	p := walkPlan{SchemaVersion: 1, DatasetDir: ds.base.plan.DatasetDir, Interval: "1h", HistoryStart: at(0), FeeBPS: 5, SlippageBPS: 2, Baseline: raw, Candidates: []walkCandidate{{"same-control", raw}}, Outer: []walkOuter{
		{"o1", at(400), at(500), []OptimizeFold{{"i1", at(250), at(325)}, {"i2", at(325), at(400)}}},
		{"o2", at(500), at(600), []OptimizeFold{{"i1", at(300), at(400)}, {"i2", at(400), at(500)}}},
	}, Selection: walkSelection{MinTrades: 30, MinPositiveWindows: 1, MaxDrawdownPct: 30, RequireNetImprovement: true, MaxDrawdownIncreasePct: 0}, CostMultipliers: []float64{1, 1.5, 2}, Budget: 18, DataUse: "retrospective", ExposureFile: "../../../tuning/validation/exposure.json"}
	norm, base, n, e := walkNormalize(p)
	if e != nil {
		t.Fatal(e)
	}
	windows := []optimizeWindow{}
	for _, f := range base.Folds {
		a, _ := optimizeDate(f.Start)
		b, _ := optimizeDate(f.End)
		windows = append(windows, optimizeWindow{a, b})
	}
	ds.base.plan, ds.base.windows = base, windows
	w, e := walkPrepare(norm, ds, n, "synthetic")
	if e != nil {
		t.Fatal(e)
	}
	return norm, w
}
func TestWalkforwardNestedContinuousReplay(t *testing.T) {
	_, w := walkFixture(t)
	a, e := w.evaluate()
	if e != nil {
		t.Fatal(e)
	}
	b, e := w.evaluate()
	if e != nil {
		t.Fatal(e)
	}
	if !reflect.DeepEqual(a, b) {
		t.Fatal("nested replay differs")
	}
	if a["evaluations_used"].(int) != 18 {
		t.Fatal("budget accounting")
	}
	for _, s := range a["selections"].([]map[string]any) {
		if s["selected"] != "baseline" {
			t.Fatal("equal or sparse candidate must fall back")
		}
	}
	actual := a["continuous_scenarios"].([]map[string]any)[0]["baseline"].(map[string]any)["metrics"]
	r, e := w.runTo(w.outer[1].end, w.baseline)
	if e != nil {
		t.Fatal(e)
	}
	full, e := w.ledger(r, w.baseline, w.outer[0].start, w.outer[1].end, 1, nil)
	if e != nil {
		t.Fatal(e)
	}
	if !reflect.DeepEqual(actual, full.Metrics) {
		t.Fatal("stitched control is not continuous ledger")
	}
	scenarios := a["continuous_scenarios"].([]map[string]any)
	for i := 1; i < len(scenarios); i++ {
		m := scenarios[i]["baseline"].(map[string]any)["metrics"].(backtest.Metrics)
		if m.NetReturnPct > full.Metrics.NetReturnPct {
			t.Fatal("higher fixed costs improve fixed signal account")
		}
	}
	if a["decision"].(map[string]any)["status"] == "meets_adoption_criteria" {
		t.Fatal("exposed data claimed adoption")
	}
	// Future prices may change later choices/results, never the first frozen choice or inner metrics.
	for i := 500; i < len(w.ds.base.rows); i++ {
		w.ds.base.rows[i].Open *= 1.1
		w.ds.base.rows[i].High *= 1.1
		w.ds.base.rows[i].Low *= 1.1
		w.ds.base.rows[i].Close *= 1.1
	}
	changed, e := w.evaluate()
	if e != nil {
		t.Fatal(e)
	}
	if !reflect.DeepEqual(a["selections"].([]map[string]any)[0], changed["selections"].([]map[string]any)[0]) {
		t.Fatal("future altered earlier selection")
	}
}
func TestWalkforwardRejectsLeaksBudgetsAndInactiveChanges(t *testing.T) {
	p, _ := walkFixture(t)
	raw, _ := json.Marshal(p)
	for _, change := range []func(*walkPlan){
		func(q *walkPlan) { q.Budget = 17 }, func(q *walkPlan) { q.DataUse = "independent" },
		func(q *walkPlan) { q.Outer[0].Inner[1].End = q.Outer[0].End },
		func(q *walkPlan) { q.CostMultipliers[2] = 1 }, func(q *walkPlan) { q.Selection.MinTrades = 29 },
		func(q *walkPlan) { q.Candidates[0].Name = "baseline" },
		func(q *walkPlan) {
			c, _ := strategy.DecodeConfig(q.Candidates[0].Config)
			c.Risk.Enabled = true
			q.Candidates[0].Config, _ = json.Marshal(c)
		},
	} {
		var q walkPlan
		json.Unmarshal(raw, &q)
		change(&q)
		if _, _, _, e := walkNormalize(q); e == nil {
			t.Fatal("bad nested plan accepted")
		}
	}
	for _, bad := range []string{strings.Replace(string(raw), `"budget":18`, `"budget":18,"budget":18`, 1), strings.Replace(string(raw), `"cost_multipliers":[1,1.5,2],`, "", 1)} {
		var q walkPlan
		if e := optimizeDecode([]byte(bad), &q); e == nil {
			t.Fatal("non-complete nested JSON accepted")
		}
	}
}
func TestWalkforwardExposureCannotRelabel(t *testing.T) {
	p, _ := walkFixture(t)
	m := exposureManifest{1, "ETHUSDT", "2025-01-01T00:00:00Z", []exposureRange{{"2020-01-01T00:00:00Z", "2025-01-01T00:00:00Z", "development_selection", "test exposed"}}, false}
	raw, _ := json.Marshal(m)
	if e := walkExposure(raw, p); e != nil {
		t.Fatal(e)
	}
	p.DataUse = "unused-final"
	if e := walkExposure(raw, p); e == nil {
		t.Fatal("exposed manifest renamed final")
	}
	m.UnusedFinalDataAvailable = true
	raw, _ = json.Marshal(m)
	if e := walkExposure(raw, p); e == nil {
		t.Fatal("available flag bypasses exposed range")
	}
	m.Ranges[0].Use = "unused_final"
	raw, _ = json.Marshal(m)
	if e := walkExposure(raw, p); e == nil {
		t.Fatal("replacement manifest erased known 2024 exposure")
	}
	for i := range p.Outer {
		p.Outer[i].Start = "2026-10-01T00:00:00Z"
		p.Outer[i].End = "2026-11-01T00:00:00Z"
	}
	m.KnownCacheEnd = "2026-11-01T00:00:00Z"
	m.Ranges[0].End = m.KnownCacheEnd
	raw, _ = json.Marshal(m)
	if e := walkExposure(raw, p); e != nil {
		t.Fatal(e)
	}
}
func TestWalkforwardEntryKindSurvivesConfigurationSwitch(t *testing.T) {
	_, w := walkFixture(t)
	r, e := w.runTo(w.outer[1].end, w.baseline)
	if e != nil {
		t.Fatal(e)
	}
	metadata := strategy.EntryMetadata(r, w.baseline)
	for i := range metadata {
		if metadata[i].Kind != "" {
			metadata[i].Kind = "pullback"
		}
	}
	report, e := w.ledger(r, w.baseline, w.outer[0].start, w.outer[1].end, 1, metadata)
	if e != nil {
		t.Fatal(e)
	}
	if len(report.Trades) == 0 {
		t.Fatal("no transaction exercised")
	}
	for _, trade := range report.Trades {
		if trade.EntryKind != "pullback" {
			t.Fatal("selected kind lost to baseline mode")
		}
	}
}

// Actual Go CLI and Python controller use complete disposable OHLC/D1/funding
// files. This is an artificial engineering fixture, never a market study.
func TestWalkforwardActualGoPythonProtocol(t *testing.T) {
	p, w := walkFixture(t)
	dir := t.TempDir()
	p.DatasetDir = dir
	rows := make([]market.Candle, 24)
	first := w.ds.base.rows[0].OpenTime - 86400000
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
			v.High = math.Max(v.High, r.High)
			v.Low = math.Min(v.Low, r.Low)
			v.Close = r.Close
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
	for path, items := range map[string][]market.Candle{"1h.csv": rows, "1d.csv": daily} {
		if e := market.SaveCandles(filepath.Join(dir, path), items); e != nil {
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
	p.Candidates[0].Config = p.Baseline
	p.ExposureFile = filepath.Join(dir, "exposure.json")
	manifest := exposureManifest{1, "ETHUSDT", iso(d.End), []exposureRange{{iso(d.Start), iso(d.End), "development_selection", "artificial test fixture only"}}, false}
	if e := writeJSON(p.ExposureFile, manifest); e != nil {
		t.Fatal(e)
	}
	plan := filepath.Join(dir, "plan.json")
	if e := writeJSON(plan, p); e != nil {
		t.Fatal(e)
	}
	root, e := filepath.Abs("../..")
	if e != nil {
		t.Fatal(e)
	}
	binary := filepath.Join(dir, "lorentz")
	build := exec.Command("go", "build", "-buildvcs=false", "-o", binary, "./cmd/lorentz")
	build.Dir = root
	if output, e := build.CombinedOutput(); e != nil {
		t.Fatalf("fixture build: %s %v", output, e)
	}
	out := filepath.Join(dir, "run")
	command := func() *exec.Cmd {
		r := exec.Command(filepath.Join(root, ".venv/bin/python"), "tuning/walkforward.py", "--plan", plan, "--binary", binary, "--out", out, "--budget", "18", "--timeout", "30")
		r.Dir = root
		return r
	}
	if output, e := command().CombinedOutput(); e != nil {
		t.Fatalf("Go/Python: %s %v", output, e)
	}
	before, e := os.ReadFile(filepath.Join(out, "summary.json"))
	if e != nil {
		t.Fatal(e)
	}
	if output, e := command().CombinedOutput(); e != nil {
		t.Fatalf("resume: %s %v", output, e)
	}
	after, _ := os.ReadFile(filepath.Join(out, "summary.json"))
	if string(before) != string(after) {
		t.Fatal("completed actual batch reran")
	}
	var state struct {
		Status string
		Result map[string]any
	}
	if e := json.Unmarshal(before, &state); e != nil {
		t.Fatal(e)
	}
	if state.Status != "COMPLETE" || state.Result["evaluations_used"] != float64(18) {
		t.Fatal(string(before))
	}
}
func TestWalkforwardPredeclaredDecisionTradeoffs(t *testing.T) {
	s := walkSelection{MinTrades: 30, MinPositiveWindows: 2, MinNetReturnPct: 0, MinWinRatePct: 40, MaxDrawdownPct: 20, RequireNetImprovement: true, RequireWinRateImprovement: true, MaxDrawdownIncreasePct: 2}
	if reasons := walkReasons(s, 10, 55, 12, 50, 2, 5, 50, 10); len(reasons) != 0 {
		t.Fatal(reasons)
	}
	if reasons := walkReasons(s, 10, 55, 13, 50, 2, 5, 50, 10); !reflect.DeepEqual(reasons, []string{"drawdown_tradeoff_exceeded"}) {
		t.Fatal(reasons)
	}
	if reasons := walkReasons(s, 0, 35, 21, 29, 1, 5, 50, 10); len(reasons) != 8 {
		t.Fatal(reasons)
	}
}

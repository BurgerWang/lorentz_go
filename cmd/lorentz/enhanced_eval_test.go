package main

import (
	"bytes"
	"encoding/json"
	"lorentzgo/indicator"
	"lorentzgo/market"
	"lorentzgo/strategy"
	"reflect"
	"strings"
	"testing"
)

func enhancedFixture(t *testing.T) *enhancedDataset {
	p, rows, funding := optimizeFixture(t)
	base, e := optimizePrepare(p, rows, funding)
	if e != nil {
		t.Fatal(e)
	}
	start, _ := optimizeDate("2020-01-01")
	daily := make([]market.Candle, 1490)
	for i := range daily {
		v := 100 + float64(i)/100
		at := start + int64(i)*86400000
		daily[i] = market.Candle{OpenTime: at, CloseTime: at + 86400000 - 1, Open: v, High: v + 1, Low: v - 1, Close: v}
	}
	return &enhancedDataset{base: base, daily: daily}
}
func TestEnhancedGoIntegrationAndRecovery(t *testing.T) {
	ds := enhancedFixture(t)
	c := strategy.DefaultConfig()
	c.Classic.UseVolatilityFilter = false
	c.Classic.UseRegimeFilter = false
	c.Classic.UseKernelFilter = false
	r, e := ds.evaluate(c)
	if e != nil {
		t.Fatal(e)
	}
	folds, a, e := ds.base.evaluate(c.Classic)
	if e != nil {
		t.Fatal(e)
	}
	if !reflect.DeepEqual(folds, r["folds"]) || a != r["aggregate"] {
		t.Fatal("disabled ledger changed")
	}
	c.Daily.Enabled = true
	c.EnvelopeEnabled = true
	raw, _ := json.Marshal(map[string]any{"id": 7, "config": c})
	var out bytes.Buffer
	if e := enhancedServe(ds, strings.NewReader("{}\n"+string(raw)+"\n"+string(raw)+"\n"), &out); e != nil {
		t.Fatal(e)
	}
	lines := strings.Split(strings.TrimSpace(out.String()), "\n")
	if len(lines) != 4 {
		t.Fatal(out.String())
	}
	if !strings.Contains(lines[1], `"type":"error"`) || !strings.Contains(lines[2], `"type":"result"`) {
		t.Fatal(out.String())
	}
	var x, y map[string]any
	json.Unmarshal([]byte(lines[2]), &x)
	json.Unmarshal([]byte(lines[3]), &y)
	delete(x, "elapsed_seconds")
	delete(x, "timing")
	delete(y, "elapsed_seconds")
	delete(y, "timing")
	if !reflect.DeepEqual(x, y) {
		t.Fatal("exported config failed replay")
	}
}

func TestEnhancedPullbackEndToEnd(t *testing.T) {
	ds := enhancedFixture(t)
	c := strategy.DefaultConfig()
	c.Classic.UseVolatilityFilter = false
	c.Classic.UseRegimeFilter = false
	c.Classic.UseKernelFilter = false
	c.Classic.MaxBarsBack = 2000
	c.Classic.Neighbors = 8
	c.Classic.IncludeFullHistory = false
	c.Classifier = strategy.ClassicClassifier(c.Classic)
	c.EnvelopeEnabled = true
	c.Envelope.X = 0
	c.Envelope.ATRLength = 2
	c.Pullback.Enabled = true
	c.Pullback.MaxWaitBars = 96
	c.EntryMode = "both"
	r, e := ds.evaluate(c)
	if e != nil {
		t.Fatal(e)
	}
	counts := r["executed_entries"].(map[string]int)
	if counts["pullback"] == 0 {
		t.Fatalf("no executed pullback: events=%v reasons=%v counts=%v", r["event_counts"], r["event_reasons"], counts)
	}
	raw, _ := json.Marshal(c)
	round, e := strategy.DecodeConfig(raw)
	if e != nil {
		t.Fatal(e)
	}
	again, e := ds.evaluate(round)
	if e != nil {
		t.Fatal(e)
	}
	delete(r, "elapsed_seconds")
	delete(r, "timing")
	delete(again, "elapsed_seconds")
	delete(again, "timing")
	if !reflect.DeepEqual(r, again) {
		t.Fatal("M2 replay differs")
	}
	old := ds.base.rows
	ds.base.rows = old[:400]
	base := *ds.base
	ds.base = &base
	ds.base.windows = ds.base.windows[:1]
	ds.base.plan.Folds = ds.base.plan.Folds[:1]
	prefix, e := ds.evaluate(c)
	if e != nil {
		t.Fatal(e)
	}
	if !reflect.DeepEqual(prefix["folds"].([]optimizeFoldResult)[0], r["folds"].([]optimizeFoldResult)[0]) {
		t.Fatal("future changed M2 first fold")
	}
}

func TestExtendedFeatureGoContract(t *testing.T) {
	ds := enhancedFixture(t)
	c := strategy.DefaultConfig()
	c.Classifier.FeatureGroup.Name = "classic-rvol"
	c.Classifier.FeatureGroup.Features = append(c.Classifier.FeatureGroup.Features, indicator.FeatureSpec{Name: "RVOL", A: 14, B: 1, Normalization: "rolling-z", Window: 20})
	raw, e := json.Marshal(c)
	if e != nil {
		t.Fatal(e)
	}
	round, e := strategy.DecodeConfig(raw)
	if e != nil {
		t.Fatal(e)
	}
	result, e := ds.evaluate(round)
	if e != nil {
		t.Fatal(e)
	}
	again, e := ds.evaluate(round)
	if e != nil {
		t.Fatal(e)
	}
	delete(result, "elapsed_seconds")
	delete(result, "timing")
	delete(again, "elapsed_seconds")
	delete(again, "timing")
	if !reflect.DeepEqual(result, again) {
		t.Fatal("extended config replay differs")
	}
	c.Classifier.FeatureGroup.Features = append(c.Classifier.FeatureGroup.Features, c.Classifier.FeatureGroup.Features[0])
	if e := c.Validate(); e == nil {
		t.Fatal("duplicate accepted")
	}
}

func TestRiskEnhancedGoIntegration(t *testing.T) {
	ds := enhancedFixture(t)
	c := strategy.DefaultConfig()
	c.Classic.UseVolatilityFilter = false
	c.Classic.UseRegimeFilter = false
	c.Classic.UseKernelFilter = false
	old, e := ds.evaluate(c)
	if e != nil {
		t.Fatal(e)
	}
	c.SchemaVersion = 4
	off, e := ds.evaluate(c)
	if e != nil {
		t.Fatal(e)
	}
	if !reflect.DeepEqual(old["folds"], off["folds"]) {
		t.Fatal("risk-off changed baseline")
	}
	c.Risk.Enabled = true
	c.Risk.ATRPeriod = 2
	c.Risk.ATRMultiplier = .0001
	c.Risk.BreakevenR = .0001
	r, e := ds.evaluate(c)
	if e != nil {
		t.Fatal(e)
	}
	if r["exit_reasons"].(map[string]int)["risk_exit"] == 0 {
		t.Fatalf("no confirmed risk exit exercised: exits=%v entries=%v events=%v", r["exit_reasons"], r["executed_entries"], r["event_counts"])
	}
	raw, _ := json.Marshal(c)
	cfg, e := strategy.DecodeConfig(raw)
	if e != nil {
		t.Fatal(e)
	}
	again, e := ds.evaluate(cfg)
	if e != nil {
		t.Fatal(e)
	}
	delete(r, "elapsed_seconds")
	delete(r, "timing")
	delete(again, "elapsed_seconds")
	delete(again, "timing")
	if !reflect.DeepEqual(r, again) {
		t.Fatal("risk config replay differs")
	}
	c.Risk.ATRPeriod = 300
	if _, e := ds.evaluate(c); e == nil {
		t.Fatal("uninitialized risk ATR accepted")
	}
}

func TestAlignedModelEnhancedContract(t *testing.T) {
	ds := enhancedFixture(t)
	c := strategy.DefaultConfig()
	c.Classic.Algorithm = "aligned-knn"
	c.Classic.IncludeFullHistory = false
	c.Classifier = strategy.ClassicClassifier(c.Classic)
	c.Classifier.Family = "aligned-extended"
	original, e := indicator.Run(ds.base.rows, c.Classic)
	if e != nil {
		t.Fatal(e)
	}
	r, e := strategy.Run(ds.base.rows, nil, c)
	if e != nil {
		t.Fatal(e)
	}
	if !reflect.DeepEqual(original, r.Points) {
		t.Fatal("integrated aligned control changes points")
	}
	control, e := ds.evaluate(c)
	if e != nil {
		t.Fatal(e)
	}
	if control["model_score_summary"].(map[string]any)["ready_bars"].(int) == 0 {
		t.Fatal("score absent")
	}
	c.Classifier.FeatureGroup.Name = "classic-rvol"
	c.Classifier.FeatureGroup.Features = append(c.Classifier.FeatureGroup.Features, indicator.FeatureSpec{Name: "RVOL", A: 14, B: 1, Normalization: "rolling-z", Window: 20})
	c.Model.VoteWeight = "inverse"
	c.Model.VoteHalfLife = 100
	c.Classic.SampleStride = 3
	c.Classic.MinVoteFraction = .2
	raw, _ := json.Marshal(c)
	decoded, e := strategy.DecodeConfig(raw)
	if e != nil {
		t.Fatal(e)
	}
	x, e := ds.evaluate(decoded)
	if e != nil {
		t.Fatal(e)
	}
	y, e := ds.evaluate(decoded)
	if e != nil {
		t.Fatal(e)
	}
	delete(x, "elapsed_seconds")
	delete(x, "timing")
	delete(y, "elapsed_seconds")
	delete(y, "timing")
	if !reflect.DeepEqual(x, y) {
		t.Fatal("model candidate replay differs")
	}
	c.Classifier.Family = "classic-extended"
	if e := c.Validate(); e == nil {
		t.Fatal("algorithm/family mismatch accepted")
	}
	c.Classifier.Family = "aligned-extended"
	c.SchemaVersion = 4
	if e := c.Validate(); e == nil {
		t.Fatal("aligned model in old schema")
	}
}

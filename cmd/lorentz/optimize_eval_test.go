package main

import (
	"bytes"
	"encoding/json"
	"math"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"lorentzgo/backtest"
	"lorentzgo/indicator"
	"lorentzgo/market"
)

func optimizeFixture(t *testing.T) (OptimizePlan, []market.Candle, []market.Funding) {
	t.Helper()
	const step = int64(3600000)
	start, _ := optimizeDate("2024-01-01")
	rows := make([]market.Candle, 600)
	for i := range rows {
		price := 100 + 8*math.Sin(float64(i)/11) + float64(i)/60
		ot := start + int64(i)*step
		rows[i] = market.Candle{OpenTime: ot, CloseTime: ot + step - 1, Open: price, High: price + 1, Low: price - 1, Close: price + .2*math.Cos(float64(i)), Volume: 100}
	}
	funding := []market.Funding{}
	for at := start; at <= start+600*step; at += 8 * step {
		funding = append(funding, market.Funding{Time: at, Rate: .0001, MarkPrice: 100})
	}
	p := OptimizePlan{SchemaVersion: 1, DatasetDir: t.TempDir(), Interval: "1h", HistoryStart: iso(start), Folds: []OptimizeFold{{"a", iso(start + 250*step), iso(start + 400*step)}, {"b", iso(start + 400*step), iso(start + 600*step)}}, FeeBPS: 5, SlippageBPS: 2, MinTrades: 30, MinPositiveFolds: 2}
	return p, rows, funding
}
func TestOptimizePlanRejectsBadWindowsAndData(t *testing.T) {
	p, rows, funding := optimizeFixture(t)
	for _, change := range []func(*OptimizePlan){
		func(p *OptimizePlan) { p.Folds[1].Start = p.Folds[0].Start },
		func(p *OptimizePlan) { p.Folds[1].Start = iso(rows[401].OpenTime) },
		func(p *OptimizePlan) { p.HistoryStart = p.Folds[0].Start },
		func(p *OptimizePlan) { p.Folds[0].Start = "2024-01-11T10:00:01Z" },
		func(p *OptimizePlan) { p.Folds[0].Start = "2024-01-11T10:00:00+01:00" },
		func(p *OptimizePlan) { p.MinTrades = 29 },
		func(p *OptimizePlan) { p.MinPositiveFolds = 3 },
		func(p *OptimizePlan) { p.FeeBPS = -1 },
		func(p *OptimizePlan) { p.HistoryStart = iso(rows[100].OpenTime) },
	} {
		bad := p
		bad.Folds = append([]OptimizeFold(nil), p.Folds...)
		change(&bad)
		if _, e := optimizePrepare(bad, rows, funding); e == nil {
			t.Fatalf("accepted invalid plan %+v", bad)
		}
	}
	if _, e := optimizePrepare(p, rows[:599], funding); e == nil {
		t.Fatal("accepted missing final candle")
	}
	if _, e := optimizePrepare(p, rows, nil); e == nil {
		t.Fatal("accepted missing funding")
	}
	gap := append([]market.Funding(nil), funding...)
	gap = append(gap[:40], gap[42:]...)
	if _, e := optimizePrepare(p, rows, gap); e == nil {
		t.Fatal("accepted funding gap")
	}
	raw, _ := json.Marshal(p)
	for _, bad := range []string{string(raw) + " {}", strings.Replace(string(raw), `"min_trades":30,`, "", 1), strings.Replace(string(raw), `"schema_version":1`, `"schema_version":1,"unknown":1`, 1)} {
		var q OptimizePlan
		if e := optimizeDecode([]byte(bad), &q); e == nil {
			t.Fatal("accepted incomplete or extra plan JSON")
		}
	}
}
func TestOptimizeContinuousCausalFolds(t *testing.T) {
	p, rows, funding := optimizeFixture(t)
	ds, e := optimizePrepare(p, rows, funding)
	if e != nil {
		t.Fatal(e)
	}
	c := optimizeDefaultConfig()
	c.MaxBarsBack = 2000
	c.UseVolatilityFilter = false
	c.UseRegimeFilter = false
	c.UseKernelFilter = false
	folds, a, e := ds.evaluate(c)
	if e != nil {
		t.Fatal(e)
	}
	points, e := indicator.Run(rows, c)
	if e != nil {
		t.Fatal(e)
	}
	for i, w := range ds.windows {
		stop := 400
		if i == 1 {
			stop = 600
		}
		b := backtest.DefaultConfig()
		b.StartTime = w.start
		b.EndTime = w.end
		b.FeeBPS = p.FeeBPS
		b.SlippageBPS = p.SlippageBPS
		r, e := backtest.Evaluate(rows[:stop], points[:stop], funding, b)
		if e != nil {
			t.Fatal(e)
		}
		if !reflect.DeepEqual(r.Metrics, folds[i].Metrics) {
			t.Fatal("fold ledger differs from continuous prefix reference")
		}
	}
	changed := append([]market.Candle(nil), rows...)
	for i := 400; i < len(changed); i++ {
		changed[i].Open *= 2
		changed[i].High *= 2
		changed[i].Low *= 2
		changed[i].Close *= 2
	}
	other, e := optimizePrepare(p, changed, funding)
	if e != nil {
		t.Fatal(e)
	}
	future, _, e := other.evaluate(c)
	if e != nil {
		t.Fatal(e)
	}
	if !reflect.DeepEqual(folds[0], future[0]) {
		t.Fatal("future candles changed prior fold")
	}
	if a.Trades == 0 {
		t.Fatal("fixture failed to exercise executions")
	}
	bad := c
	bad.Algorithm = "aligned-knn"
	if e := ds.validateConfig(bad); e == nil {
		t.Fatal("accepted nonoriginal algorithm")
	}
	bad = c
	bad.SampleStride = 1
	if e := ds.validateConfig(bad); e == nil {
		t.Fatal("accepted ignored stride")
	}
	bad = c
	bad.UseEMAFilter = true
	bad.EMAPeriod = 300
	if e := ds.validateConfig(bad); e == nil {
		t.Fatal("accepted uninitialized EMA")
	}
}
func TestOptimizeAggregate(t *testing.T) {
	a := optimizeSummarize([]optimizeFoldResult{{Metrics: backtest.Metrics{Trades: 10, Wins: 6, NetReturnPct: 10, MaxDrawdownPct: 4}}, {Metrics: backtest.Metrics{Trades: 30, Wins: 9, NetReturnPct: -5, MaxDrawdownPct: 7}}})
	if math.Abs(a.NetReturnPct-4.5) > 1e-10 || a.WinRatePct != 37.5 || a.Trades != 40 || a.Wins != 15 || a.PositiveFolds != 1 || a.WorstFoldNetReturnPct != -5 || a.MaxFoldDrawdownPct != 7 {
		t.Fatalf("wrong aggregate %+v", a)
	}
}
func TestOptimizeProtocolErrorRecovery(t *testing.T) {
	p, rows, funding := optimizeFixture(t)
	ds, e := optimizePrepare(p, rows, funding)
	if e != nil {
		t.Fatal(e)
	}
	c := optimizeDefaultConfig()
	c.MaxBarsBack = 2000
	c.UseVolatilityFilter = false
	c.UseRegimeFilter = false
	c.UseKernelFilter = false
	good, _ := json.Marshal(map[string]any{"id": 4, "config": c})
	bad := c
	bad.Algorithm = "original-chart"
	invalid, _ := json.Marshal(map[string]any{"id": 3, "config": bad})
	var out bytes.Buffer
	in := strings.NewReader("{broken}\n" + `{"id":2,"config":{}}` + "\n" + string(invalid) + "\n" + string(good) + "\n")
	if e := optimizeServe(ds, in, &out); e != nil {
		t.Fatal(e)
	}
	lines := strings.Split(strings.TrimSpace(out.String()), "\n")
	if len(lines) != 5 {
		t.Fatalf("expected ready plus four responses: %s", out.String())
	}
	want := []string{"ready", "error", "error", "error", "result"}
	kinds := []string{"", "request", "config", "config", ""}
	for i, line := range lines {
		var msg map[string]any
		if e := json.Unmarshal([]byte(line), &msg); e != nil {
			t.Fatal(e)
		}
		if msg["type"] != want[i] {
			t.Fatalf("line %d: %s", i, line)
		}
		if kinds[i] != "" && msg["error_kind"] != kinds[i] {
			t.Fatalf("bad error class: %s", line)
		}
	}
}
func TestOptimizeLoadBindsBytesAndRejectsMetadataMismatch(t *testing.T) {
	p, rows, funding := optimizeFixture(t)
	q, e := market.ValidateCandles(rows, p.Interval)
	if e != nil {
		t.Fatal(e)
	}
	meta := datasetMetadata{Source: "https://fapi.binance.com (USD-M perpetual REST)", Symbol: "ETHUSDT", Candles: map[string]market.Quality{"1h": q}, FundingRows: len(funding)}
	if e := market.SaveCandles(filepath.Join(p.DatasetDir, "1h.csv"), rows); e != nil {
		t.Fatal(e)
	}
	if e := market.SaveFunding(filepath.Join(p.DatasetDir, "funding.json"), funding); e != nil {
		t.Fatal(e)
	}
	if e := writeJSON(filepath.Join(p.DatasetDir, "metadata.json"), meta); e != nil {
		t.Fatal(e)
	}
	ds, e := optimizeLoad(p)
	if e != nil {
		t.Fatal(e)
	}
	if len(ds.identity) != 64 {
		t.Fatal("missing sha256 identity")
	}
	// Same valid JSON data with changed bytes must produce a different identity.
	path := filepath.Join(p.DatasetDir, "funding.json")
	raw, e := os.ReadFile(path)
	if e != nil {
		t.Fatal(e)
	}
	if e := os.WriteFile(path, append(raw, ' '), 0600); e != nil {
		t.Fatal(e)
	}
	changed, e := optimizeLoad(p)
	if e != nil {
		t.Fatal(e)
	}
	if ds.identity == changed.identity {
		t.Fatal("identity did not bind actual file bytes")
	}
	meta.FundingRows--
	if e := writeJSON(filepath.Join(p.DatasetDir, "metadata.json"), meta); e != nil {
		t.Fatal(e)
	}
	if _, e := optimizeLoad(p); e == nil {
		t.Fatal("accepted metadata funding count mismatch")
	}
}

func TestOptimizeProtocolEvaluationErrorsRemainRecoverable(t *testing.T) {
	p, rows, funding := optimizeFixture(t)
	ds, e := optimizePrepare(p, rows, funding)
	if e != nil {
		t.Fatal(e)
	}
	// Inject an invalid in-memory record after preparation to exercise the existing
	// ledger failure path; disk loading never bypasses funding validation.
	ds.funding = append([]market.Funding(nil), funding...)
	ds.funding[0].MarkPrice = 0
	c := optimizeDefaultConfig()
	valid, _ := json.Marshal(map[string]any{"id": 9, "config": c})
	var out bytes.Buffer
	if e := optimizeServe(ds, strings.NewReader(string(valid)+"\n"+`{"id":10,"config":{}}`+"\n"), &out); e != nil {
		t.Fatal(e)
	}
	lines := strings.Split(strings.TrimSpace(out.String()), "\n")
	if len(lines) != 3 {
		t.Fatal(out.String())
	}
	for i, kind := range []string{"evaluation", "config"} {
		var msg map[string]any
		if e := json.Unmarshal([]byte(lines[i+1]), &msg); e != nil {
			t.Fatal(e)
		}
		if msg["type"] != "error" || msg["error_kind"] != kind {
			t.Fatalf("unexpected response %s", lines[i+1])
		}
	}
}

func TestOptimizeExtremePeriodsCannotCrashProtocol(t *testing.T) {
	p, rows, funding := optimizeFixture(t)
	ds, e := optimizePrepare(p, rows, funding)
	if e != nil {
		t.Fatal(e)
	}
	large := int(^uint(0) >> 1)
	bad := optimizeDefaultConfig()
	bad.Features[0].A = large
	inactive := optimizeDefaultConfig()
	inactive.SMAPeriod = large
	inactive.FeatureCount = 4
	inactive.Features[4] = indicator.Feature{Name: "CCI", A: large, B: 1}
	var in, out bytes.Buffer
	for i, c := range []indicator.Config{bad, inactive, optimizeDefaultConfig()} {
		json.NewEncoder(&in).Encode(map[string]any{"id": i + 1, "config": c})
	}
	if e := optimizeServe(ds, &in, &out); e != nil {
		t.Fatal(e)
	}
	lines := strings.Split(strings.TrimSpace(out.String()), "\n")
	if len(lines) != 4 {
		t.Fatal(out.String())
	}
	for i, want := range []string{"error", "result", "result"} {
		var msg map[string]any
		if e := json.Unmarshal([]byte(lines[i+1]), &msg); e != nil {
			t.Fatal(e)
		}
		if msg["type"] != want {
			t.Fatal(string(lines[i+1]))
		}
	}
}

func TestOptimizePlanStrictObjectKeys(t *testing.T) {
	p, _, _ := optimizeFixture(t)
	raw, err := json.Marshal(p)
	if err != nil {
		t.Fatal(err)
	}
	var roundTrip OptimizePlan
	if err := optimizeDecode(raw, &roundTrip); err != nil || !reflect.DeepEqual(roundTrip, p) {
		t.Fatalf("valid plan round trip: %+v %v", roundTrip, err)
	}
	for _, tc := range []struct{ name, old, replacement string }{
		{"plan duplicate", `"fee_bps":5`, `"fee_bps":5,"fee_bps":0`},
		{"plan escaped duplicate", `"fee_bps":5`, `"fee_bps":5,"fee_\u0062ps":0`},
		{"plan case alias", `"fee_bps":5`, `"fee_bps":5,"FEE_BPS":0`},
		{"plan renamed", `"fee_bps":5`, `"FEE_BPS":5`},
		{"plan null", `"fee_bps":5`, `"fee_bps":null`},
		{"fold duplicate", `"name":"a"`, `"name":"a","name":"other"`},
		{"fold escaped duplicate", `"name":"a"`, `"name":"a","n\u0061me":"other"`},
		{"fold case alias", `"name":"a"`, `"name":"a","NAME":"other"`},
		{"fold renamed", `"name":"a"`, `"NAME":"a"`},
		{"fold unknown", `"name":"a"`, `"name":"a","extra":0`},
		{"fold missing", `"name":"a",`, ``},
		{"fold null", `"name":"a"`, `"name":null`},
	} {
		t.Run(tc.name, func(t *testing.T) {
			bad := strings.Replace(string(raw), tc.old, tc.replacement, 1)
			if bad == string(raw) {
				t.Fatal("mutation did not apply")
			}
			var q OptimizePlan
			if err := optimizeDecode([]byte(bad), &q); err == nil {
				t.Fatalf("accepted ambiguous plan: %s", bad)
			}
		})
	}
}

func TestOptimizeStrictProtocolErrorsThenValidRequests(t *testing.T) {
	p, rows, funding := optimizeFixture(t)
	ds, err := optimizePrepare(p, rows, funding)
	if err != nil {
		t.Fatal(err)
	}
	c := optimizeDefaultConfig()
	raw, err := json.Marshal(struct {
		ID     int              `json:"id"`
		Config indicator.Config `json:"config"`
	}{7, c})
	if err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct{ name, old, replacement, kind string }{
		{"request duplicate", `"id":7`, `"id":7,"id":8`, "request"},
		{"request escaped duplicate", `"id":7`, `"id":7,"i\u0064":8`, "request"},
		{"request case alias", `"id":7`, `"id":7,"ID":8`, "request"},
		{"request renamed", `"id":7`, `"ID":7`, "request"},
		{"request missing", `"id":7,`, ``, "request"},
		{"request null", `"id":7`, `"id":null`, "request"},
		{"request unknown", `"id":7`, `"id":7,"extra":0`, "request"},
		{"request config duplicate", `"config":`, `"config":{},"config":`, "request"},
		{"config duplicate", `"neighbors":8`, `"neighbors":8,"neighbors":99`, "config"},
		{"config escaped duplicate", `"neighbors":8`, `"neighbors":8,"neighb\u006frs":99`, "config"},
		{"config case alias", `"neighbors":8`, `"neighbors":8,"NEIGHBORS":99`, "config"},
		{"feature duplicate", `"name":"RSI"`, `"name":"RSI","name":"WT"`, "config"},
		{"feature escaped duplicate", `"name":"RSI"`, `"name":"RSI","n\u0061me":"WT"`, "config"},
		{"feature case alias", `"name":"RSI"`, `"name":"RSI","NAME":"WT"`, "config"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			bad := strings.Replace(string(raw), tc.old, tc.replacement, 1)
			if bad == string(raw) {
				t.Fatal("mutation did not apply")
			}
			var out bytes.Buffer
			if err := optimizeServe(ds, strings.NewReader(bad+"\n"+string(raw)+"\n"), &out); err != nil {
				t.Fatal(err)
			}
			lines := strings.Split(strings.TrimSpace(out.String()), "\n")
			if len(lines) != 3 {
				t.Fatal(out.String())
			}
			var failure struct {
				Type      string `json:"type"`
				ErrorKind string `json:"error_kind"`
			}
			if err := json.Unmarshal([]byte(lines[1]), &failure); err != nil || failure.Type != "error" || failure.ErrorKind != tc.kind {
				t.Fatalf("expected %s error: %s (%v)", tc.kind, lines[1], err)
			}
			var success struct {
				Type   string           `json:"type"`
				ID     int              `json:"id"`
				Config indicator.Config `json:"config"`
			}
			if err := json.Unmarshal([]byte(lines[2]), &success); err != nil || success.Type != "result" || success.ID != 7 || success.Config != c {
				t.Fatalf("valid request did not recover: %s (%v)", lines[2], err)
			}
		})
	}
}

func TestOptimizeFundingEstimatedMetadata(t *testing.T) {
	p, rows, funding := optimizeFixture(t)
	q, e := market.ValidateCandles(rows, p.Interval)
	if e != nil {
		t.Fatal(e)
	}
	for i := 0; i < 3; i++ {
		funding[i].MarkPriceSource = market.FundingMarkKlineOpen8h
		funding[i].MarkPriceTime = funding[i].Time
	}
	meta := datasetMetadata{Source: "https://fapi.binance.com (USD-M perpetual REST)", Symbol: "ETHUSDT", Candles: map[string]market.Quality{"1h": q}, FundingRows: len(funding), FundingEstimatedRows: 3}
	if e := market.SaveCandles(filepath.Join(p.DatasetDir, "1h.csv"), rows); e != nil {
		t.Fatal(e)
	}
	if e := market.SaveFunding(filepath.Join(p.DatasetDir, "funding.json"), funding); e != nil {
		t.Fatal(e)
	}
	if e := writeJSON(filepath.Join(p.DatasetDir, "metadata.json"), meta); e != nil {
		t.Fatal(e)
	}
	ds, e := optimizeLoad(p)
	if e != nil {
		t.Fatal(e)
	}
	var output bytes.Buffer
	if e := optimizeServe(ds, strings.NewReader(""), &output); e != nil {
		t.Fatal(e)
	}
	var ready struct {
		Metadata datasetMetadata `json:"metadata"`
	}
	if e := json.Unmarshal(bytes.TrimSpace(output.Bytes()), &ready); e != nil {
		t.Fatal(e)
	}
	if ready.Metadata.FundingEstimatedRows != 3 {
		t.Fatal("ready metadata hides estimated rows")
	}
	for _, n := range []int{0, 2, 4, -1} {
		meta.FundingEstimatedRows = n
		if e := writeJSON(filepath.Join(p.DatasetDir, "metadata.json"), meta); e != nil {
			t.Fatal(e)
		}
		if _, e := optimizeLoad(p); e == nil {
			t.Fatalf("accepted estimated count %d", n)
		}
	}
	// Exact legacy data must also reject a falsely declared estimated row count.
	for i := range funding {
		funding[i].MarkPriceSource = ""
		funding[i].MarkPriceTime = 0
	}
	if e := market.SaveFunding(filepath.Join(p.DatasetDir, "funding.json"), funding); e != nil {
		t.Fatal(e)
	}
	meta.FundingEstimatedRows = 1
	if e := writeJSON(filepath.Join(p.DatasetDir, "metadata.json"), meta); e != nil {
		t.Fatal(e)
	}
	if _, e := optimizeLoad(p); e == nil {
		t.Fatal("accepted claimed proxy rows for exact data")
	}
	meta.FundingEstimatedRows = 0
	if e := writeJSON(filepath.Join(p.DatasetDir, "metadata.json"), meta); e != nil {
		t.Fatal(e)
	}
	if _, e := optimizeLoad(p); e != nil {
		t.Fatalf("legacy metadata/data failed: %v", e)
	}
}

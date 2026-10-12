package backtest

import (
	"errors"
	"lorentzgo/indicator"
	"lorentzgo/market"
	"math"
	"testing"
)

func TestTraceSameLedgerAllExecutionSamples(t *testing.T) {
	const d int64 = 3600000
	rows := []market.Candle{{OpenTime: 0, CloseTime: d - 1, Open: 100, High: 102, Low: 99, Close: 101, Volume: 1}, {OpenTime: d, CloseTime: 2*d - 1, Open: 101, High: 111, Low: 100, Close: 105, Volume: 1}, {OpenTime: 2 * d, CloseTime: 3*d - 1, Open: 120, High: 121, Low: 100, Close: 103, Volume: 1}, {OpenTime: 3 * d, CloseTime: 4*d - 1, Open: 95, High: 105, Low: 90, Close: 102, Volume: 1}}
	points := make([]indicator.Point, len(rows))
	points[0].StartLong = true
	points[1].StartShort = true
	fund := []market.Funding{{Time: d + 1, Rate: .001, MarkPrice: 105}, {Time: 2 * d, Rate: -.001, MarkPrice: 120}, {Time: 3*d + 1, Rate: .001, MarkPrice: 95}}
	cfg := DefaultConfig()
	cfg.EndTime = 4 * d
	old, e := Evaluate(rows, points, fund, cfg)
	if e != nil {
		t.Fatal(e)
	}
	var events []TraceEvent
	observed, e := EvaluateWithOptions(rows, points, fund, cfg, nil, RiskConfig{}, nil, EvaluationOptions{Trace: func(e TraceEvent) error { events = append(events, e); return nil }})
	if e != nil {
		t.Fatal(e)
	}
	if observed.Metrics != old.Metrics { // ProfitFactor pointers compare separately.
		a, b := observed.Metrics, old.Metrics
		a.ProfitFactor = nil
		b.ProfitFactor = nil
		if a != b {
			t.Fatalf("trace changed metrics: %+v %+v", a, b)
		}
	}
	peak := cfg.InitialEquity
	dd := 0.
	entries, exits, settled := 0, 0, 0
	last := int64(-1)
	for i, e := range events {
		if e.Sequence != i+1 || e.Time < last {
			t.Fatal("event sequence")
		}
		last = e.Time
		if e.Sampled {
			peak = math.Max(peak, e.Equity)
			dd = math.Max(dd, 100*(peak-e.Equity)/peak)
		}
		switch e.Phase {
		case "entry":
			entries++
		case "exit":
			exits++
			if e.Trade == nil {
				t.Fatal("missing closed trade")
			}
		case "funding":
			settled++
			if e.Sampled || e.Funding == nil {
				t.Fatal("funding sample/provenance")
			}
		}
	}
	if dd != old.Metrics.MaxDrawdownPct || entries != 2 || exits != 2 || settled != 3 {
		t.Fatalf("dd/events %.12g %+v", dd, events)
	}
}
func TestAdverseFundingDirectionAndProtectedFailure(t *testing.T) {
	const d int64 = 3600000
	rows := []market.Candle{{OpenTime: 0, CloseTime: d - 1, Open: 100, High: 101, Low: 99, Close: 100, Volume: 1}, {OpenTime: d, CloseTime: 2*d - 1, Open: 100, High: 101, Low: 99, Close: 100, Volume: 1}, {OpenTime: 2 * d, CloseTime: 3*d - 1, Open: 100, High: 101, Low: 99, Close: 100, Volume: 1}, {OpenTime: 3 * d, CloseTime: 4*d - 1, Open: 100, High: 101, Low: 99, Close: 100, Volume: 1}}
	points := make([]indicator.Point, len(rows))
	points[0].StartLong = true
	points[1].StartShort = true
	fund := []market.Funding{{Time: d + 1, Rate: .001, MarkPrice: 100}, {Time: 2*d + 1, Rate: .001, MarkPrice: 100}, {Time: 3*d + 1, Rate: -.001, MarkPrice: 100}}
	opts := EvaluationOptions{FundingMark: func(f market.Funding, dir int, qty float64) (float64, error) {
		if float64(dir)*qty*f.Rate > 0 {
			return 110, nil
		}
		return 90, nil
	}}
	var paid []float64
	opts.Trace = func(e TraceEvent) error {
		if e.Phase == "funding" {
			paid = append(paid, e.Price)
		}
		return nil
	}
	_, e := EvaluateWithOptions(rows, points, fund, DefaultConfig(), nil, RiskConfig{}, nil, opts)
	if e != nil {
		t.Fatal(e)
	}
	if len(paid) != 3 || paid[0] != 110 || paid[1] != 90 || paid[2] != 110 {
		t.Fatalf("directional funding %+v", paid)
	}
	rows[2].High = 1000
	_, e = EvaluateWithOptions(rows, points, fund, DefaultConfig(), nil, RiskConfig{}, nil, opts)
	var guard EquityProtectionError
	if !errors.As(e, &guard) {
		t.Fatalf("expected protected failure: %v", e)
	}
}

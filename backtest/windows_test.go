package backtest

import (
	"lorentzgo/indicator"
	"testing"
)

func TestContinuousWindowKeepsOpenPosition(t *testing.T) {
	rows := candles(100, 100, 110, 120, 130, 140)
	s := make([]indicator.Point, len(rows))
	s[0].StartLong = true
	cfg := config()
	r, e := Evaluate(rows, s, nil, cfg)
	if e != nil {
		t.Fatal(e)
	}
	if len(r.Trades) != 1 || r.Trades[0].ExitTime != 599 {
		t.Fatal("position flattened at intermediate boundary")
	}
	a, e := SummarizeWindow(r, 0, 300)
	if e != nil {
		t.Fatal(e)
	}
	b, e := SummarizeWindow(r, 300, 600)
	if e != nil {
		t.Fatal(e)
	}
	near(t, (1+a.NetReturnPct/100)*(1+b.NetReturnPct/100), 1+r.Metrics.NetReturnPct/100)
	if a.ClosedTrades != 0 || b.ClosedTrades != 1 {
		t.Fatal("closed trade double counted")
	}
}

package backtest

import (
	"lorentzgo/indicator"
	"testing"
)

// A pullback can arrive after the classifier's four-bar signal age. The
// execution cap starts at this actual fill, while ordinary exits still win.
func TestLatePullbackHoldCapStartsAtActualFill(t *testing.T) {
	c := candles(100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100)
	const hour = int64(3600000)
	for i := range c {
		c[i].OpenTime = int64(i) * hour
		c[i].CloseTime = int64(i+1)*hour - 1
	}
	s := make([]indicator.Point, len(c))
	s[5].StartLong = true // Pullback's decision is later than the original direction.
	entries := make([]EntryInfo, len(c))
	entries[5] = EntryInfo{Kind: "pullback", Direction: 1, DecisionTime: 6 * hour, AvailableTime: 6 * hour}
	cfg := config()
	cfg.ExitPolicy = "four-bars"
	cfg.MaxHoldBars = 4
	r, err := EvaluateWithEntries(c, s, nil, cfg, entries)
	if err != nil {
		t.Fatal(err)
	}
	if len(r.Trades) != 1 || r.Trades[0].EntryTime != 6*hour || r.Trades[0].ExitTime != 10*hour || r.Trades[0].Reason != "max_hold_bars" || r.Trades[0].EntryKind != "pullback" {
		t.Fatalf("late pullback timing: %+v", r.Trades)
	}
	s[7].EndLong = true
	r, err = EvaluateWithEntries(c, s, nil, cfg, entries)
	if err != nil {
		t.Fatal(err)
	}
	if r.Trades[0].ExitTime != 8*hour || r.Trades[0].Reason != "signal_exit" {
		t.Fatalf("early signal exit ignored: %+v", r.Trades)
	}
}

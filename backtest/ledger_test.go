package backtest

import (
	"encoding/json"
	"lorentzgo/indicator"
	"lorentzgo/market"
	"math"
	"testing"
)

func candles(prices ...float64) []market.Candle {
	out := make([]market.Candle, len(prices))
	for i, p := range prices {
		out[i] = market.Candle{OpenTime: int64(i * 100), CloseTime: int64(i*100 + 99), Open: p, High: p, Low: p, Close: p}
	}
	return out
}
func config() Config { c := DefaultConfig(); c.FeeBPS = 0; c.SlippageBPS = 0; return c }

func TestFundingPaymentRechecksAdverseEquity(t *testing.T) {
	c := candles(100, 100, 100)
	c[1].High = 199
	s := make([]indicator.Point, 3)
	s[0].StartShort = true
	f := []market.Funding{{Time: 150, Rate: -0.05, MarkPrice: 100}}
	if _, err := Evaluate(c, s, f, config()); err == nil {
		t.Fatal("missed insolvency possible after funding at adverse OHLC extreme")
	}
}
func near(t *testing.T, got, want float64) {
	t.Helper()
	if math.Abs(got-want) > 1e-8*math.Max(1, math.Abs(want)) {
		t.Fatalf("got %.12f want %.12f", got, want)
	}
}
func run(t *testing.T, c []market.Candle, s []indicator.Point, f []market.Funding, cfg Config) Report {
	t.Helper()
	r, e := Evaluate(c, s, f, cfg)
	if e != nil {
		t.Fatal(e)
	}
	return r
}
func TestLongShortReversalCosts(t *testing.T) {
	c := candles(90, 100, 120, 110)
	s := make([]indicator.Point, 4)
	s[0].StartLong = true
	s[1].StartShort = true
	s[2].EndShort = true
	cfg := config()
	cfg.FeeBPS = 10
	cfg.SlippageBPS = 20
	r := run(t, c, s, nil, cfg)
	if len(r.Trades) != 2 {
		t.Fatalf("trades %+v", r.Trades)
	}
	a, b := r.Trades[0], r.Trades[1]
	q := 10000 / 100.2
	gross := q * (119.76 - 100.2)
	fees := 10 + q*119.76*.001
	eq := 10000 + gross - fees
	near(t, a.Quantity, q)
	near(t, a.EntryPrice, 100.2)
	near(t, a.ExitPrice, 119.76)
	near(t, a.NetPnL, gross-fees)
	q2 := eq / 119.76
	gross2 := q2 * (119.76 - 110.22)
	fees2 := eq*.001 + q2*110.22*.001
	near(t, b.Quantity, q2)
	near(t, b.NetPnL, gross2-fees2)
	near(t, r.Metrics.NetPnL, gross-fees+gross2-fees2)
	if a.ExitTime != 200 || b.EntryTime != 200 || a.Reason != "reverse" || b.Reason != "signal_exit" || b.Side != "short" {
		t.Fatalf("incorrect reversal %+v %+v", a, b)
	}
	near(t, r.Equity[3].Equity, 10000+r.Metrics.NetPnL)
	if r.Metrics.ProfitFactor != nil {
		t.Fatal("no losses must have null profit factor")
	}
	if _, err := json.Marshal(r); err != nil {
		t.Fatal(err)
	}
}
func TestMatchingExitThenOppositeOpen(t *testing.T) {
	c := candles(100, 100, 110)
	s := make([]indicator.Point, 3)
	s[0].StartLong = true
	s[1].EndLong = true
	s[1].StartShort = true
	r := run(t, c, s, nil, config())
	if len(r.Trades) != 2 || r.Trades[0].Reason != "signal_exit" || r.Trades[1].EntryTime != 200 || r.Trades[1].Reason != "end_of_window" {
		t.Fatalf("trades %+v", r.Trades)
	}
}
func TestFundingExactBoundaryAndMultipleDailySettlements(t *testing.T) {
	c := candles(100, 100, 100, 100)
	s := make([]indicator.Point, 4)
	s[0].StartLong = true
	s[1].StartShort = true
	s[2].EndShort = true
	f := []market.Funding{{Time: 50, Rate: .5, MarkPrice: 100}, {Time: 100, Rate: .5, MarkPrice: 100}, {Time: 150, Rate: .01, MarkPrice: 100}, {Time: 200, Rate: .02, MarkPrice: 100}, {Time: 250, Rate: .01, MarkPrice: 100}, {Time: 300, Rate: .02, MarkPrice: 100}, {Time: 350, Rate: .5, MarkPrice: 100}}
	r := run(t, c, s, f, config())
	near(t, r.Trades[0].Funding, -300)
	near(t, r.Trades[1].Funding, 291)
	near(t, r.Metrics.Funding, -9)
	// A full daily candle contains three independent funding settlements.
	c = []market.Candle{{OpenTime: 0, CloseTime: 86399999, Open: 100, High: 100, Low: 100, Close: 100}, {OpenTime: 86400000, CloseTime: 172799999, Open: 100, High: 100, Low: 100, Close: 100}}
	s = make([]indicator.Point, 2)
	s[0].StartLong = true
	f = []market.Funding{{Time: 86400000, Rate: .5, MarkPrice: 100}, {Time: 100000000, Rate: .001, MarkPrice: 100}, {Time: 130000000, Rate: .002, MarkPrice: 110}, {Time: 170000000, Rate: -.001, MarkPrice: 90}}
	r = run(t, c, s, f, config())
	near(t, r.Trades[0].Funding, -10-22+9)
}
func TestWindowAndUnfillableFinalSignal(t *testing.T) {
	c := candles(100, 110, 120, 130, 140)
	s := make([]indicator.Point, 5)
	s[0].StartLong = true
	s[2].StartLong = true
	s[3].StartShort = true
	s[4].StartLong = true
	cfg := config()
	cfg.StartTime = 200
	cfg.EndTime = 400
	r := run(t, c, s, nil, cfg)
	if len(r.Trades) != 1 || r.Trades[0].EntryTime != 300 || r.Trades[0].ExitTime != 399 {
		t.Fatalf("window trades %+v", r.Trades)
	}
	near(t, r.Trades[0].NetPnL, 0)
	if len(r.Equity) != 2 {
		t.Fatal("scoring outside range")
	}
	s = make([]indicator.Point, 5)
	s[4].StartLong = true
	r = run(t, c, s, nil, config())
	if len(r.Trades) != 0 {
		t.Fatal("last signal was filled")
	}
}
func TestFourBarsAndNoPyramiding(t *testing.T) {
	c := candles(100, 100, 110, 120, 130, 140, 150)
	s := make([]indicator.Point, 7)
	for i := 0; i < 4; i++ {
		s[i].StartLong = true
	}
	cfg := config()
	cfg.ExitPolicy = "four-bars"
	cfg.MaxHoldBars = 0
	r := run(t, c, s, nil, cfg)
	if len(r.Trades) != 1 || r.Trades[0].ExitTime != 500 || r.Trades[0].Reason != "max_hold_bars" {
		t.Fatalf("four bars %+v", r.Trades)
	}
	near(t, r.Trades[0].Quantity, 100)
	near(t, r.Trades[0].NetPnL, 4000)
	s[2].EndLong = true
	r = run(t, c, s, nil, cfg)
	if r.Trades[0].ExitTime != 300 || r.Trades[0].Reason != "signal_exit" {
		t.Fatal("signal exit ignored")
	}
}
func TestDrawdownLossAndFlatMetrics(t *testing.T) {
	c := candles(100, 100, 120, 90)
	s := make([]indicator.Point, 4)
	s[0].StartLong = true
	r := run(t, c, s, nil, config())
	near(t, r.Metrics.MaxDrawdownPct, 25)
	near(t, r.Metrics.NetReturnPct, -10)
	near(t, r.Metrics.ExpectancyPct, -10)
	if r.Metrics.Losses != 1 || r.Metrics.ProfitFactor == nil || *r.Metrics.ProfitFactor != 0 {
		t.Fatalf("metrics %+v", r.Metrics)
	}
	s = make([]indicator.Point, 4)
	r = run(t, c, s, nil, config())
	if r.Metrics.Trades != 0 || r.Metrics.ProfitFactor != nil || r.Metrics.NetPnL != 0 || r.Metrics.MaxDrawdownPct != 0 {
		t.Fatalf("flat metrics %+v", r.Metrics)
	}
	c = candles(100, 100)
	s = make([]indicator.Point, 2)
	s[0].StartLong = true
	r = run(t, c, s, nil, config())
	if r.Metrics.Breakeven != 1 {
		t.Fatal("breakeven not separate")
	}
}
func TestInvalidAndBankruptcy(t *testing.T) {
	c := candles(100, 100)
	s := make([]indicator.Point, 2)
	cases := []struct {
		name string
		c    []market.Candle
		s    []indicator.Point
		f    []market.Funding
		cfg  Config
	}{
		{"length", c, s[:1], nil, config()},
		{"both entries", c, []indicator.Point{{StartLong: true, StartShort: true}, {}}, nil, config()},
		{"missing mark", c, s, []market.Funding{{Time: 50, Rate: .01}}, config()},
		{"funding order", c, s, []market.Funding{{Time: 50, MarkPrice: 100}, {Time: 40, MarkPrice: 100}}, config()},
	}
	bad := config()
	bad.FeeBPS = math.NaN()
	cases = append(cases, struct {
		name string
		c    []market.Candle
		s    []indicator.Point
		f    []market.Funding
		cfg  Config
	}{"nan config", c, s, nil, bad})
	bankrupt := candles(100, 100, 210)
	short := make([]indicator.Point, 3)
	short[0].StartShort = true
	cases = append(cases, struct {
		name string
		c    []market.Candle
		s    []indicator.Point
		f    []market.Funding
		cfg  Config
	}{"bankruptcy", bankrupt, short, nil, config()})
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if _, err := Evaluate(tc.c, tc.s, tc.f, tc.cfg); err == nil {
				t.Fatal("expected explicit error")
			}
		})
	}
}

func TestFinalLiquidationFullCostsAndFunding(t *testing.T) {
	c := candles(100, 100)
	c[1].High = 110
	c[1].Close = 110
	s := make([]indicator.Point, 2)
	s[0].StartLong = true
	cfg := config()
	cfg.FeeBPS = 10
	cfg.SlippageBPS = 20
	r := run(t, c, s, []market.Funding{{Time: 199, Rate: .01, MarkPrice: 105}}, cfg)
	tr := r.Trades[0]
	q := 10000 / 100.2
	gross := q * (109.78 - 100.2)
	fees := 10 + q*109.78*.001
	fund := -q * 105 * .01
	near(t, tr.NetPnL, gross-fees+fund)
	near(t, r.Equity[1].Equity, 10000+gross-fees+fund)
	if tr.Reason != "end_of_window" || tr.ExitTime != 199 {
		t.Fatalf("final trade %+v", tr)
	}
}
func TestIntrabarAndFundingBankruptcyErrors(t *testing.T) {
	c := candles(100, 100)
	c[1].High = 210
	s := make([]indicator.Point, 2)
	s[0].StartShort = true
	if _, err := Evaluate(c, s, nil, config()); err == nil {
		t.Fatal("intrabar insolvent short must error")
	}
	c = candles(100, 100)
	s[0].StartLong = true
	s[0].StartShort = false
	if _, err := Evaluate(c, s, []market.Funding{{Time: 150, Rate: 2, MarkPrice: 100}}, config()); err == nil {
		t.Fatal("funding insolvency must error")
	}
}

func TestTypedEntryMetadataSingleLedger(t *testing.T) {
	c := candles(90, 100, 110, 105, 100)
	s := make([]indicator.Point, len(c))
	s[0].StartLong = true
	s[1].StartLong = true
	s[2].StartShort = true
	s[3].EndShort = true
	info := make([]EntryInfo, len(c))
	for i, p := range s {
		d := 1
		kind := "main_entry"
		if p.StartShort {
			d = -1
			kind = "pullback"
		}
		if p.StartLong || p.StartShort {
			info[i] = EntryInfo{kind, d, c[i].CloseTime + 1, c[i].CloseTime + 1}
		}
	}
	cfg := DefaultConfig()
	old, e := Evaluate(c, s, nil, cfg)
	if e != nil {
		t.Fatal(e)
	}
	r, e := EvaluateWithEntries(c, s, nil, cfg, info)
	if e != nil {
		t.Fatal(e)
	}
	if len(r.Trades) != 2 || r.Metrics != old.Metrics { // ProfitFactor is a pointer, compare numeric separately below.
		if len(r.Trades) != 2 || r.Metrics.Trades != old.Metrics.Trades || r.Metrics.NetPnL != old.Metrics.NetPnL || r.Metrics.Fees != old.Metrics.Fees {
			t.Fatal("typed entries changed cash/cost")
		}
	}
	if r.Trades[0].EntryKind != "main_entry" || r.Trades[1].EntryKind != "pullback" || r.Trades[1].EntryDecisionTime != 300 {
		t.Fatal("wrong entry attribution")
	}
	info[0].AvailableTime = 101
	if _, e := EvaluateWithEntries(c, s, nil, cfg, info); e == nil {
		t.Fatal("accepted future metadata")
	}
}

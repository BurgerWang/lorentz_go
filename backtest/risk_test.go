package backtest

import (
	"lorentzgo/indicator"
	"lorentzgo/market"
	"math"
	"reflect"
	"testing"
)

func riskRun(t *testing.T, c []market.Candle, s []indicator.Point, f []market.Funding, cfg Config, risk RiskConfig, atr []float64) Report {
	t.Helper()
	r, err := EvaluateWithRisk(c, s, f, cfg, nil, risk, atr)
	if err != nil {
		t.Fatal(err)
	}
	return r
}
func activeRisk() RiskConfig { c := DefaultRiskConfig(); c.Enabled = true; return c }

func TestRiskATRIndependentValuesAndPrefix(t *testing.T) {
	c := candles(10, 12, 15, 11)
	for i := range c {
		c[i].High++
		c[i].Low--
	}
	got, err := RiskATR(c, 2)
	if err != nil {
		t.Fatal(err)
	}
	if !math.IsNaN(got[0]) {
		t.Fatal("ATR warmup filled")
	}
	near(t, got[1], 2.5)
	near(t, got[2], 3.25)
	near(t, got[3], 4.125)
	short, _ := RiskATR(c[:3], 2)
	for i := 1; i < len(short); i++ {
		near(t, short[i], got[i])
	}
	if _, err := RiskATR(c, 0); err == nil {
		t.Fatal("accepted zero ATR period")
	}
}

func TestRiskLongShortGapAndEntryATRFreeze(t *testing.T) {
	for _, d := range []int{1, -1} {
		c := candles(100, 100, 100-5*float64(d), 100-10*float64(d), 100)
		// Entry row's extreme and ATR spike cannot change the initial distance.
		c[1].High = 150
		c[1].Low = 50
		s := make([]indicator.Point, len(c))
		s[0].StartLong = d == 1
		s[0].StartShort = d == -1
		cfg := config()
		cfg.SlippageBPS = 20
		cfg.FeeBPS = 10
		r := riskRun(t, c, s, nil, cfg, activeRisk(), []float64{2, 99, 2, 2, 2})
		trade := r.Trades[0]
		if trade.ExitTime != 300 || trade.Reason != "risk_exit" {
			t.Fatalf("gap trade %+v", trade)
		}
		near(t, trade.EntryPrice, 100*(1+float64(d)*.002))
		near(t, trade.InitialRiskDistance, 4)
		near(t, trade.RiskLine, trade.EntryPrice-float64(d)*4)
		near(t, trade.ExitPrice, c[3].Open*(1-float64(d)*.002))
		if trade.ExitPrice == trade.RiskLine {
			t.Fatal("gap filled at fictional risk line")
		}
	}
}

func TestRiskOnlyCloseConfirmsAndTrailOnlyTightens(t *testing.T) {
	for _, d := range []int{1, -1} {
		c := candles(100, 100, 100+10*float64(d), 100+8*float64(d), 100+6*float64(d), 100+4*float64(d))
		c[1].High = 105
		c[1].Low = 95 // Intrabar crossing alone does not close.
		s := make([]indicator.Point, len(c))
		s[0].StartLong = d == 1
		s[0].StartShort = d == -1
		risk := activeRisk()
		risk.Trail = true
		r := riskRun(t, c, s, nil, config(), risk, []float64{2, 2, 2, 10, 2, 2})
		trade := r.Trades[0]
		if trade.ExitTime != 500 || trade.Reason != "risk_exit" {
			t.Fatalf("trail timing %+v", trade)
		}
		near(t, trade.RiskLine, 100+6*float64(d))
	}
}

func TestRiskExitPriorityAndReentry(t *testing.T) {
	for _, test := range []struct {
		name, want          string
		exit, reverse, same bool
	}{{"signal", "signal_exit", true, true, false}, {"reverse", "reverse", false, true, false}, {"risk", "risk_exit", false, false, true}} {
		t.Run(test.name, func(t *testing.T) {
			c := candles(100, 100, 95, 90, 90)
			s := make([]indicator.Point, len(c))
			s[0].StartLong = true
			s[2].EndLong = test.exit
			s[2].StartShort = test.reverse
			s[2].StartLong = test.same
			cfg := config()
			cfg.ExitPolicy = "four-bars"
			cfg.MaxHoldBars = 2
			cfg.FeeBPS = 10
			cfg.SlippageBPS = 20
			f := []market.Funding{{Time: 300, Rate: .001, MarkPrice: 90}}
			r := riskRun(t, c, s, f, cfg, activeRisk(), []float64{2, 2, 2, 2, 2})
			if r.Trades[0].Reason != test.want || r.Trades[0].ExitTime != 300 || len(r.Trades) != 2 || r.Trades[1].EntryTime != 300 {
				t.Fatalf("priority %+v", r.Trades)
			}
			near(t, r.Trades[0].Funding, -r.Trades[0].Quantity*.09)
			near(t, r.Trades[1].Funding, 0)
		})
	}
}

func TestRiskNetBreakevenIncludesFeesSlippageAndFunding(t *testing.T) {
	for _, d := range []int{1, -1} {
		cfg := config()
		cfg.FeeBPS = 10
		cfg.SlippageBPS = 20
		p := Trade{EntryPrice: 100 * (1 + float64(d)*.002), Quantity: 100, Fees: 10, Funding: -5}
		line, err := netBreakevenLine(&p, d, cfg)
		if err != nil {
			t.Fatal(err)
		}
		exit := line * (1 - float64(d)*.002)
		net := float64(d)*p.Quantity*(exit-p.EntryPrice) - p.Fees - p.Quantity*exit*.001 + p.Funding
		near(t, net, 0)
		c := candles(100, 100, 100+10*float64(d), line-float64(d), line-3*float64(d), 100)
		s := make([]indicator.Point, len(c))
		s[0].StartLong = d == 1
		s[0].StartShort = d == -1
		risk := activeRisk()
		risk.BreakevenR = 1
		f := []market.Funding{{Time: 250, Rate: float64(d) * .001, MarkPrice: 100}}
		r := riskRun(t, c, s, f, cfg, risk, []float64{2, 2, 2, 2, 2, 2})
		trade := r.Trades[0]
		want, err := netBreakevenLine(&Trade{EntryPrice: trade.EntryPrice, Quantity: trade.Quantity, Fees: trade.Quantity * trade.EntryPrice * .001, Funding: trade.Funding}, d, cfg)
		if err != nil {
			t.Fatal(err)
		}
		near(t, trade.RiskLine, want)
		if trade.Reason != "risk_exit" || trade.ExitTime != 400 || trade.NetPnL >= 0 {
			t.Fatalf("gap cannot guarantee breakeven %+v", trade)
		}
	}
}

func TestRiskDisabledExactLegacyAndInvalidEntryATR(t *testing.T) {
	c := candles(100, 100, 110, 90, 100)
	s := make([]indicator.Point, len(c))
	s[0].StartLong = true
	s[2].StartShort = true
	f := []market.Funding{{Time: 250, Rate: .001, MarkPrice: 110}}
	cfg := DefaultConfig()
	old, err := Evaluate(c, s, f, cfg)
	if err != nil {
		t.Fatal(err)
	}
	off, err := EvaluateWithRisk(c, s, f, cfg, nil, RiskConfig{}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(old, off) {
		t.Fatal("risk off changed legacy report")
	}
	entries := make([]EntryInfo, len(c))
	entries[0] = EntryInfo{Kind: "main_entry", Direction: 1, DecisionTime: 100, AvailableTime: 100}
	entries[2] = EntryInfo{Kind: "pullback", Direction: -1, DecisionTime: 300, AvailableTime: 300}
	old, err = EvaluateWithEntries(c, s, f, cfg, entries)
	if err != nil {
		t.Fatal(err)
	}
	off, err = EvaluateWithRisk(c, s, f, cfg, entries, RiskConfig{}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(old, off) {
		t.Fatal("risk off changed metadata report")
	}
	for _, bad := range []float64{0, -1, math.NaN(), math.Inf(1)} {
		if _, err := EvaluateWithRisk(c, s, nil, cfg, nil, activeRisk(), []float64{bad, 2, 2, 2, 2}); err == nil {
			t.Fatal("accepted unavailable entry ATR")
		}
	}
	if _, err := EvaluateWithRisk(c, s, nil, cfg, nil, activeRisk(), nil); err == nil {
		t.Fatal("accepted misaligned ATR")
	}
}

func TestRiskRejectsInactiveParameters(t *testing.T) {
	c := DefaultRiskConfig()
	if e := c.Validate(); e != nil {
		t.Fatal(e)
	}
	for _, change := range []func(*RiskConfig){func(c *RiskConfig) { c.Trail = true }, func(c *RiskConfig) { c.ATRPeriod = 15 }, func(c *RiskConfig) { c.ATRMultiplier = 3 }, func(c *RiskConfig) { c.BreakevenR = 1 }, func(c *RiskConfig) { c.Enabled = true; c.TrailMultiplier = 3 }} {
		bad := c
		change(&bad)
		if e := bad.Validate(); e == nil {
			t.Fatal("inactive parameters accepted")
		}
	}
}

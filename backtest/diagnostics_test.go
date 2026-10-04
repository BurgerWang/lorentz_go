package backtest

import (
	"encoding/json"
	"lorentzgo/indicator"
	"lorentzgo/market"
	"math"
	"reflect"
	"strings"
	"testing"
)

func TestDiagnosticsLongShortSymmetryFundingAndNetContributions(t *testing.T) {
	r := Report{Config: Config{SlippageBPS: 100}, Trades: []Trade{
		{Side: "long", EntryKind: "main_entry", EntryTime: 10, ExitTime: 110, Quantity: 2, EntryPrice: 101, ExitPrice: 108.9, GrossPnL: 15.8, Fees: 1, Funding: -3, NetPnL: 11.8},
		{Side: "short", EntryKind: "pullback", EntryTime: 120, ExitTime: 420, Quantity: 2, EntryPrice: 108.9, ExitPrice: 101, GrossPnL: 15.8, Fees: 1, Funding: 3, NetPnL: 17.8},
	}, Metrics: Metrics{NetPnL: 29.6, Fees: 2}}
	before, _ := json.Marshal(r)
	d, err := SummarizeDiagnostics(r)
	if err != nil {
		t.Fatal(err)
	}
	// Raw fills are 100 -> 110 for long and 110 -> 100 for short.
	// Each has 2*(1+1.1)=4.2 adverse slippage in ledger cash units.
	near(t, d.EstimatedSlippage, 8.4)
	near(t, d.AbsGrossBeforeEstimatedSlippage, 40)
	near(t, *d.CostToAbsGrossPct, 33.5) // (2+8.4+3)/40*100
	near(t, d.Fees, 2)
	near(t, d.FundingPayments, 3)
	near(t, d.FundingReceipts, 3)
	near(t, d.NetPnL, r.Metrics.NetPnL)
	near(t, d.AverageHoldMillis, 200)
	if d.MaxHoldMillis != 300 || d.Top3PositivePnLPct != 100 {
		t.Fatalf("holding or positive concentration mismatch: %+v", d)
	}
	for _, groups := range []map[string]DiagnosticGroup{d.BySide, d.ByEntryKind} {
		net, fees, funding, trades, wins := 0.0, 0.0, 0.0, 0, 0
		for _, g := range groups {
			net, fees, funding = net+g.NetPnL, fees+g.Fees, funding+g.Funding
			trades, wins = trades+g.Trades, wins+g.Wins
		}
		near(t, net, 29.6)
		near(t, fees, 2)
		near(t, funding, 0)
		if trades != 2 || wins != 2 {
			t.Fatal("group trade/win counts do not reconcile")
		}
	}
	if d.BySide["long"].Funding != -3 || d.BySide["short"].Funding != 3 || d.ByEntryKind["pullback"].NetPnL != 17.8 {
		t.Fatal("group attribution lost")
	}
	after, _ := json.Marshal(r)
	if string(before) != string(after) {
		t.Fatal("diagnostics changed report ledger")
	}
	if !strings.Contains(d.FundingBasis, "not gross settlement flows") || !strings.Contains(d.CostRatioBasis, "exceed 100%") || !strings.Contains(d.SlippageBasis, "NetPnL is unchanged") {
		t.Fatal("required accounting limits absent")
	}
}

func TestDiagnosticsWorksOnActualSyntheticLedgerReport(t *testing.T) {
	rows := candles(90, 100, 110, 100)
	s := make([]indicator.Point, len(rows))
	s[0].StartLong, s[1].StartShort, s[2].EndShort = true, true, true
	entries := make([]EntryInfo, len(rows))
	entries[0] = EntryInfo{Kind: "main_entry", Direction: 1, DecisionTime: 100, AvailableTime: 100}
	entries[1] = EntryInfo{Kind: "pullback", Direction: -1, DecisionTime: 200, AvailableTime: 200}
	cfg := DefaultConfig()
	cfg.FeeBPS, cfg.SlippageBPS = 10, 100
	funding := []market.Funding{{Time: 150, Rate: .001, MarkPrice: 100}, {Time: 250, Rate: .001, MarkPrice: 110}}
	r, err := EvaluateWithEntries(rows, s, funding, cfg, entries)
	if err != nil {
		t.Fatal(err)
	}
	d, err := SummarizeDiagnostics(r)
	if err != nil {
		t.Fatal(err)
	}
	qLong, qShort := r.Trades[0].Quantity, r.Trades[1].Quantity
	near(t, d.EstimatedSlippage, (qLong+qShort)*2.1)
	near(t, d.AbsGrossBeforeEstimatedSlippage, (qLong+qShort)*10)
	near(t, d.FundingPayments, qLong*.1)
	near(t, d.FundingReceipts, qShort*.11)
	near(t, d.FundingReceipts-d.FundingPayments, r.Metrics.Funding)
	near(t, d.NetPnL, r.Metrics.NetPnL)
	near(t, d.Fees, r.Metrics.Fees)
	near(t, d.ByEntryKind["main_entry"].NetPnL+d.ByEntryKind["pullback"].NetPnL, r.Metrics.NetPnL)
	if _, err := json.Marshal(d); err != nil {
		t.Fatalf("valid diagnostics cannot be encoded: %v", err)
	}
}

func TestDiagnosticsTopThreePositiveTradesAndHoldTime(t *testing.T) {
	r := Report{Trades: make([]Trade, 6), Metrics: Metrics{NetPnL: 5}}
	for i, net := range []float64{1, 2, 3, 4, -5, 0} {
		side, direction := "long", 1.0
		if i%2 == 1 {
			side, direction = "short", -1
		}
		kind := "main_entry"
		if i >= 3 {
			kind = "pullback"
		}
		r.Trades[i] = Trade{Side: side, EntryKind: kind, EntryTime: int64(i) * 100, ExitTime: int64(i)*100 + int64(i+1)*10, Quantity: 1, EntryPrice: 100, ExitPrice: 100 + direction*net, GrossPnL: net, NetPnL: net}
	}
	d, err := SummarizeDiagnostics(r)
	if err != nil {
		t.Fatal(err)
	}
	near(t, d.Top3PositivePnLPct, 90) // (4+3+2)/(1+2+3+4)
	near(t, d.AverageHoldMillis, 35)
	if d.MaxHoldMillis != 60 || d.BySide["long"].Wins != 2 || d.BySide["short"].Wins != 2 {
		t.Fatal("holding/wins incorrect")
	}
	near(t, d.BySide["long"].NetPnL, -1)
	near(t, d.BySide["short"].NetPnL, 6)
	near(t, d.ByEntryKind["main_entry"].NetPnL, 6)
	near(t, d.ByEntryKind["pullback"].NetPnL, -1)
	near(t, d.NetPnL, 5)
}

func TestDiagnosticsNoPositiveTradesZeroDenominatorAndLargeRatio(t *testing.T) {
	r := Report{Trades: []Trade{{Side: "long", Quantity: 1, EntryPrice: 100, ExitPrice: 101, GrossPnL: 1, Fees: 2, Funding: -1, NetPnL: -2}}}
	d, err := SummarizeDiagnostics(r)
	if err != nil {
		t.Fatal(err)
	}
	if d.Top3PositivePnLPct != 0 || d.BySide["long"].Wins != 0 || d.ByEntryKind[""].Trades != 1 {
		t.Fatal("no-profit or empty legacy kind handling incorrect")
	}
	near(t, *d.CostToAbsGrossPct, 300)
	r.Trades[0].ExitPrice, r.Trades[0].GrossPnL, r.Trades[0].NetPnL = 100, 0, -3
	d, err = SummarizeDiagnostics(r)
	if err != nil || d.CostToAbsGrossPct != nil || d.AbsGrossBeforeEstimatedSlippage != 0 {
		t.Fatalf("zero denominator not null: %+v %v", d, err)
	}
	encoded, _ := json.Marshal(d)
	if !strings.Contains(string(encoded), `"cost_to_abs_gross_pct":null`) {
		t.Fatal("undefined ratio did not encode as null")
	}
	empty, err := SummarizeDiagnostics(Report{})
	if err != nil || empty.CostToAbsGrossPct != nil || empty.Top3PositivePnLPct != 0 || empty.AverageHoldMillis != 0 || empty.MaxHoldMillis != 0 || len(empty.BySide) != 0 || len(empty.ByEntryKind) != 0 {
		t.Fatal("empty completed-trade report mishandled")
	}
}

func TestDiagnosticsRejectsInvalidAndUnrepresentableInputs(t *testing.T) {
	base := Report{Trades: []Trade{{Side: "long", Quantity: 1, EntryPrice: 100, ExitPrice: 100}}}
	for _, mutate := range []func(*Report){
		func(r *Report) { r.Config.SlippageBPS = 10000 },
		func(r *Report) { r.Config.SlippageBPS = math.NaN() },
		func(r *Report) { r.Trades[0].Side = "other" },
		func(r *Report) { r.Trades[0].Quantity = 0 },
		func(r *Report) { r.Trades[0].GrossPnL = math.Inf(1) },
		func(r *Report) { r.Trades[0].Fees = -1 },
		func(r *Report) { r.Trades[0].EntryTime = 10 },
		func(r *Report) { r.Config.SlippageBPS = 9999; r.Trades[0].ExitPrice = math.MaxFloat64 },
	} {
		r := base
		r.Trades = append([]Trade(nil), base.Trades...)
		mutate(&r)
		if _, err := SummarizeDiagnostics(r); err == nil {
			t.Fatalf("invalid report accepted: %+v", r)
		}
	}
	if !reflect.DeepEqual(base.Trades, []Trade{{Side: "long", Quantity: 1, EntryPrice: 100, ExitPrice: 100}}) {
		t.Fatal("test or helper mutated original report")
	}
}

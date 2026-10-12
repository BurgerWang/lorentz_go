package backtest

import (
	"fmt"
	"math"
	"sort"
)

type DiagnosticGroup struct {
	Trades  int     `json:"trades"`
	Wins    int     `json:"wins"`
	NetPnL  float64 `json:"net_pnl"`
	Fees    float64 `json:"fees"`
	Funding float64 `json:"funding"`
}

type Diagnostics struct {
	BySide                          map[string]DiagnosticGroup `json:"by_side"`
	ByEntryKind                     map[string]DiagnosticGroup `json:"by_entry_kind"`
	NetPnL                          float64                    `json:"net_pnl"`
	AverageHoldMillis               float64                    `json:"average_hold_millis"`
	MaxHoldMillis                   int64                      `json:"max_hold_millis"`
	Top3PositivePnLPct              float64                    `json:"top3_positive_pnl_pct"`
	Fees                            float64                    `json:"fees"`
	EstimatedSlippage               float64                    `json:"estimated_slippage"`
	FundingPayments                 float64                    `json:"funding_payments"`
	FundingReceipts                 float64                    `json:"funding_receipts"`
	AbsGrossBeforeEstimatedSlippage float64                    `json:"abs_gross_before_estimated_slippage"`
	CostToAbsGrossPct               *float64                   `json:"cost_to_abs_gross_pct"`
	FundingBasis                    string                     `json:"funding_basis"`
	SlippageBasis                   string                     `json:"slippage_basis"`
	CostRatioBasis                  string                     `json:"cost_ratio_basis"`
	HoldingTimeBasis                string                     `json:"holding_time_basis"`
	PositiveContributionBasis       string                     `json:"positive_contribution_basis"`
}

// SummarizeDiagnostics derives descriptive statistics from completed trades.
// It never subtracts estimated slippage from the already-costed net ledger.
func SummarizeDiagnostics(r Report) (Diagnostics, error) {
	if !finite(r.Config.SlippageBPS) || r.Config.SlippageBPS < 0 || r.Config.SlippageBPS >= 10000 {
		return Diagnostics{}, fmt.Errorf("diagnostics require finite slippage in [0,10000) bps")
	}
	d := Diagnostics{
		BySide:                    make(map[string]DiagnosticGroup),
		ByEntryKind:               make(map[string]DiagnosticGroup),
		FundingBasis:              "Completed-trade net funding split: payments=sum(max(-Trade.Funding,0)), receipts=sum(max(Trade.Funding,0)). Positive Funding is cash received. Offsetting settlements within one trade cannot be recovered from Report; these are not gross settlement flows.",
		SlippageBasis:             "Estimate in ledger cash units from actual fill prices: s=SlippageBPS/10000, side=+1 long/-1 short, rawEntry=actualEntry/(1+side*s), rawExit=actualExit/(1-side*s); cost=Quantity*side*((actualEntry-rawEntry)+(rawExit-actualExit)). GrossPnL already includes slippage; NetPnL is unchanged.",
		CostRatioBasis:            "100*(Fees+EstimatedSlippage+FundingPayments)/sum(abs(Trade.GrossPnL+estimatedSlippagePerTrade)). Denominator is absolute pre-slippage gross movement in ledger cash units; zero denominator is null. Funding receipts are excluded from the numerator. This descriptive cost ratio can exceed 100%; it is not a probability or expected profit.",
		HoldingTimeBasis:          "Completed trades only: ExitTime-EntryTime in milliseconds, arithmetic mean across trades and maximum; zero trades yields zero. Entry decision time is not holding time.",
		PositiveContributionBasis: "100*sum(largest up to three positive Trade.NetPnL)/sum(all positive Trade.NetPnL); no positive trade yields zero. Wins use NetPnL>0; empty legacy entry_kind remains an empty grouping key.",
	}
	slip := r.Config.SlippageBPS / 10000
	holdTotal, positiveTotal := 0.0, 0.0
	positive := make([]float64, 0, len(r.Trades))
	addGroup := func(groups map[string]DiagnosticGroup, key string, trade Trade) {
		group := groups[key]
		group.Trades++
		if trade.NetPnL > 0 {
			group.Wins++
		}
		group.NetPnL += trade.NetPnL
		group.Fees += trade.Fees
		group.Funding += trade.Funding
		groups[key] = group
	}
	for i, trade := range r.Trades {
		side := 0.0
		switch trade.Side {
		case "long":
			side = 1
		case "short":
			side = -1
		default:
			return Diagnostics{}, fmt.Errorf("trade %d: unsupported side %q", i, trade.Side)
		}
		if trade.EntryTime < 0 || trade.ExitTime < trade.EntryTime || !finite(trade.Quantity) || trade.Quantity <= 0 || !finite(trade.EntryPrice) || trade.EntryPrice <= 0 || !finite(trade.ExitPrice) || trade.ExitPrice <= 0 || !finite(trade.GrossPnL) || !finite(trade.NetPnL) || !finite(trade.Fees) || trade.Fees < 0 || !finite(trade.Funding) {
			return Diagnostics{}, fmt.Errorf("trade %d: invalid completed trade diagnostics inputs", i)
		}
		rawEntry := trade.EntryPrice / (1 + side*slip)
		rawExit := trade.ExitPrice / (1 - side*slip)
		slippage := trade.Quantity * side * ((trade.EntryPrice - rawEntry) + (rawExit - trade.ExitPrice))
		if !finite(rawEntry) || !finite(rawExit) || !finite(slippage) || slippage < 0 {
			return Diagnostics{}, fmt.Errorf("trade %d: unrepresentable adverse slippage estimate", i)
		}
		addGroup(d.BySide, trade.Side, trade)
		addGroup(d.ByEntryKind, trade.EntryKind, trade)
		d.NetPnL += trade.NetPnL
		d.Fees += trade.Fees
		d.EstimatedSlippage += slippage
		d.FundingPayments += math.Max(-trade.Funding, 0)
		d.FundingReceipts += math.Max(trade.Funding, 0)
		d.AbsGrossBeforeEstimatedSlippage += math.Abs(trade.GrossPnL + slippage)
		hold := trade.ExitTime - trade.EntryTime
		holdTotal += float64(hold)
		d.MaxHoldMillis = max(d.MaxHoldMillis, hold)
		if trade.NetPnL > 0 {
			positive = append(positive, trade.NetPnL)
			positiveTotal += trade.NetPnL
		}
	}
	if len(r.Trades) > 0 {
		d.AverageHoldMillis = holdTotal / float64(len(r.Trades))
	}
	if positiveTotal > 0 {
		sort.Sort(sort.Reverse(sort.Float64Slice(positive)))
		top := 0.0
		for _, profit := range positive[:min(3, len(positive))] {
			top += profit
		}
		d.Top3PositivePnLPct = top / positiveTotal * 100
	}
	if d.AbsGrossBeforeEstimatedSlippage > 0 {
		ratio := (d.Fees + d.EstimatedSlippage + d.FundingPayments) / d.AbsGrossBeforeEstimatedSlippage * 100
		d.CostToAbsGrossPct = &ratio
	}
	for _, value := range []float64{d.NetPnL, d.Fees, d.EstimatedSlippage, d.FundingPayments, d.FundingReceipts, d.AbsGrossBeforeEstimatedSlippage, d.AverageHoldMillis, d.Top3PositivePnLPct, positiveTotal} {
		if !finite(value) {
			return Diagnostics{}, fmt.Errorf("nonfinite aggregate diagnostics")
		}
	}
	if d.CostToAbsGrossPct != nil && !finite(*d.CostToAbsGrossPct) {
		return Diagnostics{}, fmt.Errorf("unrepresentable diagnostic cost ratio")
	}
	for _, groups := range []map[string]DiagnosticGroup{d.BySide, d.ByEntryKind} {
		for _, group := range groups {
			if !finite(group.NetPnL) || !finite(group.Fees) || !finite(group.Funding) {
				return Diagnostics{}, fmt.Errorf("nonfinite grouped diagnostics")
			}
		}
	}
	return d, nil
}

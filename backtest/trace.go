package backtest

import (
	"fmt"
	"lorentzgo/indicator"
	"lorentzgo/market"
)

// EquityProtectionError denotes a normally triggered protection. Controllers
// consume the reservation and fail this configuration; they must not score it.
type EquityProtectionError struct{ Message string }

func (e EquityProtectionError) Error() string { return e.Message }

// TraceEvent is an observation of the same ledger, never a second calculator.
// Sampled identifies exactly the equity observations used by MaxDrawdownPct.
type TraceEvent struct {
	Version     string          `json:"version"`
	Sequence    int             `json:"sequence"`
	Time        int64           `json:"time"`
	Phase       string          `json:"phase"`
	Sampled     bool            `json:"sampled"`
	Cash        float64         `json:"cash"`
	Equity      float64         `json:"equity"`
	Price       float64         `json:"price"`
	Direction   int             `json:"direction"`
	Quantity    float64         `json:"quantity"`
	EntryPrice  float64         `json:"entry_price"`
	TradeID     int             `json:"trade_id"`
	Fee         float64         `json:"fee"`
	GrossPnL    float64         `json:"gross_pnl"`
	FundingCash float64         `json:"funding_cash"`
	Funding     *market.Funding `json:"funding,omitempty"`
	Trade       *Trade          `json:"trade,omitempty"`
}
type EvaluationOptions struct {
	Trace       func(TraceEvent) error
	FundingMark func(market.Funding, int, float64) (float64, error)
}

func EvaluateWithOptions(candles []market.Candle, signals []indicator.Point, funding []market.Funding, cfg Config, entries []EntryInfo, risk RiskConfig, atr []float64, options EvaluationOptions) (Report, error) {
	if entries != nil && len(entries) != len(candles) {
		return Report{}, fmt.Errorf("entry metadata length mismatch")
	}
	if risk.Enabled {
		if err := risk.Validate(); err != nil {
			return Report{}, err
		}
		if len(atr) != len(candles) {
			return Report{}, fmt.Errorf("risk ATR length mismatch")
		}
	}
	return evaluate(candles, signals, funding, cfg, entries, risk, atr, options)
}

package backtest

import (
	"fmt"
	"lorentzgo/indicator"
	"lorentzgo/market"
	"math"
)

type RiskConfig struct {
	Enabled         bool    `json:"enabled"`
	ATRPeriod       int     `json:"atr_period"`
	ATRMultiplier   float64 `json:"atr_multiplier"`
	Trail           bool    `json:"trail"`
	TrailMultiplier float64 `json:"trail_multiplier"`
	BreakevenR      float64 `json:"breakeven_r"`
}

func DefaultRiskConfig() RiskConfig {
	return RiskConfig{ATRPeriod: 14, ATRMultiplier: 2, TrailMultiplier: 2}
}
func (c RiskConfig) Validate() error {
	if !c.Enabled && c != DefaultRiskConfig() {
		return fmt.Errorf("disabled risk parameters must retain defaults")
	}
	if !c.Trail && c.TrailMultiplier != DefaultRiskConfig().TrailMultiplier {
		return fmt.Errorf("inactive trail multiplier must retain default")
	}
	if c.ATRPeriod < 1 || !finite(c.ATRMultiplier) || c.ATRMultiplier <= 0 || !finite(c.TrailMultiplier) || c.TrailMultiplier <= 0 || !finite(c.BreakevenR) || c.BreakevenR < 0 {
		return fmt.Errorf("invalid risk configuration")
	}
	return nil
}

// RiskATR uses only the finalized prefix. Wilder smoothing starts at the SMA
// of length true ranges; the first true range is high-low.
func RiskATR(rows []market.Candle, length int) ([]float64, error) {
	if length < 1 {
		return nil, fmt.Errorf("ATR length must be positive")
	}
	out := make([]float64, len(rows))
	sum := 0.0
	state := math.NaN()
	for i, c := range rows {
		if !finite(c.High) || !finite(c.Low) || !finite(c.Close) || c.Low <= 0 || c.High < c.Low || c.Close < c.Low || c.Close > c.High {
			return nil, fmt.Errorf("invalid ATR candle %d", i)
		}
		tr := c.High - c.Low
		if i > 0 {
			tr = math.Max(tr, math.Max(math.Abs(c.High-rows[i-1].Close), math.Abs(c.Low-rows[i-1].Close)))
		}
		if i < length {
			sum += tr
			if i == length-1 {
				state = sum / float64(length)
			}
		} else {
			state += (tr - state) / float64(length)
		}
		if !finite(tr) || (!math.IsNaN(state) && !finite(state)) {
			return nil, fmt.Errorf("nonfinite ATR at %d", i)
		}
		out[i] = state
	}
	return out, nil
}

// EvaluateWithRisk keeps one ledger. Risk decisions confirmed at a close
// execute at the next actual open with adverse slippage, never at RiskLine.
// The caller supplies aligned causal ATR values and must gate warmup entries.
func EvaluateWithRisk(candles []market.Candle, signals []indicator.Point, funding []market.Funding, cfg Config, entries []EntryInfo, risk RiskConfig, atrAtDecision []float64) (Report, error) {
	if entries != nil && len(entries) != len(candles) {
		return Report{}, fmt.Errorf("entry metadata length mismatch")
	}
	if risk.Enabled {
		if err := risk.Validate(); err != nil {
			return Report{}, err
		}
		if len(atrAtDecision) != len(candles) {
			return Report{}, fmt.Errorf("risk ATR length mismatch")
		}
	}
	return evaluate(candles, signals, funding, cfg, entries, risk, atrAtDecision, EvaluationOptions{})
}

// netBreakevenLine solves net PnL=0 for the underlying next-open price,
// including actual entry fee, expected exit fee/slippage and funding to date.
// A later gap or funding settlement can still realize a loss.
func netBreakevenLine(p *Trade, direction int, cfg Config) (float64, error) {
	d := float64(direction)
	fee := cfg.FeeBPS / 10000
	slip := cfg.SlippageBPS / 10000
	exit := (d*p.EntryPrice + p.Fees/p.Quantity - p.Funding/p.Quantity) / (d - fee)
	line := exit / (1 - d*slip)
	if !finite(line) || line <= 0 {
		return 0, fmt.Errorf("invalid net breakeven risk line")
	}
	return line, nil
}

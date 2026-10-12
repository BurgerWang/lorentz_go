package strategy

import (
	"fmt"
	"lorentzgo/backtest"
)

type ExitConfig struct {
	Policy      string `json:"policy"`
	MaxHoldBars int    `json:"max_hold_bars"`
}

func DefaultExitConfig() ExitConfig { return ExitConfig{Policy: "signals", MaxHoldBars: 4} }
func (c ExitConfig) Validate() error {
	if (c.Policy != "signals" && c.Policy != "four-bars") || c.MaxHoldBars < 1 {
		return fmt.Errorf("invalid exit policy/hold limit")
	}
	if c.Policy == "signals" && c.MaxHoldBars != 4 {
		return fmt.Errorf("inactive hold limit must retain default4")
	}
	return nil
}
func (c Config) LedgerConfig() backtest.Config {
	b := backtest.DefaultConfig()
	if c.SchemaVersion >= 4 {
		b.ExitPolicy = c.Exit.Policy
		b.MaxHoldBars = c.Exit.MaxHoldBars
	}
	return b
}

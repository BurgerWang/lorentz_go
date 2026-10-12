package backtest

import (
	"fmt"
	"math"
)

type WindowMetrics struct {
	Start          int64   `json:"start"`
	End            int64   `json:"end"`
	NetReturnPct   float64 `json:"net_return_pct"`
	MaxDrawdownPct float64 `json:"max_drawdown_pct"`
	ClosedTrades   int     `json:"closed_trades"`
	Wins           int     `json:"wins"`
	WinRatePct     float64 `json:"win_rate_pct"`
}

// SummarizeWindow uses one continuous equity series; boundaries do not flatten
// positions. Closed trades are attributed by actual close time, not entry time.
func SummarizeWindow(r Report, start, end int64) (WindowMetrics, error) {
	m := WindowMetrics{Start: start, End: end}
	if start < 0 || end <= start {
		return m, fmt.Errorf("invalid continuous report window")
	}
	base := r.Config.InitialEquity
	foundBefore := false
	for _, p := range r.Equity {
		if p.Time < start {
			base = p.Equity
			foundBefore = true
		} else {
			break
		}
	}
	if !foundBefore && r.Config.StartTime != start {
		return m, fmt.Errorf("window start lacks prior equity boundary")
	}
	if !finite(base) || base <= 0 {
		return m, fmt.Errorf("invalid boundary equity")
	}
	last, peak := base, base
	count := 0
	for _, p := range r.Equity {
		if p.Time < start {
			continue
		}
		if p.Time >= end {
			break
		}
		if !finite(p.Equity) || p.Equity <= 0 {
			return m, fmt.Errorf("invalid continuous equity")
		}
		last = p.Equity
		peak = math.Max(peak, last)
		m.MaxDrawdownPct = math.Max(m.MaxDrawdownPct, 100*(peak-last)/peak)
		count++
	}
	if count == 0 {
		return m, fmt.Errorf("empty continuous equity window")
	}
	m.NetReturnPct = 100 * (last/base - 1)
	for _, t := range r.Trades {
		if t.ExitTime >= start && t.ExitTime < end {
			m.ClosedTrades++
			if t.NetPnL > 0 {
				m.Wins++
			}
		}
	}
	if m.ClosedTrades > 0 {
		m.WinRatePct = 100 * float64(m.Wins) / float64(m.ClosedTrades)
	}
	return m, nil
}

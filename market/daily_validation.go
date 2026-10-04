package market

import (
	"fmt"
	"math"
)

// ValidateDailyAggregation establishes same-market consistency from OHLCV,
// rather than trusting identical timestamps or a declared metadata symbol.
// Volume differences are diagnostics: historical Binance aggregates sometimes differ.
// Every daily row must be covered by complete low-timeframe bars.
type DailyAggregation struct {
	VolumeMismatchDays          int     `json:"volume_mismatch_days"`
	MaxVolumeRelativeDifference float64 `json:"max_volume_relative_difference"`
}

func ValidateDailyAggregation(low, daily []Candle, interval string) (DailyAggregation, error) {
	diagnostics := DailyAggregation{}
	step, e := IntervalMillis(interval)
	if e != nil {
		return diagnostics, e
	}
	const day int64 = 86400000
	if step >= day {
		return diagnostics, fmt.Errorf("daily consistency requires lower timeframe")
	}
	if _, e := ValidateCandles(low, interval); e != nil {
		return diagnostics, e
	}
	if _, e := ValidateCandles(daily, "1d"); e != nil {
		return diagnostics, e
	}
	j := 0
	equal := func(a, b float64) bool { return math.Abs(a-b) <= 1e-10*math.Max(1, math.Abs(b)) }
	for _, d := range daily {
		for j < len(low) && low[j].OpenTime < d.OpenTime {
			j++
		}
		if j >= len(low) || low[j].OpenTime != d.OpenTime {
			return diagnostics, fmt.Errorf("D1 missing lower-timeframe coverage at %d", d.OpenTime)
		}
		a := low[j]
		count := 0
		vol := 0.0
		for j < len(low) && low[j].OpenTime <= d.CloseTime {
			c := low[j]
			a.High = math.Max(a.High, c.High)
			a.Low = math.Min(a.Low, c.Low)
			a.Close = c.Close
			vol += c.Volume
			count++
			j++
		}
		if count != int(day/step) || !equal(a.Open, d.Open) || !equal(a.High, d.High) || !equal(a.Low, d.Low) || !equal(a.Close, d.Close) {
			return diagnostics, fmt.Errorf("D1 OHLCV differs from lower timeframe at %d", d.OpenTime)
		}
		if !equal(vol, d.Volume) {
			diagnostics.VolumeMismatchDays++
			diagnostics.MaxVolumeRelativeDifference = math.Max(diagnostics.MaxVolumeRelativeDifference, math.Abs(vol-d.Volume)/math.Max(1, d.Volume))
		}
	}
	return diagnostics, nil
}

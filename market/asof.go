package market

import (
	"fmt"
	"math"
)

type DailyContext struct {
	Direction     int     `json:"direction"`
	Center        float64 `json:"center"`
	Slope         float64 `json:"slope"`
	AvailableTime int64   `json:"available_time"`
	Ready         bool    `json:"ready"`
}

// AlignDaily supplies the last two daily centers available at CloseTime+1.
// Daily CloseTime+1 equal to the low decision time is included. Centers must
// have been computed causally from the same daily cache, with fixed startup.
// Candle rows have no symbol identity: the caller must verify cache identity.
func AlignDaily(low, daily []Candle, centers []float64) ([]DailyContext, error) {
	return alignDaily(low, daily, centers, false)
}

// AlignDailyInitialization permits unavailable D1 only in initialization;
// scoring callers must separately require full context warmup.
func AlignDailyInitialization(low, daily []Candle, centers []float64) ([]DailyContext, error) {
	return alignDaily(low, daily, centers, true)
}
func alignDaily(low, daily []Candle, centers []float64, initialization bool) ([]DailyContext, error) {
	const day int64 = 24 * 60 * 60 * 1000
	if len(low) == 0 {
		return nil, fmt.Errorf("empty low candle series")
	}
	interval := ""
	switch low[0].CloseTime - low[0].OpenTime + 1 {
	case 15 * 60 * 1000:
		interval = "15m"
	case 60 * 60 * 1000:
		interval = "1h"
	case 4 * 60 * 60 * 1000:
		interval = "4h"
	case 24 * 60 * 60 * 1000:
		interval = "1d"
	default:
		return nil, fmt.Errorf("unsupported low candle interval")
	}
	if _, err := ValidateCandles(low, interval); err != nil {
		return nil, fmt.Errorf("low context: %w", err)
	}
	if _, err := ValidateCandles(daily, "1d"); err != nil {
		return nil, fmt.Errorf("daily context: %w", err)
	}
	if len(centers) != len(daily) {
		return nil, fmt.Errorf("daily center count does not match candles")
	}
	first, last := low[0].CloseTime+1, low[len(low)-1].CloseTime+1
	// The last closed UTC day must exist at both coverage boundaries. A cache
	// whose first center is still warming up is valid; a missing day is not.
	if !initialization && first >= day && daily[0].CloseTime+1 > first/day*day {
		return nil, fmt.Errorf("daily cache misses first required closed day")
	}
	if daily[len(daily)-1].CloseTime+1 < last/day*day {
		return nil, fmt.Errorf("daily cache misses last required closed day")
	}
	out := make([]DailyContext, len(low))
	j := -1
	for i, c := range low {
		decision := c.CloseTime + 1
		for j+1 < len(daily) && daily[j+1].CloseTime+1 <= decision {
			j++
		}
		p := DailyContext{Center: math.NaN(), Slope: math.NaN()}
		if j >= 0 {
			p.Center, p.AvailableTime = centers[j], daily[j].CloseTime+1
			if j > 0 && finite(centers[j]) && finite(centers[j-1]) {
				p.Slope = centers[j] - centers[j-1]
				p.Ready = finite(p.Slope)
				if p.Ready && p.Slope > 0 {
					p.Direction = 1
				} else if p.Ready && p.Slope < 0 {
					p.Direction = -1
				}
			}
		}
		out[i] = p
	}
	return out, nil
}

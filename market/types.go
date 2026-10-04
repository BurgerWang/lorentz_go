// Package market reads public Binance USDT perpetual market data. All timestamps
// are Unix milliseconds; requested ranges are [start, end) in UTC.
package market

import (
	"fmt"
	"math"
)

type Candle struct {
	OpenTime  int64   `json:"open_time"`
	CloseTime int64   `json:"close_time"`
	Open      float64 `json:"open"`
	High      float64 `json:"high"`
	Low       float64 `json:"low"`
	Close     float64 `json:"close"`
	Volume    float64 `json:"volume"`
}

// FundingMarkKlineOpen8h identifies an estimated settlement mark from Binance's
// official eight-hour mark-price kline opening price.
const FundingMarkKlineOpen8h = "binance-mark-kline-open-8h"

type Funding struct {
	Time            int64   `json:"time"`
	Rate            float64 `json:"rate"`
	MarkPrice       float64 `json:"mark_price"`
	MarkPriceSource string  `json:"mark_price_source,omitempty"`
	// MarkPriceTime is the proxy kline bucket opening time, not an exact mark observation time.
	MarkPriceTime int64 `json:"mark_price_time,omitempty"`
}

type Quality struct {
	Rows         int   `json:"rows"`
	Start        int64 `json:"start"`
	End          int64 `json:"end"` // Exclusive: final close time plus one.
	Duplicates   int   `json:"duplicates"`
	Gaps         int   `json:"gaps"` // Number of missing bars between observed rows.
	OutOfOrder   int   `json:"out_of_order"`
	NonFinite    int   `json:"nonfinite"`
	BadOHLC      int   `json:"bad_ohlc"`
	BadVolume    int   `json:"bad_volume"`
	Misaligned   int   `json:"misaligned"`
	BadCloseTime int   `json:"bad_close_time"`
}

func IntervalMillis(interval string) (int64, error) {
	switch interval {
	case "15m":
		return 15 * 60 * 1000, nil
	case "1h":
		return 60 * 60 * 1000, nil
	case "4h":
		return 4 * 60 * 60 * 1000, nil
	case "1d":
		return 24 * 60 * 60 * 1000, nil
	default:
		return 0, fmt.Errorf("unsupported interval %q", interval)
	}
}

func finite(x float64) bool { return !math.IsNaN(x) && !math.IsInf(x, 0) }

// ValidateCandles reports defects without sorting, filling, or repairing data.
// Empty series are errors. It checks internal continuity, not listing dates or
// requested range coverage; callers must inspect Start/End for those boundaries.
func ValidateCandles(rows []Candle, interval string) (Quality, error) {
	q := Quality{Rows: len(rows)}
	d, err := IntervalMillis(interval)
	if err != nil {
		return q, err
	}
	if len(rows) == 0 {
		return q, fmt.Errorf("empty candle series")
	}
	q.Start = rows[0].OpenTime
	q.End = rows[len(rows)-1].CloseTime + 1
	seen := make(map[int64]bool, len(rows))
	for i, c := range rows {
		if seen[c.OpenTime] {
			q.Duplicates++
		}
		seen[c.OpenTime] = true
		if c.OpenTime < 0 || c.OpenTime%d != 0 {
			q.Misaligned++
		}
		if c.CloseTime != c.OpenTime+d-1 {
			q.BadCloseTime++
		}
		if i > 0 {
			delta := c.OpenTime - rows[i-1].OpenTime
			if delta < 0 {
				q.OutOfOrder++
			}
			if delta > d {
				q.Gaps += int((delta - 1) / d)
			}
		}
		if !finite(c.Open) || !finite(c.High) || !finite(c.Low) || !finite(c.Close) || !finite(c.Volume) {
			q.NonFinite++
		}
		if c.Open <= 0 || c.High <= 0 || c.Low <= 0 || c.Close <= 0 || c.High < c.Low || c.High < c.Open || c.High < c.Close || c.Low > c.Open || c.Low > c.Close {
			q.BadOHLC++
		}
		if c.Volume < 0 {
			q.BadVolume++
		}
	}
	if q.Duplicates+q.Gaps+q.OutOfOrder+q.NonFinite+q.BadOHLC+q.BadVolume+q.Misaligned+q.BadCloseTime > 0 {
		return q, fmt.Errorf("invalid candles: %+v", q)
	}
	return q, nil
}

// ValidateFunding checks values, ordering, and mark-price provenance. An empty
// source preserves legacy settlement marks and requires no proxy timestamp.
func ValidateFunding(rows []Funding) error {
	for i, r := range rows {
		if r.Time < 0 || !finite(r.Rate) || !finite(r.MarkPrice) || r.MarkPrice <= 0 {
			return fmt.Errorf("invalid funding row %d", i)
		}
		switch r.MarkPriceSource {
		case "":
			if r.MarkPriceTime != 0 {
				return fmt.Errorf("funding row %d: settlement mark must not have a proxy timestamp", i)
			}
		case FundingMarkKlineOpen8h:
			const bucketMillis int64 = 8 * 60 * 60 * 1000
			if r.MarkPriceTime != r.Time/bucketMillis*bucketMillis || r.Time-r.MarkPriceTime >= 60000 {
				return fmt.Errorf("funding row %d: proxy mark must be the same eight-hour bucket open within one minute of settlement", i)
			}
		default:
			return fmt.Errorf("funding row %d: unsupported mark price source %q", i, r.MarkPriceSource)
		}
		if i > 0 && r.Time <= rows[i-1].Time {
			return fmt.Errorf("funding timestamps duplicate or out of order at row %d", i)
		}
	}
	return nil
}

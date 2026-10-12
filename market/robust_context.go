package market

import "fmt"

// ValidateDailyOverlap validates only complete UTC days actually represented in
// the target cache. Older D1 initialization remains available without inventing
// target candles for it. The legacy all-days validator retains its contract.
func ValidateDailyOverlap(target, daily []Candle, interval string) (DailyAggregation, error) {
	if _, err := ValidateCandles(target, interval); err != nil {
		return DailyAggregation{}, err
	}
	if _, err := ValidateCandles(daily, "1d"); err != nil {
		return DailyAggregation{}, err
	}
	const day int64 = 86400000
	first := (target[0].OpenTime + day - 1) / day * day
	end := (target[len(target)-1].CloseTime + 1) / day * day
	var overlap []Candle
	for _, c := range daily {
		if c.OpenTime >= first && c.CloseTime < end {
			overlap = append(overlap, c)
		}
	}
	if end <= first || len(overlap) != int((end-first)/day) {
		return DailyAggregation{}, fmt.Errorf("missing complete overlapping D1 days")
	}
	if interval == "1d" {
		if len(target) != len(overlap) {
			return DailyAggregation{}, fmt.Errorf("daily target coverage mismatch")
		}
		for i := range target {
			if target[i] != overlap[i] {
				return DailyAggregation{}, fmt.Errorf("daily target differs at %d", i)
			}
		}
		return DailyAggregation{}, nil
	}
	return ValidateDailyAggregation(target, overlap, interval)
}

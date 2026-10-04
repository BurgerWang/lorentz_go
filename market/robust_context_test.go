package market

import "testing"

func TestDailySamePeriodCausalityAndLegacyStartup(t *testing.T) {
	const day int64 = 86400000
	d := []Candle{{0, day - 1, 100, 102, 99, 101, 1}, {day, 2*day - 1, 101, 103, 100, 102, 1}, {2 * day, 3*day - 1, 102, 104, 101, 103, 1}}
	c, e := AlignDaily(d, d, []float64{1, 2, 1000})
	if e != nil {
		t.Fatal(e)
	}
	if c[0].AvailableTime != day || c[1].Center != 2 || c[1].AvailableTime != 2*day || c[2].Direction != 1 {
		t.Fatalf("same-day context %+v", c)
	}
	if _, e = ValidateDailyOverlap(d, d, "1d"); e != nil {
		t.Fatal(e)
	}
	low := []Candle{{day, day + 3600000 - 1, 101, 102, 100, 101, 1}}
	if _, e = AlignDaily(low, d[1:], []float64{2, 1000}); e == nil {
		t.Fatal("legacy missing predecessor accepted")
	}
	causal, e := AlignDailyInitialization(low, d[1:], []float64{2, 1000})
	if e != nil {
		t.Fatal(e)
	}
	if causal[0].Ready || causal[0].AvailableTime != 0 {
		t.Fatalf("future D1 used %+v", causal)
	}
}

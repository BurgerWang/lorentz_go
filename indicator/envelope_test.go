package indicator

import (
	"lorentzgo/market"
	"math"
	"testing"
)

func TestEnvelopeIndependentFormula(t *testing.T) {
	// X=0 uses two observations, with weights 1 and (1+1/2)^-1.
	rows := []market.Candle{{High: 12, Low: 8, Close: 10}, {High: 15, Low: 9, Close: 12}, {High: 19, Low: 11, Close: 16}, {High: 24, Low: 18, Close: 20}}
	cfg := EnvelopeConfig{H: 1, R: 1, X: 0, ATRLength: 2, Near: 1.5, Far: 8}
	got, err := ComputeEnvelope(rows, cfg)
	if err != nil {
		t.Fatal(err)
	}
	if got[0].Ready || got[1].Ready || !got[2].Ready {
		t.Fatalf("wrong warmup: %+v", got)
	}
	w := 2.0 / 3
	weighted := func(a, b float64) float64 { return (a + w*b) / (1 + w) }
	h1, l1, c1 := weighted(15, 12), weighted(9, 8), weighted(12, 10)
	h2, l2, c2 := weighted(19, 15), weighted(11, 9), weighted(16, 12)
	tr1 := h1 - l1
	tr2 := math.Max(h2-l2, math.Max(math.Abs(h2-c1), math.Abs(l2-c1)))
	atr := (tr1 + tr2) / 2
	checks := []struct{ got, want float64 }{{got[2].Center, c2}, {got[2].High, h2}, {got[2].Low, l2}, {got[2].ATR, atr}, {got[2].NearUpper, c2 + 1.5*atr}, {got[2].AverageLower, c2 - 4.75*atr}, {got[2].FarUpper, c2 + 8*atr}, {got[2].Deviation, (16 - c2) / atr}}
	for _, check := range checks {
		if math.Abs(check.got-check.want) > 1e-12 {
			t.Fatalf("got %.16g want %.16g", check.got, check.want)
		}
	}
	h3, l3 := weighted(24, 19), weighted(18, 11)
	tr3 := math.Max(h3-l3, math.Max(math.Abs(h3-c2), math.Abs(l3-c2)))
	if math.Abs(got[3].ATR-(atr+tr3)/2) > 1e-12 {
		t.Fatal("RMA recurrence mismatch")
	}
}

func TestEnvelopePrefixAndUnavailable(t *testing.T) {
	cfg := DefaultEnvelopeConfig()
	rows := make([]market.Candle, 100)
	for i := range rows {
		x := 100 + float64(i)
		rows[i] = market.Candle{High: x + 2, Low: x - 2, Close: x}
	}
	all, _ := ComputeEnvelope(rows, cfg)
	short, _ := ComputeEnvelope(rows[:90], cfg)
	for i := range short {
		if short[i].Ready != all[i].Ready || (short[i].Ready && short[i] != all[i]) {
			t.Fatalf("prefix changed at %d", i)
		}
	}
	if all[84].Ready || !all[85].Ready {
		t.Fatal("default warmup must finish at index 85")
	}
	for i := range rows {
		rows[i] = market.Candle{High: 100, Low: 100, Close: 100}
	}
	flat, _ := ComputeEnvelope(rows, cfg)
	if flat[99].Ready || flat[99].ATR != 0 || !math.IsNaN(flat[99].Deviation) {
		t.Fatal("zero ATR must remain unavailable")
	}
	rows[99].Close = math.Inf(1)
	bad, _ := ComputeEnvelope(rows, cfg)
	if bad[99].Ready {
		t.Fatal("nonfinite kernel must remain unavailable")
	}
}

func TestEnvelopeValidationAndRQ(t *testing.T) {
	bad := []EnvelopeConfig{{H: 0, R: 8, X: 25, ATRLength: 60, Near: 1, Far: 2}, {H: 8, R: math.NaN(), X: 25, ATRLength: 60, Near: 1, Far: 2}, {H: 8, R: 8, X: 1001, ATRLength: 60, Near: 1, Far: 2}, {H: 8, R: 8, X: 25, ATRLength: 0, Near: 1, Far: 2}, {H: 8, R: 8, X: 25, ATRLength: 60, Near: 2, Far: 1}}
	for _, cfg := range bad {
		if _, err := ComputeEnvelope(nil, cfg); err == nil {
			t.Fatalf("accepted %+v", cfg)
		}
	}
	src := []float64{1, 2, 3, 4}
	got, err := RationalQuadratic(src, 1, 1, 0)
	if err != nil || !math.IsNaN(got[0]) || math.Abs(got[1]-1.6) > 1e-12 {
		t.Fatalf("unexpected RQ: %v %v", got, err)
	}
	if _, err := RationalQuadratic(src, 0, 1, 0); err == nil {
		t.Fatal("accepted invalid kernel")
	}
}

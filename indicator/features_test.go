package indicator

import (
	"lorentzgo/market"
	"math"
	"testing"
)

func near(t *testing.T, got, want float64) {
	t.Helper()
	if math.IsNaN(want) {
		if !math.IsNaN(got) {
			t.Fatalf("got %g, want na", got)
		}
		return
	}
	if math.IsNaN(got) || math.Abs(got-want) > 1e-10 {
		t.Fatalf("got %.15g, want %.15g", got, want)
	}
}

func TestKernelLibraryInclusiveOffsets(t *testing.T) {
	// x=0 still uses two bars. x=1 uses three, including offset two.
	src := []float64{10, 20, 40}
	rq := kernel(src, 2, 1, 1, false)
	ga := kernel(src, 2, 1, 1, true)
	near(t, rq[0], math.NaN())
	near(t, rq[1], math.NaN())
	near(t, rq[2], (40+20*(8.0/9)+10*(2.0/3))/(1+8.0/9+2.0/3))
	near(t, ga[2], (40+20*math.Exp(-1.0/8)+10*math.Exp(-0.5))/(1+math.Exp(-1.0/8)+math.Exp(-0.5)))
	near(t, kernel(src, 2, 1, 0, false)[1], (20+10*8.0/9)/(1+8.0/9))
	src[1] = math.NaN()
	near(t, kernel(src, 2, 1, 1, false)[2], math.NaN())
}

func TestLargeKernelBandwidthDoesNotIntegerOverflow(t *testing.T) {
	// On 64-bit systems this bandwidth squared overflowed to zero before conversion.
	h := int64(4294967296)
	if int64(int(h)) != h {
		t.Skip("requires 64-bit int")
	}
	near(t, kernel([]float64{10, 20, 40}, int(h), 1, 1, false)[2], 70.0/3)
}

func TestSmoothingSeedsAndMissing(t *testing.T) {
	src := []float64{math.NaN(), 2, 4, math.NaN(), 6, 8}
	e := ema(src, 3)
	r := rma(src, 3)
	for i, w := range []float64{math.NaN(), 2, 3, 3, 4.5, 6.25} {
		near(t, e[i], w)
	}
	for i, w := range []float64{math.NaN(), math.NaN(), math.NaN(), math.NaN(), 4, 16.0 / 3} {
		near(t, r[i], w)
	}
	// Gains 2,0,4; losses 0,1,0 seed to 2 and 1/3, hence RSI=600/7.
	values := rsi([]float64{10, 12, 11, 15, 13}, 3)
	near(t, values[2], math.NaN())
	near(t, values[3], 600.0/7)
	// Next Wilder averages: gain 4/3, loss 8/9; RSI=60.
	near(t, values[4], 60)
}

func TestCustomADXStartsAgainstZero(t *testing.T) {
	// First TR=11, plusDM=11, minusDM=0, DX=100.
	// Second Wilder sums TR=6.5, plus=5.5, minus=0, DX=100.
	// Third sums TR=6.25, plus=2.75, minus=2, DX=300/19.
	a := customADX([]float64{11, 11, 9}, []float64{10, 10, 8}, []float64{10.5, 10.5, 8.5}, 2)
	near(t, a[0], math.NaN())
	near(t, a[1], 100)
	near(t, a[2], 1100.0/19)
	// A completely flat zero series has 0/0 DX and never acquires a seed.
	for _, x := range customADX([]float64{0, 0, 0}, []float64{0, 0, 0}, []float64{0, 0, 0}, 2) {
		near(t, x, math.NaN())
	}
}

func TestNormalizerUsesCausalHistoricExtrema(t *testing.T) {
	src := []float64{math.NaN(), 4, 6, 5, 1, 100}
	want := []float64{math.NaN(), 0, 1, 0.5, 0, 1}
	all := normalize(src)
	for i := range src {
		near(t, all[i], want[i])
		near(t, normalize(src[:i+1])[i], all[i])
	}
}

func TestCCIUsesMeanAbsoluteDeviation(t *testing.T) {
	// For 1,2,6: mean=3, mean absolute deviation=2, CCI=100.
	values := cci([]float64{1, 2, 6, 2}, 3)
	near(t, values[1], math.NaN())
	near(t, values[2], 100)
	near(t, values[3], -50)
}

func TestUnseedableLongWindowsDoNotAllocateByPeriod(t *testing.T) {
	period := int(^uint(0) >> 1)
	for _, values := range [][]float64{sma([]float64{1, 2, 3}, period), cci([]float64{1, 2, 3}, period)} {
		for _, x := range values {
			near(t, x, math.NaN())
		}
	}
	c := DefaultConfig()
	c.SMAPeriod = period // Disabled, but Compute still calculates its input series.
	c.Features[4] = Feature{Name: "CCI", A: period, B: 1}
	c.FeatureCount = 4
	if _, err := Run(testCandles(50), c); err != nil {
		t.Fatal(err)
	}
}

func testCandles(n int) []market.Candle {
	out := make([]market.Candle, n)
	for i := range out {
		p := 100 + float64(i)*0.2 + math.Sin(float64(i)*0.3)*3
		out[i] = market.Candle{Open: p - 0.2, High: p + 1, Low: p - 1, Close: p, Volume: 10}
	}
	return out
}

func TestComputePrefixStabilityAndDisabledFilters(t *testing.T) {
	cfg := DefaultConfig()
	cfg.Source = "ohlc4"
	cfg.UseVolatilityFilter = false
	cfg.UseRegimeFilter = false
	cfg.UseADXFilter = false
	cfg.UseEMAFilter = false
	cfg.UseSMAFilter = false
	candles := testCandles(90)
	full, err := Compute(candles, cfg)
	if err != nil {
		t.Fatal(err)
	}
	for _, n := range []int{1, 10, 30, 60} {
		part, err := Compute(candles[:n], cfg)
		if err != nil {
			t.Fatal(err)
		}
		for i, v := range part {
			for j := range v.Features {
				near(t, v.Features[j], full[i].Features[j])
			}
			near(t, v.KernelRQ, full[i].KernelRQ)
			near(t, v.KernelGaussian, full[i].KernelGaussian)
			if !v.VolatilityOK || !v.RegimeOK || !v.ADXOK || !v.EMAUp || !v.EMADown || !v.SMAUp || !v.SMADown {
				t.Fatalf("disabled filters blocked bar %d", i)
			}
		}
	}
	near(t, full[0].Features[0], math.NaN())
	near(t, full[0].Features[1], math.NaN())
	near(t, full[0].Features[2], math.NaN())
	near(t, full[0].Features[3], math.NaN())
}

func TestComputeEnabledWarmupAndFeatureSources(t *testing.T) {
	cfg := DefaultConfig()
	cfg.UseADXFilter = true
	cfg.UseEMAFilter = true
	cfg.UseSMAFilter = true
	candles := testCandles(80)
	out, err := Compute(candles, cfg)
	if err != nil {
		t.Fatal(err)
	}
	if out[0].VolatilityOK || out[0].RegimeOK || out[0].ADXOK || out[0].EMAUp || out[0].EMADown || out[0].SMAUp || out[0].SMADown {
		t.Fatal("enabled filter passed missing/equal first values")
	}
	near(t, out[25].KernelRQ, math.NaN())
	if !finite(out[26].KernelRQ) {
		t.Fatal("27-bar kernel did not warm up")
	}
	cfg.Source = "hlc3"
	other, err := Compute(candles, cfg)
	if err != nil {
		t.Fatal(err)
	}
	for i := range out {
		for j := range out[i].Features {
			near(t, other[i].Features[j], out[i].Features[j])
		}
		if other[i].RegimeOK != out[i].RegimeOK {
			t.Fatal("regime depended on configurable source")
		}
	}
	cfg.KernelH = 0
	if _, err := Compute(candles, cfg); err == nil {
		t.Fatal("invalid config accepted")
	}
}

func TestComputeStrictFilterThresholds(t *testing.T) {
	candles := make([]market.Candle, 15)
	for i := range candles {
		p := 10 + float64(i)
		candles[i] = market.Candle{Open: p, High: p + 1, Low: p - 1, Close: p}
	}
	cfg := DefaultConfig()
	cfg.UseADXFilter = true
	cfg.ADXThreshold = 100
	cfg.RegimeThreshold = 0
	out, err := Compute(candles, cfg)
	if err != nil {
		t.Fatal(err)
	}
	if out[13].ADXOK {
		t.Fatal("ADX accepted equality at threshold 100")
	}
	if out[8].VolatilityOK || out[9].VolatilityOK {
		t.Fatal("ATR filter accepted warmup or equal ATRs")
	}
	if out[0].RegimeOK || out[1].RegimeOK || !out[2].RegimeOK {
		t.Fatal("regime slope seed differs from EMA-first-finite recurrence")
	}
	cfg.ADXThreshold = 99
	candles[14].High += 10
	out, err = Compute(candles, cfg)
	if err != nil {
		t.Fatal(err)
	}
	if !out[13].ADXOK || !out[14].VolatilityOK {
		t.Fatal("ADX/range expansion filters failed")
	}
}

// Copyright jdehorty. Pine-derived indicator logic is licensed under MPL-2.0.
package indicator

import (
	"lorentzgo/market"
	"math"
)

// Compute evaluates the imported Pine v2 libraries causally from the supplied
// history. Stateful normalizers start afresh at the first supplied candle.
func Compute(candles []market.Candle, cfg Config) ([]Inputs, error) {
	if err := cfg.Validate(); err != nil {
		return nil, err
	}
	n := len(candles)
	close, high, low, hlc3, ohlc4, src := make([]float64, n), make([]float64, n), make([]float64, n), make([]float64, n), make([]float64, n), make([]float64, n)
	for i, c := range candles {
		close[i], high[i], low[i] = safe(c.Close), safe(c.High), safe(c.Low)
		hlc3[i] = safe((high[i] + low[i] + close[i]) / 3)
		ohlc4[i] = safe((safe(c.Open) + high[i] + low[i] + close[i]) / 4)
		switch cfg.Source {
		case "hlc3":
			src[i] = hlc3[i]
		case "ohlc4":
			src[i] = ohlc4[i]
		default:
			src[i] = close[i]
		}
	}
	var features [5][]float64
	for j, f := range cfg.Features {
		switch f.Name {
		case "RSI":
			features[j] = ema(rsi(close, f.A), f.B)
			for i := range features[j] {
				features[j][i] /= 100
			}
		case "CCI":
			features[j] = normalize(ema(cci(close, f.A), f.B))
		case "WT":
			e1 := ema(hlc3, f.A)
			dev := nanSeries(n)
			for i := range dev {
				dev[i] = math.Abs(hlc3[i] - e1[i])
			}
			e2 := ema(dev, f.A)
			ci := nanSeries(n)
			for i := range ci {
				ci[i] = safe((hlc3[i] - e1[i]) / (0.015 * e2[i]))
			}
			wt1 := ema(ci, f.B)
			wt2 := sma(wt1, 4)
			for i := range wt1 {
				wt1[i] -= wt2[i]
			}
			features[j] = normalize(wt1)
		case "ADX":
			features[j] = customADX(high, low, close, f.A)
			for i := range features[j] {
				features[j][i] /= 100
			}
		}
	}
	tr := nanSeries(n)
	for i := range tr {
		if i == 0 || !finite(close[i-1]) {
			tr[i] = high[i] - low[i]
		} else {
			tr[i] = math.Max(high[i]-low[i], math.Max(math.Abs(high[i]-close[i-1]), math.Abs(low[i]-close[i-1])))
		}
	}
	atr1, atr10 := rma(tr, 1), rma(tr, 10)
	regime := regimeSlope(ohlc4, high, low)
	adx := customADX(high, low, src, 14)
	em, sm := ema(close, cfg.EMAPeriod), sma(close, cfg.SMAPeriod)
	rq, ga := kernel(src, cfg.KernelH, cfg.KernelR, cfg.KernelX, false), kernel(src, cfg.KernelH-cfg.KernelLag, cfg.KernelR, cfg.KernelX, true)
	out := make([]Inputs, n)
	for i := range out {
		out[i] = Inputs{Source: src[i], VolatilityOK: !cfg.UseVolatilityFilter || atr1[i] > atr10[i], RegimeOK: !cfg.UseRegimeFilter || regime[i] >= cfg.RegimeThreshold, ADXOK: !cfg.UseADXFilter || adx[i] > cfg.ADXThreshold,
			EMAUp: !cfg.UseEMAFilter || close[i] > em[i], EMADown: !cfg.UseEMAFilter || close[i] < em[i], SMAUp: !cfg.UseSMAFilter || close[i] > sm[i], SMADown: !cfg.UseSMAFilter || close[i] < sm[i], KernelRQ: rq[i], KernelGaussian: ga[i]}
		for j := range features {
			out[i].Features[j] = features[j][i]
		}
	}
	return out, nil
}

func nanSeries(n int) []float64 {
	a := make([]float64, n)
	for i := range a {
		a[i] = math.NaN()
	}
	return a
}
func safe(x float64) float64 {
	if !finite(x) {
		return math.NaN()
	}
	return x
}
func nz(x float64) float64 {
	if !finite(x) {
		return 0
	}
	return x
}

// Pine ta.ema seeds with the first non-na observation and ignores na inputs.
func ema(src []float64, length int) []float64 {
	out := nanSeries(len(src))
	state := math.NaN()
	a := 2.0 / (float64(length) + 1)
	for i, x := range src {
		if finite(x) {
			if !finite(state) {
				state = x
			} else {
				state = a*x + (1-a)*state
			}
		}
		out[i] = state
	}
	return out
}

// Pine ta.rma seeds with an SMA of length valid observations.
func rma(src []float64, length int) []float64 {
	out := nanSeries(len(src))
	state := math.NaN()
	sum, count := 0.0, 0
	a := 1.0 / float64(length)
	for i, x := range src {
		if finite(x) {
			if !finite(state) {
				sum += x
				count++
				if count == length {
					state = sum / float64(length)
				}
			} else {
				state = a*x + (1-a)*state
			}
		}
		out[i] = state
	}
	return out
}

// SMA/CCI ignore na observations; the window contains the latest length valid values.
func sma(src []float64, length int) []float64 {
	out := nanSeries(len(src))
	// No length-sized allocation is needed when history cannot seed the window.
	if length > len(src) {
		return out
	}
	ring := make([]float64, length)
	count, pos := 0, 0
	sum := 0.0
	for i, x := range src {
		if finite(x) {
			if count >= length {
				sum -= ring[pos]
			} else {
				count++
			}
			ring[pos] = x
			pos = (pos + 1) % length
			sum += x
		}
		if count == length {
			out[i] = sum / float64(length)
		}
	}
	return out
}
func rsi(src []float64, length int) []float64 {
	up, down := nanSeries(len(src)), nanSeries(len(src))
	prev := math.NaN()
	for i, x := range src {
		if !finite(x) {
			continue
		}
		if finite(prev) {
			d := x - prev
			up[i], down[i] = math.Max(d, 0), math.Max(-d, 0)
		}
		prev = x
	}
	u, d := rma(up, length), rma(down, length)
	out := nanSeries(len(src))
	for i := range out {
		if !finite(u[i]) || !finite(d[i]) {
			continue
		}
		out[i] = safe(100 - 100/(1+u[i]/d[i]))
		if d[i] == 0 && u[i] > 0 {
			out[i] = 100
		}
	}
	return out
}
func cci(src []float64, length int) []float64 {
	out := nanSeries(len(src))
	if length > len(src) {
		return out
	}
	ring := make([]float64, length)
	count, pos := 0, 0
	for i, x := range src {
		if !finite(x) {
			continue
		}
		ring[pos] = x
		pos = (pos + 1) % length
		if count < length {
			count++
		}
		if count < length {
			continue
		}
		mean := 0.0
		for _, v := range ring {
			mean += v
		}
		mean /= float64(length)
		dev := 0.0
		for _, v := range ring {
			dev += math.Abs(v - mean)
		}
		dev /= float64(length)
		out[i] = safe((x - mean) / (0.015 * dev))
	}
	return out
}
func normalize(src []float64) []float64 {
	out := nanSeries(len(src))
	lo, hi := 1e11, -1e11
	for i, x := range src {
		if !finite(x) {
			continue
		}
		lo = math.Min(lo, x)
		hi = math.Max(hi, x)
		out[i] = (x - lo) / math.Max(hi-lo, 1e-9)
	}
	return out
}

// Custom library ADX intentionally starts TR and positive DM against zero,
// then uses zero-seeded Wilder sums before the SMA-seeded DX RMA.
func customADX(high, low, src []float64, length int) []float64 {
	dx := nanSeries(len(src))
	trSum, plusSum, minusSum := 0.0, 0.0, 0.0
	for i := range src {
		ph, pl, pc := 0.0, 0.0, 0.0
		if i > 0 {
			ph, pl, pc = nz(high[i-1]), nz(low[i-1]), nz(src[i-1])
		}
		tr := math.Max(high[i]-low[i], math.Max(math.Abs(high[i]-pc), math.Abs(low[i]-pc)))
		up, down := high[i]-ph, pl-low[i]
		plus, minus := 0.0, 0.0
		if up > down {
			plus = math.Max(up, 0)
		}
		if down > up {
			minus = math.Max(down, 0)
		}
		trSum = nz(trSum) - nz(trSum)/float64(length) + tr
		plusSum = nz(plusSum) - nz(plusSum)/float64(length) + plus
		minusSum = nz(minusSum) - nz(minusSum)/float64(length) + minus
		dp, dm := plusSum/trSum*100, minusSum/trSum*100
		dx[i] = safe(math.Abs(dp-dm) / (dp + dm) * 100)
	}
	return rma(dx, length)
}
func regimeSlope(src, high, low []float64) []float64 {
	slopes := nanSeries(len(src))
	v1, v2, klmf := 0.0, 0.0, math.NaN()
	for i, x := range src {
		prev := math.NaN()
		if i > 0 {
			prev = src[i-1]
		}
		v1 = 0.2*(x-prev) + 0.8*nz(v1)
		v2 = 0.1*(high[i]-low[i]) + 0.8*nz(v2)
		omega := math.Abs(v1 / v2)
		o2 := omega * omega
		alpha := (-o2 + math.Sqrt(o2*o2+16*o2)) / 8
		next := safe(alpha*x + (1-alpha)*nz(klmf))
		slopes[i] = math.Abs(next - klmf)
		klmf = next
	}
	avg := ema(slopes, 200)
	for i := range slopes {
		slopes[i] = safe((slopes[i] - avg[i]) / avg[i])
	}
	return slopes
}
func kernel(src []float64, h int, r float64, x int, gaussian bool) []float64 {
	out := nanSeries(len(src))
	weights := make([]float64, x+2)
	total := 0.0
	for i := range weights {
		q := float64(i) * float64(i) / (2 * float64(h) * float64(h))
		w := math.Pow(1+q/r, -r)
		if gaussian {
			w = math.Exp(-q)
		}
		weights[i] = w
		total += w
	}
	for i := x + 1; i < len(src); i++ {
		sum := 0.0
		for offset, w := range weights {
			sum += src[i-offset] * w
		}
		out[i] = safe(sum / total)
	}
	return out
}

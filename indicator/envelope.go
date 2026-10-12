// Copyright jdehorty. Pine-derived indicator logic is licensed under MPL-2.0.
package indicator

import (
	"fmt"
	"lorentzgo/market"
	"math"
)

// EnvelopeConfig keeps envelope parameters independent of the main classifier.
type EnvelopeConfig struct {
	H         int     `json:"h"`
	R         float64 `json:"r"`
	X         int     `json:"x"`
	ATRLength int     `json:"atr_length"`
	Near      float64 `json:"near"`
	Far       float64 `json:"far"`
}

func DefaultEnvelopeConfig() EnvelopeConfig {
	return EnvelopeConfig{H: 8, R: 8, X: 25, ATRLength: 60, Near: 1.5, Far: 8}
}

func (c EnvelopeConfig) Validate() error {
	if c.H < 1 || c.X < 0 || c.X > 1000 || !finite(c.R) || c.R <= 0 || c.ATRLength < 1 || !finite(c.Near) || !finite(c.Far) || c.Near <= 0 || c.Far < c.Near {
		return fmt.Errorf("invalid kernel envelope configuration")
	}
	return nil
}

// RationalQuadratic exposes the same causal kernel used by Classic, without
// calculating classifier features. Its first X+1 values are unavailable.
func RationalQuadratic(src []float64, h int, r float64, x int) ([]float64, error) {
	c := DefaultEnvelopeConfig()
	c.H, c.R, c.X = h, r, x
	if err := c.Validate(); err != nil {
		return nil, err
	}
	return kernel(src, h, r, x, false), nil
}

type EnvelopePoint struct {
	Center       float64 `json:"center"`
	High         float64 `json:"high"`
	Low          float64 `json:"low"`
	ATR          float64 `json:"atr"`
	NearUpper    float64 `json:"near_upper"`
	NearLower    float64 `json:"near_lower"`
	AverageUpper float64 `json:"average_upper"`
	AverageLower float64 `json:"average_lower"`
	FarUpper     float64 `json:"far_upper"`
	FarLower     float64 `json:"far_lower"`
	Deviation    float64 `json:"deviation"`
	Ready        bool    `json:"ready"`
}

// ComputeEnvelope applies RQ independently to H/L/C, then SMA-seeded Wilder
// smoothing to their true range. Deviation is (raw close - center) / ATR.
// Missing kernel history and zero/nonfinite ATR never produce a ready point.
func ComputeEnvelope(candles []market.Candle, cfg EnvelopeConfig) ([]EnvelopePoint, error) {
	if err := cfg.Validate(); err != nil {
		return nil, err
	}
	n := len(candles)
	high, low, close := nanSeries(n), nanSeries(n), nanSeries(n)
	for i, c := range candles {
		high[i], low[i], close[i] = safe(c.High), safe(c.Low), safe(c.Close)
	}
	kh, kl, kc := kernel(high, cfg.H, cfg.R, cfg.X, false), kernel(low, cfg.H, cfg.R, cfg.X, false), kernel(close, cfg.H, cfg.R, cfg.X, false)
	tr := nanSeries(n)
	for i := range tr {
		if !finite(kh[i]) || !finite(kl[i]) || !finite(kc[i]) {
			continue
		}
		tr[i] = kh[i] - kl[i]
		if i > 0 && finite(kc[i-1]) {
			tr[i] = math.Max(tr[i], math.Max(math.Abs(kh[i]-kc[i-1]), math.Abs(kl[i]-kc[i-1])))
		}
	}
	atr := rma(tr, cfg.ATRLength)
	out := make([]EnvelopePoint, n)
	for i := range out {
		p := EnvelopePoint{Center: kc[i], High: kh[i], Low: kl[i], ATR: atr[i], NearUpper: math.NaN(), NearLower: math.NaN(), AverageUpper: math.NaN(), AverageLower: math.NaN(), FarUpper: math.NaN(), FarLower: math.NaN(), Deviation: math.NaN()}
		if finite(kc[i]) && finite(kh[i]) && finite(kl[i]) && finite(atr[i]) && atr[i] > 0 && finite(close[i]) {
			p.NearUpper, p.NearLower = kc[i]+cfg.Near*atr[i], kc[i]-cfg.Near*atr[i]
			p.FarUpper, p.FarLower = kc[i]+cfg.Far*atr[i], kc[i]-cfg.Far*atr[i]
			p.AverageUpper, p.AverageLower = kc[i]+((cfg.Near+cfg.Far)/2)*atr[i], kc[i]-((cfg.Near+cfg.Far)/2)*atr[i]
			p.Deviation = (close[i] - kc[i]) / atr[i]
			p.Ready = finite(p.NearUpper) && finite(p.NearLower) && finite(p.AverageUpper) && finite(p.AverageLower) && finite(p.FarUpper) && finite(p.FarLower) && finite(p.Deviation)
		}
		out[i] = p
	}
	return out, nil
}

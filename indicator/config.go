// Copyright jdehorty. Pine-derived indicator logic is licensed under MPL-2.0.
package indicator

import (
	"fmt"
	"math"
)

type Feature struct {
	Name string `json:"name"`
	A    int    `json:"a"`
	B    int    `json:"b"`
}

type Config struct {
	Algorithm           string     `json:"algorithm"` // original-chart, original-online, or aligned-knn
	Source              string     `json:"source"`
	Neighbors           int        `json:"neighbors"`
	MaxBarsBack         int        `json:"max_bars_back"`
	FeatureCount        int        `json:"feature_count"`
	Features            [5]Feature `json:"features"`
	IncludeFullHistory  bool       `json:"include_full_history"`
	UseVolatilityFilter bool       `json:"use_volatility_filter"`
	UseRegimeFilter     bool       `json:"use_regime_filter"`
	UseADXFilter        bool       `json:"use_adx_filter"`
	RegimeThreshold     float64    `json:"regime_threshold"`
	ADXThreshold        float64    `json:"adx_threshold"`
	UseEMAFilter        bool       `json:"use_ema_filter"`
	EMAPeriod           int        `json:"ema_period"`
	UseSMAFilter        bool       `json:"use_sma_filter"`
	SMAPeriod           int        `json:"sma_period"`
	UseKernelFilter     bool       `json:"use_kernel_filter"`
	KernelSmoothing     bool       `json:"kernel_smoothing"`
	KernelH             int        `json:"kernel_h"`
	KernelR             float64    `json:"kernel_r"`
	KernelX             int        `json:"kernel_x"`
	KernelLag           int        `json:"kernel_lag"`
	UseDynamicExits     bool       `json:"use_dynamic_exits"`
	MinVoteFraction     float64    `json:"min_vote_fraction"`
	SampleStride        int        `json:"sample_stride"`
}

func DefaultConfig() Config {
	return Config{
		Algorithm: "original-chart", Source: "close", Neighbors: 8, MaxBarsBack: 2000, FeatureCount: 5,
		Features:            [5]Feature{{"RSI", 14, 1}, {"WT", 10, 11}, {"CCI", 20, 1}, {"ADX", 20, 2}, {"RSI", 9, 1}},
		UseVolatilityFilter: true, UseRegimeFilter: true, RegimeThreshold: -0.1, ADXThreshold: 20,
		EMAPeriod: 200, SMAPeriod: 200, UseKernelFilter: true, KernelH: 8, KernelR: 8, KernelX: 25, KernelLag: 2,
		SampleStride: 4,
	}
}

func (c Config) Validate() error {
	if c.Algorithm != "original-chart" && c.Algorithm != "original-online" && c.Algorithm != "aligned-knn" {
		return fmt.Errorf("unsupported algorithm %q", c.Algorithm)
	}
	if c.Source != "close" && c.Source != "hlc3" && c.Source != "ohlc4" {
		return fmt.Errorf("unsupported source %q", c.Source)
	}
	if c.Neighbors < 1 || c.Neighbors > 100 || c.MaxBarsBack < 4 || c.FeatureCount < 2 || c.FeatureCount > 5 {
		return fmt.Errorf("invalid neighbor/window/feature configuration")
	}
	for _, f := range c.Features {
		if f.A < 1 || f.B < 1 || (f.Name != "RSI" && f.Name != "WT" && f.Name != "CCI" && f.Name != "ADX") {
			return fmt.Errorf("invalid feature %+v", f)
		}
	}
	if c.EMAPeriod < 1 || c.SMAPeriod < 1 || c.KernelH < 3 || c.KernelLag < 1 || c.KernelLag >= c.KernelH || c.KernelX < 0 || c.KernelX > 1000 || !finite(c.KernelR) || c.KernelR <= 0 {
		return fmt.Errorf("invalid smoothing/kernel configuration")
	}
	if !finite(c.MinVoteFraction) || c.MinVoteFraction < 0 || c.MinVoteFraction > 1 || c.SampleStride < 1 || !finite(c.RegimeThreshold) || !finite(c.ADXThreshold) {
		return fmt.Errorf("invalid threshold/stride")
	}
	return nil
}

func finite(v float64) bool { return !math.IsNaN(v) && !math.IsInf(v, 0) }

// Inputs are causal feature/filter values at a closed candle. NaN represents Pine na.
type Inputs struct {
	Source                         float64
	Features                       [5]float64
	VolatilityOK, RegimeOK, ADXOK  bool
	EMAUp, EMADown, SMAUp, SMADown bool
	KernelRQ, KernelGaussian       float64
}

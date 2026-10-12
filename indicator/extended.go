// Copyright jdehorty. Pine-derived indicator logic is licensed under MPL-2.0.
package indicator

import (
	"fmt"
	"lorentzgo/market"
	"math"
	"strings"
)

// FeatureSpec separates Classic's unchanged normalization from the causal
// rolling standardization used by the new OHLCV/context inputs.
type FeatureSpec struct {
	Name          string `json:"name"`
	A             int    `json:"a"`
	B             int    `json:"b"`
	Normalization string `json:"normalization"`
	Window        int    `json:"window"`
}

func (f FeatureSpec) Validate() error {
	switch f.Name {
	case "RSI", "WT", "CCI", "ADX":
		if f.A < 1 || f.B < 1 || f.Normalization != "legacy" || f.Window != 0 {
			return fmt.Errorf("invalid legacy feature %+v", f)
		}
	case "RVOL", "ATR_PRICE", "KERNEL_DEVIATION", "D1_SLOPE":
		if f.Normalization != "rolling-z" || f.Window < 2 || f.Window > 10000 || f.B != 1 || f.A < 1 || f.A > 10000 {
			return fmt.Errorf("invalid extended feature %+v", f)
		}
		if (f.Name == "KERNEL_DEVIATION" || f.Name == "D1_SLOPE") && f.A != 1 {
			return fmt.Errorf("feature %s has no active A parameter; use 1", f.Name)
		}
	default:
		return fmt.Errorf("unsupported feature %q", f.Name)
	}
	return nil
}

type NamedFeatureGroup struct {
	Name     string        `json:"name"`
	Features []FeatureSpec `json:"features"`
}

func (g NamedFeatureGroup) Validate() error {
	if strings.TrimSpace(g.Name) == "" {
		return fmt.Errorf("feature group name is empty")
	}
	return ValidateFeatureSpecs(g.Features)
}

// ValidateFeatureSpecs preserves order and permits differently parameterized
// copies of one family (including Classic's RSI14 and RSI9).
func ValidateFeatureSpecs(specs []FeatureSpec) error {
	if len(specs) < 2 || len(specs) > 15 {
		return fmt.Errorf("feature vector requires 2..15 ordered features")
	}
	seen := make(map[FeatureSpec]bool, len(specs))
	for _, f := range specs {
		if err := f.Validate(); err != nil {
			return err
		}
		if seen[f] {
			return fmt.Errorf("duplicate feature %+v", f)
		}
		seen[f] = true
	}
	return nil
}

// ComputeExtended reuses Classic's features and filters. Context slices are
// already aligned to rows; a required missing/mismatched slice is an error.
// Each standardized historical value is fixed at that candle's close.
func ComputeExtended(rows []market.Candle, cfg Config, specs []FeatureSpec, envelope []EnvelopePoint, daily []market.DailyContext) ([]Inputs, [][]float64, error) {
	if err := ValidateFeatureSpecs(specs); err != nil {
		return nil, nil, err
	}
	for _, f := range specs {
		if f.Name == "KERNEL_DEVIATION" && len(envelope) != len(rows) {
			return nil, nil, fmt.Errorf("kernel deviation requires one envelope point per candle")
		}
		if f.Name == "D1_SLOPE" && len(daily) != len(rows) {
			return nil, nil, fmt.Errorf("daily slope requires one aligned daily context per candle")
		}
	}
	x, err := Compute(rows, cfg)
	if err != nil {
		return nil, nil, err
	}
	legacy := make(map[Feature][]float64)
	for slot, f := range cfg.Features {
		series := make([]float64, len(rows))
		for i := range x {
			series[i] = x[i].Features[slot]
		}
		legacy[f] = series
	}
	var missing []Feature
	for _, f := range specs {
		if f.Normalization == "legacy" {
			key := Feature{f.Name, f.A, f.B}
			if _, ok := legacy[key]; !ok {
				missing = append(missing, key)
			}
		}
	}
	// Reuse Compute's original formulas in five-slot batches. This keeps a
	// single source of truth for Pine initialization and cumulative scaling.
	for start := 0; start < len(missing); start += 5 {
		end := min(start+5, len(missing))
		batch := cfg
		for j := start; j < end; j++ {
			batch.Features[j-start] = missing[j]
		}
		values, err := Compute(rows, batch)
		if err != nil {
			return nil, nil, err
		}
		for j := start; j < end; j++ {
			series := make([]float64, len(rows))
			for i := range values {
				series[i] = values[i].Features[j-start]
			}
			legacy[missing[j]] = series
		}
	}
	vectors := make([][]float64, len(rows))
	for i := range vectors {
		vectors[i] = make([]float64, len(specs))
	}
	for j, f := range specs {
		var series []float64
		if f.Normalization == "legacy" {
			series = legacy[Feature{f.Name, f.A, f.B}]
		} else {
			series = rollingZ(extendedRaw(rows, f, envelope, daily), f.Window)
		}
		for i := range vectors {
			vectors[i][j] = series[i]
		}
	}
	return x, vectors, nil
}

func extendedRaw(rows []market.Candle, f FeatureSpec, envelope []EnvelopePoint, daily []market.DailyContext) []float64 {
	out := nanSeries(len(rows))
	switch f.Name {
	case "RVOL":
		// The denominator contains preceding closed bars, excluding the
		// current bar. Rebuild this bounded window directly: subtracting a
		// departing large volume from a sliding total can permanently erase
		// the normal volumes that remain. Divide before summing to avoid an
		// overflowing intermediate total for a finite mean.
		for i := f.A; i < len(rows); i++ {
			if !finite(rows[i].Volume) || rows[i].Volume < 0 {
				continue
			}
			mean, valid := 0.0, true
			for j := i - f.A; j < i; j++ {
				volume := rows[j].Volume
				if !finite(volume) || volume < 0 {
					valid = false
					break
				}
				mean += volume / float64(f.A)
			}
			if valid && finite(mean) && mean > 0 {
				out[i] = safe(rows[i].Volume / mean)
			}
		}
	case "ATR_PRICE":
		tr := nanSeries(len(rows))
		for i, c := range rows {
			if !finite(c.High) || !finite(c.Low) || !finite(c.Close) || c.High < c.Low || c.Close <= 0 {
				continue
			}
			if i == 0 {
				tr[i] = c.High - c.Low
			} else if finite(rows[i-1].Close) && rows[i-1].Close > 0 {
				tr[i] = math.Max(c.High-c.Low, math.Max(math.Abs(c.High-rows[i-1].Close), math.Abs(c.Low-rows[i-1].Close)))
			}
		}
		atr := rma(tr, f.A)
		for i, c := range rows {
			if finite(tr[i]) && finite(atr[i]) && c.Close > 0 {
				out[i] = safe(atr[i] / c.Close)
			}
		}
	case "KERNEL_DEVIATION":
		for i, p := range envelope {
			if p.Ready && finite(p.Center) && finite(p.ATR) && p.ATR > 0 && finite(rows[i].Close) {
				out[i] = safe((rows[i].Close - p.Center) / p.ATR)
			}
		}
	case "D1_SLOPE":
		for i, p := range daily {
			if p.Ready && p.AvailableTime > 0 && rows[i].CloseTime < math.MaxInt64 && p.AvailableTime <= rows[i].CloseTime+1 && finite(p.Slope) && finite(p.Center) && p.Center > 0 {
				out[i] = safe(p.Slope / p.Center)
			}
		}
	}
	return out
}

// rollingZ uses the latest window bars (including current), requiring all
// observations finite. Population standard deviation is explicit. Sliding
// Welford moments avoid subtracting large squared sums; periodic rebuilding
// bounds accumulated roundoff without revising already emitted samples.
func rollingZ(src []float64, window int) []float64 {
	out := nanSeries(len(src))
	if window > len(src) {
		return out
	}
	count, mean, m2 := 0, 0.0, 0.0
	counts := make(map[float64]int, window)
	add := func(v float64) {
		if !finite(v) {
			return
		}
		count++
		delta := v - mean
		mean += delta / float64(count)
		m2 += delta * (v - mean)
		counts[v]++
	}
	for i, v := range src {
		rebuild := false
		if i >= window {
			old := src[i-window]
			if finite(old) {
				counts[old]--
				if counts[old] == 0 {
					delete(counts, old)
				}
				if count == 1 {
					count, mean, m2 = 0, 0, 0
				} else {
					nextMean := mean - (old-mean)/float64(count-1)
					previousM2 := m2
					m2 -= (old - mean) * (old - nextMean)
					// When an outlier leaves, subtraction can lose most of
					// the remaining variance; rebuild this window directly.
					rebuild = previousM2 > 0 && m2 < previousM2*1e-8
					mean, count = nextMean, count-1
				}
			}
		}
		add(v)
		if i+1 < window || count != window {
			continue
		}
		if len(counts) == 1 {
			mean, m2 = v, 0
			out[i] = 0
			continue
		}
		if rebuild || (i+1)%window == 0 || m2 <= 0 || !finite(m2) || !finite(mean) {
			count, mean, m2 = 0, 0, 0
			clear(counts)
			for _, value := range src[i-window+1 : i+1] {
				add(value)
			}
		}
		if m2 > 0 {
			out[i] = safe((v - mean) / math.Sqrt(m2/float64(window)))
		}
	}
	return out
}

// RunExtendedInputs injects vector distances into the original Classic-online
// FIFO/labels/state path. Aligned models are a separate later-stage contract.
func RunExtendedInputs(inputs []Inputs, cfg Config, vectors [][]float64) ([]Point, error) {
	if err := cfg.Validate(); err != nil {
		return nil, err
	}
	if cfg.Algorithm != "original-online" {
		return nil, fmt.Errorf("extended vectors currently require original-online")
	}
	ready, err := vectorReadiness(inputs, vectors)
	if err != nil {
		return nil, err
	}
	metric := func(t, j int) float64 {
		if !ready[t] || !ready[j] {
			return math.NaN()
		}
		d := 0.0
		for k, value := range vectors[t] {
			d += math.Log(1 + math.Abs(value-vectors[j][k]))
		}
		return safe(d)
	}
	return runInputs(inputs, cfg, metric, func(t int) bool { return ready[t] }, nil), nil
}

func vectorReadiness(inputs []Inputs, vectors [][]float64) ([]bool, error) {
	if len(vectors) != len(inputs) {
		return nil, fmt.Errorf("vector count does not match inputs")
	}
	width := 0
	ready := make([]bool, len(inputs))
	for i, vector := range vectors {
		if i == 0 {
			width = len(vector)
		}
		if len(vector) != width || width < 2 || width > 15 {
			return nil, fmt.Errorf("vector %d must have the same 2..15 dimensions", i)
		}
		ready[i] = finite(inputs[i].Source)
		for _, value := range vector {
			ready[i] = ready[i] && finite(value)
		}
	}
	return ready, nil
}

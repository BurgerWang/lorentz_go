// Copyright jdehorty. Pine-derived indicator logic is licensed under MPL-2.0.
package indicator

import (
	"lorentzgo/market"
	"math"
)

// Point preserves distinct simultaneous events rather than the lossy Pine backtest stream.
type Point struct {
	Time             int64 `json:"time"`
	Prediction       int   `json:"prediction"`
	Signal           int   `json:"signal"`
	BarsHeld         int   `json:"bars_held"`
	EarlyFlip        bool  `json:"early_flip"`
	StartLong        bool  `json:"start_long"`
	StartShort       bool  `json:"start_short"`
	EndLong          bool  `json:"end_long"`
	EndShort         bool  `json:"end_short"`
	Neighbors        int   `json:"neighbors"`
	MaxNeighborIndex int   `json:"max_neighbor_index"`
	SearchStart      int   `json:"search_start"`
	SearchEnd        int   `json:"search_end"`
	FilterOK         bool  `json:"filter_ok"`
}

func Run(candles []market.Candle, c Config) ([]Point, error) {
	x, err := Compute(candles, c)
	if err != nil {
		return nil, err
	}
	out, err := RunInputs(x, c)
	if err != nil {
		return nil, err
	}
	for i := range out {
		out[i].Time = candles[i].CloseTime
	}
	return out, nil
}

// RunInputs exposes the classification/state path for independent reference fixtures.
func RunInputs(x []Inputs, c Config) ([]Point, error) {
	if err := c.Validate(); err != nil {
		return nil, err
	}
	return runInputs(x, c, func(t, j int) float64 {
		return distance(x[t].Features, x[j].Features, c.FeatureCount)
	}, nil, nil), nil
}

type classification struct {
	prediction, neighbors, maxIndex, direction int
}

// A supplied readiness gate acts like a Classic filter hold: unavailable input
// cannot change the signal, while held-bar counters and exits keep advancing.
func runInputs(x []Inputs, c Config, metric func(t, j int) float64, ready func(t int) bool, classify func(t int) classification) []Point {
	out := make([]Point, len(x))
	labels := make([]int, len(x))
	queueLabels := make([]int, 0, c.Neighbors+1)
	queueDistances := make([]float64, 0, c.Neighbors+1)
	queueIndices := make([]int, 0, c.Neighbors+1)
	lastLong, lastShort, lastBull, lastBear := -1, -1, -1, -1
	validLongPrev, validShortPrev := false, false
	signal, held, prediction := 0, 0, 0
	fixedGate := max(0, len(x)-1-c.MaxBarsBack)
	for t, v := range x {
		p := Point{MaxNeighborIndex: -1, SearchStart: -1, SearchEnd: -1}
		direction := 0
		if t >= 4 {
			labels[t] = -sign(v.Source - x[t-4].Source)
		}
		if c.Algorithm == "aligned-knn" {
			if classify == nil {
				prediction, p.Neighbors, p.MaxNeighborIndex = nearestVote(x, t, c)
				direction = sign(float64(prediction))
			} else {
				result := classify(t)
				prediction, p.Neighbors, p.MaxNeighborIndex, direction = result.prediction, result.neighbors, result.maxIndex, result.direction
			}
			p.SearchStart = max(0, t-c.MaxBarsBack)
			p.SearchEnd = t - 4
		} else {
			gate := fixedGate
			if c.Algorithm == "original-online" {
				gate = max(0, t-c.MaxBarsBack)
			}
			if t >= gate {
				start := gate
				if c.IncludeFullHistory {
					start = 0
				}
				end := min(c.MaxBarsBack-1, t)
				p.SearchStart, p.SearchEnd = start, end
				step := 1
				if start > end {
					step = -1
				}
				lastDistance := -1.0
				for j := start; ; j += step {
					d := metric(t, j)
					if d >= lastDistance && j%4 != 0 {
						lastDistance = d
						queueDistances = append(queueDistances, d)
						queueLabels = append(queueLabels, labels[j])
						queueIndices = append(queueIndices, j)
						if len(queueLabels) > c.Neighbors {
							lastDistance = queueDistances[int(math.Round(float64(c.Neighbors)*3/4))]
							queueDistances = queueDistances[1:]
							queueLabels = queueLabels[1:]
							queueIndices = queueIndices[1:]
						}
					}
					if j == end {
						break
					}
				}
				prediction = 0
				for _, label := range queueLabels {
					prediction += label
				}
			}
			p.Neighbors = len(queueLabels)
			for _, j := range queueIndices {
				p.MaxNeighborIndex = max(p.MaxNeighborIndex, j)
			}
			direction = sign(float64(prediction))
		}
		p.FilterOK = v.VolatilityOK && v.RegimeOK && v.ADXOK
		prevSignal := signal
		if p.FilterOK && (ready == nil || ready(t)) {
			if direction != 0 {
				signal = direction
			}
		}
		changed := signal != prevSignal
		if changed {
			held = 0
		} else {
			held++
		}
		p.Prediction, p.Signal, p.BarsHeld = prediction, signal, held
		if changed {
			for j := max(1, t-3); j < t; j++ {
				if out[j].Signal != out[j-1].Signal {
					p.EarlyFlip = true
				}
			}
		}
		newBuy := changed && signal == 1 && v.EMAUp && v.SMAUp
		newSell := changed && signal == -1 && v.EMADown && v.SMADown
		bullRate, bearRate, bullChange, bearChange := false, false, false, false
		bullCross, bearCross := false, false
		if t >= 1 {
			bullRate = x[t-1].KernelRQ < v.KernelRQ
			bearRate = x[t-1].KernelRQ > v.KernelRQ
			bullCross = v.KernelGaussian > v.KernelRQ && x[t-1].KernelGaussian <= x[t-1].KernelRQ
			bearCross = v.KernelGaussian < v.KernelRQ && x[t-1].KernelGaussian >= x[t-1].KernelRQ
		}
		if t >= 2 {
			bullChange = bullRate && x[t-2].KernelRQ > x[t-1].KernelRQ
			bearChange = bearRate && x[t-2].KernelRQ < x[t-1].KernelRQ
		}
		bull, bear := true, true
		if c.UseKernelFilter {
			bull, bear = bullRate, bearRate
			if c.KernelSmoothing {
				bull = v.KernelGaussian >= v.KernelRQ
				bear = v.KernelGaussian <= v.KernelRQ
			}
		}
		p.StartLong = newBuy && bull
		p.StartShort = newSell && bear
		if p.StartLong {
			lastLong = t
		}
		if p.StartShort {
			lastShort = t
		}
		alertBull, alertBear := bullChange, bearChange
		if c.KernelSmoothing {
			alertBull, alertBear = bullCross, bearCross
		}
		if alertBull {
			lastBull = t
		}
		if alertBear {
			lastBear = t
		}
		validLong := lastLong >= 0 && lastBear >= 0 && lastBear < lastLong
		validShort := lastShort >= 0 && lastBull >= 0 && lastBull < lastShort
		if c.UseDynamicExits && !c.UseEMAFilter && !c.UseSMAFilter && !c.KernelSmoothing {
			p.EndLong = bearChange && validLongPrev
			p.EndShort = bullChange && validShortPrev
		} else if t >= 4 {
			lastBuy := out[t-4].Signal == 1 && x[t-4].EMAUp && x[t-4].SMAUp
			lastSell := out[t-4].Signal == -1 && x[t-4].EMADown && x[t-4].SMADown
			p.EndLong = ((held == 4 && lastBuy) || (held > 0 && held < 4 && newSell && lastBuy)) && out[t-4].StartLong
			p.EndShort = ((held == 4 && lastSell) || (held > 0 && held < 4 && newBuy && lastSell)) && out[t-4].StartShort
		}
		validLongPrev, validShortPrev = validLong, validShort
		out[t] = p
	}
	return out
}

func sign(v float64) int {
	if v > 0 {
		return 1
	}
	if v < 0 {
		return -1
	}
	return 0
}
func distance(a, b [5]float64, n int) float64 {
	d := 0.0
	for j := 0; j < n; j++ {
		d += math.Log(1 + math.Abs(a[j]-b[j]))
	}
	return d
}

// A small sorted top-k buffer avoids sorting the full history. Equal distances
// prefer older indices deterministically. Only labels matured at t are eligible.
func nearestVote(x []Inputs, t int, c Config) (vote, count, maxIndex int) {
	type neighbor struct {
		d            float64
		index, label int
	}
	best := make([]neighbor, 0, c.Neighbors+1)
	maxIndex = -1
	start := max(0, t-c.MaxBarsBack)
	// Anchor sampling to absolute indices; it never depends on the future endpoint.
	start += ((c.SampleStride - start%c.SampleStride) % c.SampleStride)
	for j := start; j <= t-4; j += c.SampleStride {
		d := distance(x[t].Features, x[j].Features, c.FeatureCount)
		if !finite(d) || !finite(x[j].Source) || !finite(x[j+4].Source) {
			continue
		}
		if len(best) == c.Neighbors && d >= best[len(best)-1].d {
			continue
		}
		n := neighbor{d, j, sign(x[j+4].Source - x[j].Source)}
		pos := len(best)
		for pos > 0 && d < best[pos-1].d {
			pos--
		}
		best = append(best, neighbor{})
		copy(best[pos+1:], best[pos:])
		best[pos] = n
		if len(best) > c.Neighbors {
			best = best[:c.Neighbors]
		}
	}
	for _, n := range best {
		vote += n.label
		maxIndex = max(maxIndex, n.index)
	}
	count = len(best)
	if count < c.Neighbors || float64(abs(vote)) < c.MinVoteFraction*float64(count) {
		vote = 0
	}
	return
}

func abs(v int) int {
	if v < 0 {
		return -v
	}
	return v
}

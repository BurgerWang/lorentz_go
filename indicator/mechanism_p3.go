//go:build mechanism_p3

// Copyright jdehorty. Pine-derived indicator logic is licensed under MPL-2.0.
// Experimental P3 direction/memory controls; the frozen Classic path is reused.
package indicator

import (
	"fmt"
	"math"
)

const MechanismP3Contract = "mechanism-p3-go-v1"

// MechanismObservation records global history indices before downstream entry
// selection. Queue indices are copied per bar and never depend on a future bar.
type MechanismObservation struct {
	Version      string `json:"version"`
	Variant      string `json:"variant"`
	T            int    `json:"index"`
	QueueIndices []int  `json:"queue_indices"`
	Ready        bool   `json:"ready"`
	RawDirection int    `json:"raw_direction"`
	Point
}

// RunMechanismInputs keeps runInputs as the single signal/age/exit state
// machine. Readiness retains the original full-vector filter-hold contract.
func RunMechanismInputs(inputs []Inputs, cfg Config, vectors [][]float64, variant string) ([]Point, []MechanismObservation, error) {
	if err := cfg.Validate(); err != nil {
		return nil, nil, err
	}
	if cfg.Algorithm != "original-online" || cfg.SampleStride != 4 {
		return nil, nil, fmt.Errorf("P3 requires original-online and absolute Classic stride 4")
	}
	if variant != "classic-original" && variant != "rq-direction" && variant != "momentum-four" && variant != "classic-recent" {
		return nil, nil, fmt.Errorf("unknown P3 variant %q", variant)
	}
	ready, err := vectorReadiness(inputs, vectors)
	if err != nil {
		return nil, nil, err
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
	audit := make([]MechanismObservation, len(inputs))
	// Original observations replay the frozen FIFO only for index disclosure;
	// points are obtained from the frozen implementation below.
	qLabels := []int{}
	qDistances := []float64{}
	qIndices := []int{}
	classify := func(t int) classification {
		start, end := -1, -1
		directionReady := ready[t]
		prediction, direction := 0, 0
		if variant == "classic-original" || variant == "classic-recent" {
			if variant == "classic-recent" {
				start, end = max(0, t-cfg.MaxBarsBack+1), t
				kept := 0
				for i, j := range qIndices {
					if j >= start {
						qIndices[kept] = j
						qDistances[kept] = qDistances[i]
						qLabels[kept] = qLabels[i]
						kept++
					}
				}
				qIndices, qDistances, qLabels = qIndices[:kept], qDistances[:kept], qLabels[:kept]
			} else {
				start, end = max(0, t-cfg.MaxBarsBack), min(cfg.MaxBarsBack-1, t)
				if cfg.IncludeFullHistory {
					start = 0
				}
			}
			step := 1
			if start > end {
				step = -1
			}
			lastDistance := -1.0
			for j := start; ; j += step {
				d := metric(t, j)
				if d >= lastDistance && j%4 != 0 {
					label := 0
					if j >= 4 {
						label = -sign(inputs[j].Source - inputs[j-4].Source)
					}
					lastDistance = d
					qDistances = append(qDistances, d)
					qLabels = append(qLabels, label)
					qIndices = append(qIndices, j)
					if len(qLabels) > cfg.Neighbors {
						lastDistance = qDistances[int(math.Round(float64(cfg.Neighbors)*3/4))]
						qDistances, qLabels, qIndices = qDistances[1:], qLabels[1:], qIndices[1:]
					}
				}
				if j == end {
					break
				}
			}
			for _, label := range qLabels {
				prediction += label
			}
			direction = sign(float64(prediction))
		} else if variant == "rq-direction" {
			delta := math.NaN()
			if t >= 1 && finite(inputs[t].KernelRQ) && finite(inputs[t-1].KernelRQ) {
				delta = inputs[t].KernelRQ - inputs[t-1].KernelRQ
			}
			directionReady = directionReady && finite(delta)
			if finite(delta) {
				direction = sign(delta)
			}
			prediction = direction
		} else {
			delta := math.NaN()
			if t >= 4 && finite(inputs[t].Source) && finite(inputs[t-4].Source) {
				delta = inputs[t].Source - inputs[t-4].Source
			}
			directionReady = directionReady && finite(delta)
			if finite(delta) {
				direction = sign(delta)
			}
			prediction = direction
		}
		indices := append([]int{}, qIndices...)
		maxIndex := -1
		for _, j := range indices {
			maxIndex = max(maxIndex, j)
		}
		audit[t] = MechanismObservation{Version: "mechanism-indicator-v1", Variant: variant, T: t, QueueIndices: indices, Ready: directionReady, RawDirection: direction, Point: Point{SearchStart: start, SearchEnd: end}}
		return classification{prediction: prediction, neighbors: len(indices), maxIndex: maxIndex, direction: direction}
	}
	var points []Point
	if variant == "classic-original" {
		points, err = RunExtendedInputs(inputs, cfg, vectors)
		if err != nil {
			return nil, nil, err
		}
		for t := range inputs {
			classify(t)
		}
	} else {
		callbackCfg := cfg
		callbackCfg.Algorithm = "aligned-knn"
		points = runInputs(inputs, callbackCfg, metric, func(t int) bool { return audit[t].Ready }, classify)
		for t := range points {
			points[t].SearchStart = audit[t].SearchStart
			points[t].SearchEnd = audit[t].SearchEnd
		}
	}
	for t := range points {
		audit[t].Point = points[t]
	}
	return points, audit, nil
}

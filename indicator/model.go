package indicator

import (
	"fmt"
	"math"
)

type ModelConfig struct {
	Distance        string  `json:"distance"`
	VoteWeight      string  `json:"vote_weight"`
	DistanceEpsilon float64 `json:"distance_epsilon"`
	VoteHalfLife    float64 `json:"vote_half_life"`
	RankHalfLife    float64 `json:"rank_half_life"`
}

func DefaultModelConfig() ModelConfig {
	return ModelConfig{Distance: "lorentzian", VoteWeight: "equal", DistanceEpsilon: 1e-9}
}

func (c ModelConfig) Validate() error {
	if c.Distance != "lorentzian" && c.Distance != "euclidean" {
		return fmt.Errorf("unsupported model distance %q", c.Distance)
	}
	if c.VoteWeight != "equal" && c.VoteWeight != "inverse" {
		return fmt.Errorf("unsupported model vote weight %q", c.VoteWeight)
	}
	if !finite(c.DistanceEpsilon) || c.DistanceEpsilon <= 0 {
		return fmt.Errorf("model distance epsilon must be finite and positive")
	}
	if c.VoteWeight == "equal" && c.DistanceEpsilon != DefaultModelConfig().DistanceEpsilon {
		return fmt.Errorf("equal votes require default inactive distance epsilon")
	}
	for _, halfLife := range []float64{c.VoteHalfLife, c.RankHalfLife} {
		if !finite(halfLife) || halfLife < 0 || halfLife > 0 && halfLife < 1 {
			return fmt.Errorf("model half-life must be zero or finite and at least one")
		}
	}
	if c.VoteHalfLife > 0 && c.RankHalfLife > 0 {
		return fmt.Errorf("vote and rank age variants must be evaluated separately")
	}
	return nil
}

// ModelScore is signed vote strength, not a calibrated probability. Ready
// requires a complete k-neighbor set and representable positive weight sum.
type ModelScore struct {
	Score     float64 `json:"score"`
	WeightSum float64 `json:"weight_sum"`
	Ready     bool    `json:"ready"`
}

type modelNeighbor struct {
	distance, rank float64
	index, label   int
}

func modelDistance(a, b []float64, name string) float64 {
	d := 0.0
	for i, v := range a {
		difference := math.Abs(v - b[i])
		if name == "euclidean" {
			// Hypot evaluates the Euclidean norm without overflowing
			// squared intermediate terms for representable norms.
			d = math.Hypot(d, difference)
		} else {
			// Keep Classic's exact evaluation order for the control.
			d += math.Log(1 + difference)
		}
	}
	return d
}

// RunModelInputs pairs historical vectors with four-bar forward labels only
// after maturity. Ranking age changes selection; vote age changes weights of
// that selected set. Both use actual t-j bars from the prediction origin j.
func RunModelInputs(inputs []Inputs, cfg Config, vectors [][]float64, model ModelConfig) ([]Point, []ModelScore, error) {
	if err := cfg.Validate(); err != nil {
		return nil, nil, err
	}
	if cfg.Algorithm != "aligned-knn" {
		return nil, nil, fmt.Errorf("model vectors require aligned-knn")
	}
	if err := model.Validate(); err != nil {
		return nil, nil, err
	}
	ready, err := vectorReadiness(inputs, vectors)
	if err != nil {
		return nil, nil, err
	}
	scores := make([]ModelScore, len(inputs))
	best := make([]modelNeighbor, 0, cfg.Neighbors+1)
	classify := func(t int) classification {
		result := classification{maxIndex: -1}
		if !ready[t] {
			return result
		}
		best = best[:0]
		start := max(0, t-cfg.MaxBarsBack)
		start += (cfg.SampleStride - start%cfg.SampleStride) % cfg.SampleStride
		for j := start; j <= t-4; j += cfg.SampleStride {
			if !ready[j] || !finite(inputs[j+4].Source) {
				continue
			}
			d := modelDistance(vectors[t], vectors[j], model.Distance)
			if !finite(d) {
				continue
			}
			rank := d
			if model.RankHalfLife > 0 {
				// Comparing log-inflated distances is mathematically
				// equivalent to d*2^(age/half-life), without overflow.
				rank = math.Inf(-1)
				if d > 0 {
					rank = math.Log(d) + float64(t-j)*math.Ln2/model.RankHalfLife
				}
			}
			if len(best) == cfg.Neighbors && rank >= best[len(best)-1].rank {
				continue
			}
			n := modelNeighbor{d, rank, j, sign(inputs[j+4].Source - inputs[j].Source)}
			pos := len(best)
			for pos > 0 && rank < best[pos-1].rank {
				pos--
			}
			best = append(best, modelNeighbor{})
			copy(best[pos+1:], best[pos:])
			best[pos] = n
			if len(best) > cfg.Neighbors {
				best = best[:cfg.Neighbors]
			}
		}
		result.neighbors = len(best)
		rawVote, weightedVote, weightSum := 0, 0.0, 0.0
		for _, n := range best {
			rawVote += n.label
			result.maxIndex = max(result.maxIndex, n.index)
			weight := 1.0
			if model.VoteWeight == "inverse" {
				weight = 1 / math.Max(n.distance, model.DistanceEpsilon)
			}
			if model.VoteHalfLife > 0 {
				weight *= math.Exp2(-float64(t-n.index) / model.VoteHalfLife)
			}
			weightedVote += weight * float64(n.label)
			weightSum += weight
		}
		result.prediction = rawVote
		if result.neighbors < cfg.Neighbors || float64(abs(rawVote)) < cfg.MinVoteFraction*float64(result.neighbors) {
			result.prediction = 0
		}
		if result.neighbors == cfg.Neighbors && finite(weightedVote) && finite(weightSum) && weightSum > 0 {
			score := math.Max(-1, math.Min(1, weightedVote/weightSum))
			scores[t] = ModelScore{Score: score, WeightSum: weightSum, Ready: true}
			if model.VoteWeight == "equal" && model.VoteHalfLife == 0 {
				// Preserve integer-threshold rounding at the legacy
				// boundary, instead of changing it via division first.
				result.direction = sign(float64(result.prediction))
			} else if math.Abs(score) >= cfg.MinVoteFraction {
				result.direction = sign(score)
			}
		}
		return result
	}
	points := runInputs(inputs, cfg, nil, func(t int) bool { return ready[t] }, classify)
	return points, scores, nil
}

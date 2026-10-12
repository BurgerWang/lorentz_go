package indicator

import (
	"encoding/json"
	"math"
	"reflect"
	"testing"
)

func modelFixture(n int) ([]Inputs, [][]float64, Config) {
	x := make([]Inputs, n)
	v := make([][]float64, n)
	for i := range x {
		x[i] = Inputs{Source: 100, VolatilityOK: true, RegimeOK: true, ADXOK: true, EMAUp: true, EMADown: true, SMAUp: true, SMADown: true, KernelRQ: float64(i), KernelGaussian: float64(i)}
		v[i] = []float64{math.NaN(), math.NaN()}
	}
	c := DefaultConfig()
	c.Algorithm, c.UseKernelFilter, c.SampleStride, c.Neighbors = "aligned-knn", false, 1, 1
	return x, v, c
}

func TestModelConfigContract(t *testing.T) {
	base := DefaultModelConfig()
	if err := base.Validate(); err != nil {
		t.Fatal(err)
	}
	b, err := json.Marshal(base)
	if err != nil || string(b) != `{"distance":"lorentzian","vote_weight":"equal","distance_epsilon":1e-9,"vote_half_life":0,"rank_half_life":0}` {
		t.Fatalf("model JSON %s: %v", b, err)
	}
	for _, mutate := range []func(*ModelConfig){
		func(m *ModelConfig) { m.Distance = "unknown" }, func(m *ModelConfig) { m.VoteWeight = "unknown" },
		func(m *ModelConfig) { m.DistanceEpsilon = 0 }, func(m *ModelConfig) { m.DistanceEpsilon = math.Inf(1) },
		func(m *ModelConfig) { m.VoteHalfLife = .5 }, func(m *ModelConfig) { m.RankHalfLife = -1 },
		func(m *ModelConfig) { m.RankHalfLife = math.NaN() },
		func(m *ModelConfig) { m.RankHalfLife, m.VoteHalfLife = 1, 1 },
	} {
		m := base
		mutate(&m)
		if m.Validate() == nil {
			t.Fatalf("invalid model accepted: %+v", m)
		}
	}
	if _, _, err := RunModelInputs(nil, DefaultConfig(), nil, base); err == nil {
		t.Fatal("Classic model route accepted")
	}
}

func TestModelMaturityZeroDistancesTiesAndTooFew(t *testing.T) {
	x, v, c := modelFixture(8)
	for i := range v {
		v[i] = []float64{0, 0}
	}
	x[4].Source = 110
	x[5].Source = 90
	points, scores, err := RunModelInputs(x, c, v, DefaultModelConfig())
	if err != nil {
		t.Fatal(err)
	}
	if points[3].Neighbors != 0 || scores[3].Ready || points[4].MaxNeighborIndex != 0 || points[4].Prediction != 1 || !points[4].StartLong || scores[4].Score != 1 {
		t.Fatalf("matured boundary failed: %+v %+v", points[4], scores[4])
	}
	for i, p := range points {
		if p.MaxNeighborIndex > i-4 && p.MaxNeighborIndex >= 0 {
			t.Fatalf("immature neighbor at %d: %+v", i, p)
		}
	}
	c.Neighbors = 2
	m := DefaultModelConfig()
	m.VoteWeight = "inverse"
	points, scores, _ = RunModelInputs(x, c, v, m)
	if scores[4].Ready || points[4].Prediction != 0 || points[4].StartLong || points[4].Neighbors != 1 {
		t.Fatal("incomplete neighbor set caused a decision")
	}
	if points[5].MaxNeighborIndex != 1 || scores[5].Score != 0 || math.Abs(scores[5].WeightSum-2/m.DistanceEpsilon) > 1e-6 || !scores[5].Ready {
		t.Fatalf("zero distance or deterministic ties failed: %+v %+v", points[5], scores[5])
	}
	v[0][0] = math.NaN()
	points, _, _ = RunModelInputs(x, c, v, m)
	if points[4].Neighbors != 0 {
		t.Fatal("missing historical vector became a neighbor")
	}
	v[0][0] = 0
	x[4].Source = math.NaN()
	c.Neighbors = 1
	points, _, _ = RunModelInputs(x, c, v, m)
	if points[5].MaxNeighborIndex != 1 {
		t.Fatal("candidate with unavailable matured source was included")
	}
}

func TestModelInverseWeightedDirectionRetainsIntegerPrediction(t *testing.T) {
	x, v, c := modelFixture(11)
	v[0], v[1], v[2], v[10] = []float64{.01, 0}, []float64{1, 0}, []float64{2, 0}, []float64{0, 0}
	x[4].Source, x[5].Source, x[6].Source = 110, 90, 90
	c.Neighbors = 3
	m := DefaultModelConfig()
	m.Distance = "euclidean"
	equal, _, _ := RunModelInputs(x, c, v, m)
	m.VoteWeight = "inverse"
	weighted, scores, err := RunModelInputs(x, c, v, m)
	if err != nil {
		t.Fatal(err)
	}
	if equal[10].Signal != -1 || weighted[10].Signal != 1 || weighted[10].Prediction != -1 || !weighted[10].StartLong {
		t.Fatalf("weighted direction or integer prediction lost: equal=%+v weighted=%+v", equal[10], weighted[10])
	}
	expected := (100.0 - 1 - .5) / (100 + 1 + .5)
	if math.Abs(scores[10].Score-expected) > 1e-14 || scores[10].WeightSum != 101.5 {
		t.Fatalf("weighted signed fraction incorrect: %+v", scores[10])
	}
	c.MinVoteFraction = .6
	weighted, scores, _ = RunModelInputs(x, c, v, m)
	if weighted[10].Prediction != 0 || weighted[10].Signal != 1 || !scores[10].Ready {
		t.Fatal("weighted threshold incorrectly used raw integer vote")
	}
	c.MinVoteFraction = .99
	weighted, scores, _ = RunModelInputs(x, c, v, m)
	if weighted[10].Signal != 0 || !scores[10].Ready || scores[10].Score <= 0 {
		t.Fatal("fraction threshold modified diagnostic score or failed to gate decision")
	}
}

func TestModelVoteAndRankAgeAreIndependent(t *testing.T) {
	x, v, c := modelFixture(11)
	v[0], v[6], v[10] = []float64{.1, 0}, []float64{.15, 0}, []float64{0, 0}
	x[4].Source, x[10].Source = 110, 90
	m := DefaultModelConfig()
	m.Distance = "euclidean"
	base, _, _ := RunModelInputs(x, c, v, m)
	m.VoteHalfLife = 1
	voted, scores, _ := RunModelInputs(x, c, v, m)
	if base[10].MaxNeighborIndex != 0 || voted[10].MaxNeighborIndex != 0 || voted[10].Signal != 1 || scores[10].WeightSum != math.Exp2(-10) {
		t.Fatal("vote age changed selection or used label maturity as age origin")
	}
	m.VoteHalfLife, m.RankHalfLife = 0, 1
	ranked, scores, _ := RunModelInputs(x, c, v, m)
	if ranked[10].MaxNeighborIndex != 6 || ranked[10].Signal != -1 || scores[10].WeightSum != 1 {
		t.Fatalf("rank age failed to change selection independently: %+v %+v", ranked[10], scores[10])
	}
}

func TestModelEuclideanChangesActualSelection(t *testing.T) {
	x, v, c := modelFixture(7)
	v[0], v[1], v[6] = []float64{3, 0}, []float64{2, 2}, []float64{0, 0}
	x[4].Source, x[5].Source = 110, 90
	m := DefaultModelConfig()
	lorentzian, _, _ := RunModelInputs(x, c, v, m)
	m.Distance = "euclidean"
	euclidean, _, _ := RunModelInputs(x, c, v, m)
	if lorentzian[6].MaxNeighborIndex != 0 || lorentzian[6].Signal != 1 || euclidean[6].MaxNeighborIndex != 1 || euclidean[6].Signal != -1 {
		t.Fatal("metric option failed to affect true nearest neighbor")
	}
	if modelDistance([]float64{3, 4}, []float64{0, 0}, "euclidean") != 5 {
		t.Fatal("Euclidean norm incorrect")
	}
}

func TestModelDefaultFullPointControlAndPrefixes(t *testing.T) {
	rows := extendedCandles(160)
	for count := 2; count <= 5; count++ {
		for _, k := range []int{1, 3, 8} {
			for _, stride := range []int{1, 4, 7} {
				for _, fraction := range []float64{0, 1.0 / 3, .5, 1} {
					c := DefaultConfig()
					c.Algorithm, c.Neighbors, c.FeatureCount, c.SampleStride, c.MinVoteFraction = "aligned-knn", k, count, stride, fraction
					c.MaxBarsBack, c.UseVolatilityFilter, c.UseRegimeFilter, c.UseKernelFilter = 20+k, false, false, false
					x, v, err := ComputeExtended(rows, c, legacySpecs(c), nil, nil)
					if err != nil {
						t.Fatal(err)
					}
					old, _ := RunInputs(x, c)
					actual, _, err := RunModelInputs(x, c, v, DefaultModelConfig())
					if err != nil || !reflect.DeepEqual(old, actual) {
						t.Fatalf("default model changed full Points count=%d k=%d stride=%d fraction=%g err=%v", count, k, stride, fraction, err)
					}
				}
			}
		}
	}
	c := DefaultConfig()
	c.Algorithm, c.MaxBarsBack = "aligned-knn", 40
	specs := append(legacySpecs(c), FeatureSpec{"RVOL", 5, 1, "rolling-z", 9})
	x, vectors, _ := ComputeExtended(rows, c, specs, nil, nil)
	for _, m := range []ModelConfig{DefaultModelConfig(), {"euclidean", "inverse", 1e-9, 10, 0}, {"lorentzian", "equal", 1e-9, 0, 10}} {
		points, scores, err := RunModelInputs(x, c, vectors, m)
		if err != nil {
			t.Fatal(err)
		}
		for _, end := range []int{35, 90, 159} {
			p, s, err := RunModelInputs(x[:end], c, vectors[:end], m)
			if err != nil || !reflect.DeepEqual(p, points[:end]) || !reflect.DeepEqual(s, scores[:end]) {
				t.Fatalf("model history revised for %+v prefix %d: %v", m, end, err)
			}
		}
	}
}

func TestModelUnavailableQueryAndAbsoluteStride(t *testing.T) {
	x, v, c := modelFixture(12)
	for i := range v {
		v[i] = []float64{0, 0}
	}
	x[4].Source = 110
	c.MaxBarsBack, c.SampleStride = 5, 3
	points, _, _ := RunModelInputs(x, c, v, DefaultModelConfig())
	if points[7].MaxNeighborIndex != 3 || points[10].MaxNeighborIndex != 6 {
		t.Fatalf("stride phase was relative to window start: %+v %+v", points[7], points[10])
	}
	c.SampleStride, c.MaxBarsBack = 1, 20
	v[5][0] = math.NaN()
	points, scores, _ := RunModelInputs(x, c, v, DefaultModelConfig())
	if points[5].Signal != points[4].Signal || points[5].StartLong || points[5].StartShort || scores[5].Ready {
		t.Fatal("unavailable query changed signal")
	}
}

func BenchmarkAlignedControl(b *testing.B) {
	x, vectors, c := modelFixture(2000)
	c.Neighbors, c.MaxBarsBack, c.SampleStride = 8, 200, 4
	for i := range x {
		x[i].Source = 100 + 5*math.Sin(float64(i)/13)
		vectors[i] = make([]float64, c.FeatureCount)
		for j := range vectors[i] {
			value := math.Sin(float64(i+j*3) / float64(7+j))
			x[i].Features[j], vectors[i][j] = value, value
		}
	}
	legacyPoints, err := RunInputs(x, c)
	if err != nil {
		b.Fatal(err)
	}
	modelPoints, _, err := RunModelInputs(x, c, vectors, DefaultModelConfig())
	if err != nil || !reflect.DeepEqual(legacyPoints, modelPoints) {
		b.Fatalf("benchmark controls differ: %v", err)
	}
	b.Run("legacy", func(b *testing.B) {
		b.ReportAllocs()
		for i := 0; i < b.N; i++ {
			if _, err := RunInputs(x, c); err != nil {
				b.Fatal(err)
			}
		}
	})
	b.Run("model_control", func(b *testing.B) {
		b.ReportAllocs()
		for i := 0; i < b.N; i++ {
			if _, _, err := RunModelInputs(x, c, vectors, DefaultModelConfig()); err != nil {
				b.Fatal(err)
			}
		}
	})
}

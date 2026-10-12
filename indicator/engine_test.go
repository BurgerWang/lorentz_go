package indicator

import (
	"math"
	"reflect"
	"testing"
)

func fixtureInputs() []Inputs {
	prices := []float64{100, 101, 102, 103, 110, 108, 105, 99, 95, 96, 98, 102}
	x := make([]Inputs, len(prices))
	for i, p := range prices {
		x[i] = Inputs{Source: p, VolatilityOK: true, RegimeOK: true, ADXOK: true, EMAUp: true, EMADown: true, SMAUp: true, SMADown: true, KernelRQ: float64(i), KernelGaussian: float64(i)}
	}
	return x
}

func TestOriginalLiteralVoteAndFourBarExit(t *testing.T) {
	c := DefaultConfig()
	c.MaxBarsBack = 8
	c.Neighbors = 3
	c.UseKernelFilter = false
	x := fixtureInputs()
	p, err := RunInputs(x, c)
	if err != nil {
		t.Fatal(err)
	}
	// All feature distances are zero. Hand-following the modulo gate/FIFO gives
	// labels 0, -1, -1, +1 at indices 3,5,6,7, not forward-return labels.
	want := []int{0, 0, 0, 0, 0, -1, -2, -1, -1, -1, -1, -1}
	for i, v := range p {
		if v.Prediction != want[i] {
			t.Fatalf("bar %d vote=%d want=%d", i, v.Prediction, want[i])
		}
	}
	if !p[5].StartShort || !p[9].EndShort {
		t.Fatalf("missing hand-derived entry/exit: %+v %+v", p[5], p[9])
	}
	if p[3].SearchStart != 3 || p[3].SearchEnd != 3 {
		t.Fatal("chart endpoint gate not preserved")
	}
}

func TestPineDescendingBoundsAndFullHistory(t *testing.T) {
	c := DefaultConfig()
	c.MaxBarsBack = 4
	c.Neighbors = 2
	c.UseKernelFilter = false
	x := fixtureInputs()
	p, err := RunInputs(x, c)
	if err != nil {
		t.Fatal(err)
	}
	if p[7].SearchStart != 7 || p[7].SearchEnd != 3 || p[7].Prediction != -1 || p[7].MaxNeighborIndex != 5 {
		t.Fatalf("descending hand fixture: %+v", p[7])
	}
	for i := 0; i < 7; i++ {
		if p[i].Neighbors != 0 {
			t.Fatal("started before original chart gate")
		}
	}
	c.IncludeFullHistory = true
	p, err = RunInputs(x, c)
	if err != nil {
		t.Fatal(err)
	}
	if p[7].SearchStart != 0 || p[7].SearchEnd != 3 || p[7].Prediction != 0 {
		t.Fatalf("full history must preserve earliest capped pool: %+v", p[7])
	}
}

func TestCausalAlgorithmsUnaffectedByFutureAppend(t *testing.T) {
	for _, algorithm := range []string{"aligned-knn", "original-online"} {
		c := DefaultConfig()
		c.Algorithm = algorithm
		c.MaxBarsBack = 8
		c.Neighbors = 1
		c.SampleStride = 1
		c.UseKernelFilter = false
		x := fixtureInputs()
		a, err := RunInputs(x[:8], c)
		if err != nil {
			t.Fatal(err)
		}
		b, err := RunInputs(x, c)
		if err != nil {
			t.Fatal(err)
		}
		if !reflect.DeepEqual(a, b[:8]) {
			t.Fatalf("%s changes past on future append", algorithm)
		}
		if algorithm == "aligned-knn" {
			for i, p := range b {
				if p.MaxNeighborIndex >= 0 && p.MaxNeighborIndex > i-4 {
					t.Fatalf("immature label at %d: %+v", i, p)
				}
			}
		}
	}
}

func TestAlignedHandMaturedLabelAndNaN(t *testing.T) {
	c := DefaultConfig()
	c.Algorithm = "aligned-knn"
	c.Neighbors = 1
	c.SampleStride = 1
	c.UseKernelFilter = false
	x := fixtureInputs()
	p, err := RunInputs(x, c)
	if err != nil {
		t.Fatal(err)
	}
	if p[3].Neighbors != 0 || p[4].Prediction != 1 || p[4].MaxNeighborIndex != 0 || !p[4].StartLong {
		t.Fatalf("matured forward label not paired with old feature: %+v", p[4])
	}
	x[0].Features[0] = math.NaN()
	p, err = RunInputs(x, c)
	if err != nil {
		t.Fatal(err)
	}
	if p[4].Neighbors != 0 || p[4].Prediction != 0 {
		t.Fatal("invalid warmup sample included")
	}
}

func TestFilteredFlipIsNotRetried(t *testing.T) {
	c := DefaultConfig()
	c.Algorithm = "aligned-knn"
	c.Neighbors = 1
	c.SampleStride = 1
	x := fixtureInputs()
	x[4].KernelRQ = 2 // falling on the first long flip
	p, err := RunInputs(x, c)
	if err != nil {
		t.Fatal(err)
	}
	if p[4].Signal != 1 || p[4].StartLong || p[5].StartLong {
		t.Fatal("filtered original entry was retried")
	}
}

func TestConfigRejectsNonfiniteAndInvalid(t *testing.T) {
	c := DefaultConfig()
	c.KernelR = math.Inf(1)
	if _, err := RunInputs(fixtureInputs(), c); err == nil {
		t.Fatal("invalid kernel accepted")
	}
}

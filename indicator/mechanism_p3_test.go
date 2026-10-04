//go:build mechanism_p3

package indicator

import (
	"math"
	"reflect"
	"testing"
)

func mechanismFixture(n int) ([]Inputs, [][]float64, Config) {
	c := DefaultConfig()
	c.Algorithm = "original-online"
	c.MaxBarsBack = 7
	c.Neighbors = 4
	c.UseKernelFilter = false
	x := make([]Inputs, n)
	v := make([][]float64, n)
	for i := range x {
		x[i] = Inputs{Source: float64(i) + 100, VolatilityOK: true, RegimeOK: true, ADXOK: true, EMAUp: true, EMADown: true, SMAUp: true, SMADown: true, KernelRQ: float64(i % 7), KernelGaussian: float64(i % 7)}
		v[i] = []float64{float64(i % 3), float64(i % 5)}
	}
	return x, v, c
}

func TestMechanismOriginalFrozenPointCompatibility(t *testing.T) {
	x, v, c := mechanismFixture(43)
	for _, full := range []bool{false, true} {
		c.IncludeFullHistory = full
		want, err := RunExtendedInputs(x, c, v)
		if err != nil {
			t.Fatal(err)
		}
		got, audit, err := RunMechanismInputs(x, c, v, "classic-original")
		if err != nil {
			t.Fatal(err)
		}
		if !reflect.DeepEqual(got, want) {
			t.Fatal("frozen original points changed")
		}
		for i, a := range audit {
			if !reflect.DeepEqual(a.Point, want[i]) {
				t.Fatalf("point disclosure mismatch at %d", i)
			}
			maxIndex := -1
			vote := 0
			for _, j := range a.QueueIndices {
				if j > i || j%4 == 0 {
					t.Fatalf("invalid original index %d at %d", j, i)
				}
				maxIndex = max(maxIndex, j)
				if j >= 4 {
					vote -= sign(x[j].Source - x[j-4].Source)
				}
			}
			if len(a.QueueIndices) != want[i].Neighbors || maxIndex != want[i].MaxNeighborIndex || vote != want[i].Prediction {
				t.Fatalf("original queue disclosure differs at %d", i)
			}
		}
	}
}

func TestMechanismNewDirectionStateFilterZeroAndReadiness(t *testing.T) {
	x, v, c := mechanismFixture(12)
	for i := range v {
		v[i] = []float64{0, 0}
		x[i].KernelRQ = float64(i)
	}
	old, err := RunExtendedInputs(x, c, v)
	if err != nil {
		t.Fatal(err)
	}
	x[6].Source = 90
	x[6].VolatilityOK = false
	x[7].Source = x[3].Source       // exactly zero direction retains held signal
	v[8] = []float64{math.NaN(), 0} // old readiness contract holds new negative direction
	got, audit, err := RunMechanismInputs(x, c, v, "momentum-four")
	if err != nil {
		t.Fatal(err)
	}
	if old[5].Signal != -1 || got[4].Signal != 1 || !got[4].StartLong || got[4].BarsHeld != 0 {
		t.Fatal("new momentum did not replace contradictory old classifier")
	}
	if got[6].Signal != 1 || got[6].BarsHeld != 2 || got[6].FilterOK {
		t.Fatal("filter must hold signal and advance age")
	}
	if audit[7].RawDirection != 0 || got[7].Signal != 1 || got[7].BarsHeld != 3 {
		t.Fatal("zero direction did not hold")
	}
	if audit[8].Ready || got[8].Signal != 1 || got[8].BarsHeld != 4 || !got[8].EndLong {
		t.Fatal("unready must hold signal while fixed exit advances")
	}
	for i := range audit {
		if i < 4 && audit[i].Ready {
			t.Fatal("momentum requires 4 prior bars")
		}
	}
	x[9].KernelRQ = math.NaN()
	rq, a, err := RunMechanismInputs(x, c, v, "rq-direction")
	if err != nil {
		t.Fatal(err)
	}
	if rq[1].Signal != 1 || !rq[1].StartLong || a[0].Ready || a[9].Ready || a[10].Ready {
		t.Fatal("RQ causal readiness/positive direction")
	}
}

func TestMechanismDynamicExitsUseNewDirection(t *testing.T) {
	x, v, c := mechanismFixture(6)
	c.UseDynamicExits = true
	kernels := []float64{1, 2, 1, 2, 3, 2}
	for i := range x {
		x[i].KernelRQ = kernels[i]
		v[i] = []float64{0, 0}
	}
	got, _, err := RunMechanismInputs(x, c, v, "rq-direction")
	if err != nil {
		t.Fatal(err)
	}
	old, err := RunExtendedInputs(x, c, v)
	if err != nil {
		t.Fatal(err)
	}
	if !got[3].StartLong || got[3].Signal != 1 || !got[5].EndLong || old[5].EndLong {
		t.Fatal("dynamic exit still depends on old classifier trigger")
	}
}

func TestMechanismRecentExpiryWithoutAnyNewNeighbors(t *testing.T) {
	x, v, c := mechanismFixture(12)
	c.MaxBarsBack = 4
	for i := range v {
		v[i] = []float64{0, 0}
		if i >= 6 {
			v[i][0] = math.NaN()
		}
	}
	p, a, err := RunMechanismInputs(x, c, v, "classic-recent")
	if err != nil {
		t.Fatal(err)
	}
	if p[5].Prediction != -1 || p[5].Neighbors == 0 {
		t.Fatal("fixture lacks prior nonzero vote")
	}
	// Queue contains repeated out-of-order indices; every item must be filtered.
	for i, row := range a {
		for _, j := range row.QueueIndices {
			if j < max(0, i-c.MaxBarsBack+1) || j > i || j%4 == 0 {
				t.Fatalf("expired/phase invalid index %d at %d", j, i)
			}
		}
	}
	if p[9].Prediction != 0 || p[9].Neighbors != 0 || p[9].MaxNeighborIndex != -1 || len(a[9].QueueIndices) != 0 {
		t.Fatal("expired vote persisted with no new neighbors")
	}
	if p[9].Signal != -1 || p[9].BarsHeld != 4 {
		t.Fatal("expiry must not reset held state")
	}
}

func TestMechanismAllVariantsPrefixAsOf(t *testing.T) {
	x, v, c := mechanismFixture(39)
	for _, variant := range []string{"classic-original", "rq-direction", "momentum-four", "classic-recent"} {
		full, a, err := RunMechanismInputs(x, c, v, variant)
		if err != nil {
			t.Fatal(err)
		}
		for _, n := range []int{5, 8, 13, 27} {
			prefix, b, err := RunMechanismInputs(x[:n], c, v[:n], variant)
			if err != nil {
				t.Fatal(err)
			}
			if !reflect.DeepEqual(prefix, full[:n]) || !reflect.DeepEqual(b, a[:n]) {
				t.Fatalf("future changed %s prefix %d", variant, n)
			}
		}
	}
}

func TestMechanismRejectUnsupported(t *testing.T) {
	x, v, c := mechanismFixture(8)
	if _, _, e := RunMechanismInputs(x, c, v, "expanding"); e == nil {
		t.Fatal("unknown variant accepted")
	}
	c.SampleStride = 8
	if _, _, e := RunMechanismInputs(x, c, v, "classic-recent"); e == nil {
		t.Fatal("non4 stride accepted")
	}
	c.SampleStride = 4
	c.Algorithm = "aligned-knn"
	if _, _, e := RunMechanismInputs(x, c, v, "rq-direction"); e == nil {
		t.Fatal("aligned accepted")
	}
}

func TestMechanismBaselinesIgnoreFiniteVectorDistancesAndHoldOverflow(t *testing.T) {
	x, v, c := mechanismFixture(35)
	alternate := make([][]float64, len(v))
	for i := range alternate {
		alternate[i] = []float64{float64(i * i), float64((i % 4) * 900)}
	}
	for _, variant := range []string{"rq-direction", "momentum-four"} {
		a, _, e := RunMechanismInputs(x, c, v, variant)
		if e != nil {
			t.Fatal(e)
		}
		b, _, e := RunMechanismInputs(x, c, alternate, variant)
		if e != nil {
			t.Fatal(e)
		}
		if !reflect.DeepEqual(a, b) {
			t.Fatal("baseline depends on discarded classifier distances")
		}
	}
	x[0].KernelRQ = -math.MaxFloat64
	x[1].KernelRQ = math.MaxFloat64
	_, a, e := RunMechanismInputs(x, c, v, "rq-direction")
	if e != nil {
		t.Fatal(e)
	}
	if a[1].Ready || a[1].RawDirection != 0 {
		t.Fatal("overflow RQ delta accepted")
	}
	x[0].Source = -math.MaxFloat64
	x[4].Source = math.MaxFloat64
	_, a, e = RunMechanismInputs(x, c, v, "momentum-four")
	if e != nil {
		t.Fatal(e)
	}
	if a[4].Ready || a[4].RawDirection != 0 {
		t.Fatal("overflow momentum delta accepted")
	}
}

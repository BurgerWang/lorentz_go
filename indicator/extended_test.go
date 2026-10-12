package indicator

import (
	"encoding/json"
	"lorentzgo/market"
	"math"
	"reflect"
	"testing"
)

func legacySpecs(c Config) []FeatureSpec {
	f := make([]FeatureSpec, c.FeatureCount)
	for i := range f {
		f[i] = FeatureSpec{c.Features[i].Name, c.Features[i].A, c.Features[i].B, "legacy", 0}
	}
	return f
}

func extendedCandles(n int) []market.Candle {
	rows := make([]market.Candle, n)
	for i := range rows {
		p := 100 + float64(i)/20 + 5*math.Sin(float64(i)/7)
		rows[i] = market.Candle{OpenTime: int64(i) * 3600000, CloseTime: int64(i+1)*3600000 - 1, Open: p, High: p + 2, Low: p - 2, Close: p, Volume: 100 + 20*math.Cos(float64(i)/5)}
	}
	return rows
}

func equalFloat(a, b float64) bool {
	return a == b || math.IsNaN(a) && math.IsNaN(b)
}

func TestFeatureSpecValidationAndJSON(t *testing.T) {
	specs := legacySpecs(DefaultConfig())
	if err := (NamedFeatureGroup{"classic", specs}).Validate(); err != nil {
		t.Fatal(err)
	}
	data, err := json.Marshal(specs[0])
	if err != nil || string(data) != `{"name":"RSI","a":14,"b":1,"normalization":"legacy","window":0}` {
		t.Fatalf("unexpected feature JSON %s: %v", data, err)
	}
	for _, f := range []FeatureSpec{
		{"RSI", 14, 1, "rolling-z", 10}, {"WT", 10, 11, "legacy", 1},
		{"RVOL", 0, 1, "rolling-z", 20}, {"RVOL", 20, 2, "rolling-z", 20},
		{"ATR_PRICE", 10001, 1, "rolling-z", 20}, {"ATR_PRICE", 14, 1, "rolling-z", 1},
		{"ATR_PRICE", 14, 1, "rolling-z", 10001}, {"D1_SLOPE", 2, 1, "rolling-z", 20},
		{"KERNEL_DEVIATION", 2, 1, "rolling-z", 20}, {"UNKNOWN", 1, 1, "legacy", 0},
	} {
		if f.Validate() == nil {
			t.Fatalf("accepted invalid %+v", f)
		}
	}
	if ValidateFeatureSpecs(specs[:1]) == nil || ValidateFeatureSpecs(append(specs, specs[0])) == nil || (NamedFeatureGroup{" ", specs}).Validate() == nil {
		t.Fatal("invalid group accepted")
	}
	for _, name := range []string{"RVOL", "ATR_PRICE", "KERNEL_DEVIATION", "D1_SLOPE"} {
		if err := (FeatureSpec{name, 1, 1, "rolling-z", 2}).Validate(); err != nil {
			t.Fatal(err)
		}
	}
}

func TestExtendedLegacyExactControlAndAdditionalSlots(t *testing.T) {
	c := DefaultConfig()
	c.Algorithm = "original-online"
	c.MaxBarsBack = 40
	rows := extendedCandles(230)
	x, v, err := ComputeExtended(rows, c, legacySpecs(c), nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	for i := range x {
		for j := range v[i] {
			if !equalFloat(x[i].Features[j], v[i][j]) {
				t.Fatalf("legacy feature differs at %d/%d", i, j)
			}
		}
	}
	control, _ := RunInputs(x, c)
	extended, err := RunExtendedInputs(x, c, v)
	if err != nil || !reflect.DeepEqual(control, extended) {
		t.Fatalf("Classic control changed: %v", err)
	}
	for count := 2; count <= 5; count++ {
		for _, fullHistory := range []bool{false, true} {
			alternate := c
			alternate.FeatureCount = count
			alternate.IncludeFullHistory = fullHistory
			alternate.UseVolatilityFilter, alternate.UseRegimeFilter, alternate.UseKernelFilter = false, false, false
			x, vectors, err := ComputeExtended(rows, alternate, legacySpecs(alternate), nil, nil)
			if err != nil {
				t.Fatal(err)
			}
			original, _ := RunInputs(x, alternate)
			actual, err := RunExtendedInputs(x, alternate, vectors)
			if err != nil || !reflect.DeepEqual(original, actual) {
				t.Fatalf("Classic control changed with count=%d full-history=%t: %v", count, fullHistory, err)
			}
		}
	}
	specs := append(legacySpecs(c), FeatureSpec{"CCI", 7, 2, "legacy", 0}, FeatureSpec{"RSI", 3, 1, "legacy", 0})
	_, v, err = ComputeExtended(rows, c, specs, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	for j := 5; j < len(specs); j++ {
		alternate := c
		alternate.Features[0] = Feature{specs[j].Name, specs[j].A, specs[j].B}
		ref, err := Compute(rows, alternate)
		if err != nil {
			t.Fatal(err)
		}
		for i := range ref {
			if !equalFloat(v[i][j], ref[i].Features[0]) {
				t.Fatalf("additional legacy slot differs at %d/%d", i, j)
			}
		}
	}
}

func TestRollingZReadyConstantMissingAndOutlier(t *testing.T) {
	src := []float64{4, 4, 4, 4, math.NaN(), 4, 4, 4, 1e12, 1, 2, 3, 3, 3, 3}
	z := rollingZ(src, 3)
	for _, i := range []int{0, 1, 4, 5, 6} {
		if !math.IsNaN(z[i]) {
			t.Fatalf("unready window %d became %g", i, z[i])
		}
	}
	for _, i := range []int{2, 3, 7, 13, 14} {
		if z[i] != 0 {
			t.Fatalf("constant window %d became %g", i, z[i])
		}
	}
	if math.Abs(z[8]-math.Sqrt(2)) > 1e-10 || math.Abs(z[11]-math.Sqrt(1.5)) > 1e-10 {
		t.Fatalf("outlier or its removal incorrect: %v", z)
	}
	for end := 1; end <= len(src); end++ {
		prefix := rollingZ(src[:end], 3)
		for i := range prefix {
			if !equalFloat(prefix[i], z[i]) {
				t.Fatalf("z-score revised history at %d after append", i)
			}
		}
	}
}

func TestExtendedRawCausalDefinitions(t *testing.T) {
	rows := extendedCandles(8)
	for i := range rows {
		rows[i].Volume = float64(i + 1)
		rows[i].High, rows[i].Low, rows[i].Close = 11, 9, 10
	}
	rvol := extendedRaw(rows, FeatureSpec{Name: "RVOL", A: 2}, nil, nil)
	if !math.IsNaN(rvol[1]) || rvol[2] != 2 || rvol[3] != 1.6 {
		t.Fatalf("RVOL included current volume or wrong window: %v", rvol)
	}
	atr := extendedRaw(rows, FeatureSpec{Name: "ATR_PRICE", A: 3}, nil, nil)
	if !math.IsNaN(atr[1]) || atr[2] != .2 || atr[7] != .2 {
		t.Fatalf("Wilder ATR/close incorrect: %v", atr)
	}
	rows[0].Volume, rows[1].Volume = 0, 0
	if !math.IsNaN(extendedRaw(rows, FeatureSpec{Name: "RVOL", A: 2}, nil, nil)[2]) {
		t.Fatal("zero RVOL denominator filled")
	}
	env := make([]EnvelopePoint, len(rows))
	daily := make([]market.DailyContext, len(rows))
	for i := range rows {
		env[i] = EnvelopePoint{Center: 8, ATR: 2, Ready: true}
		daily[i] = market.DailyContext{Center: 100, Slope: 2, Ready: true, AvailableTime: rows[i].CloseTime + 1}
	}
	if extendedRaw(rows, FeatureSpec{Name: "KERNEL_DEVIATION"}, env, nil)[0] != 1 || extendedRaw(rows, FeatureSpec{Name: "D1_SLOPE"}, nil, daily)[0] != .02 {
		t.Fatal("context ratios incorrect")
	}
	env[1].Ready = false
	daily[1].AvailableTime++
	if !math.IsNaN(extendedRaw(rows, FeatureSpec{Name: "KERNEL_DEVIATION"}, env, nil)[1]) || !math.IsNaN(extendedRaw(rows, FeatureSpec{Name: "D1_SLOPE"}, nil, daily)[1]) {
		t.Fatal("unready or future context became available")
	}
	rows[3].High, rows[3].Volume = math.NaN(), math.NaN()
	if !math.IsNaN(extendedRaw(rows, FeatureSpec{Name: "ATR_PRICE", A: 3}, nil, nil)[3]) || !math.IsNaN(extendedRaw(rows, FeatureSpec{Name: "RVOL", A: 2}, nil, nil)[4]) {
		t.Fatal("missing raw observation filled")
	}
}

func TestRVOLRecoversAfterExtremeVolumeLeavesWindow(t *testing.T) {
	rows := extendedCandles(80)
	for i := range rows {
		rows[i].Volume = float64(1 + i%7)
	}
	rows[20].Volume = 1e20
	raw := extendedRaw(rows, FeatureSpec{Name: "RVOL", A: 3}, nil, nil)
	// Once the outlier has left, bars 30..32 have independently derived
	// volume ratios: 3/((7+1+2)/3), 4/((1+2+3)/3), 5/((2+3+4)/3).
	wantRaw := []float64{.9, 2, 5.0 / 3}
	for j, expected := range wantRaw {
		if math.Abs(raw[30+j]-expected) > 1e-14 {
			t.Fatalf("RVOL after outlier at %d=%g want=%g", 30+j, raw[30+j], expected)
		}
	}
	mean := (wantRaw[0] + wantRaw[1] + wantRaw[2]) / 3
	variance := (math.Pow(wantRaw[0]-mean, 2) + math.Pow(wantRaw[1]-mean, 2) + math.Pow(wantRaw[2]-mean, 2)) / 3
	expectedZ := (wantRaw[2] - mean) / math.Sqrt(variance)
	cfg := DefaultConfig()
	cfg.Algorithm = "original-online"
	specs := []FeatureSpec{{"RSI", 14, 1, "legacy", 0}, {"RVOL", 3, 1, "rolling-z", 3}}
	_, vectors, err := ComputeExtended(rows, cfg, specs, nil, nil)
	if err != nil || math.Abs(vectors[32][1]-expectedZ) > 1e-12 || vectors[32][1] <= 0 {
		t.Fatalf("RVOL z-score failed to recover at 32: %g want=%g err=%v", vectors[32][1], expectedZ, err)
	}
	_, prefix, err := ComputeExtended(rows[:33], cfg, specs, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	for i := range prefix {
		if !equalFloat(prefix[i][1], vectors[i][1]) {
			t.Fatalf("RVOL history changed after append at %d", i)
		}
	}
	// A finite-volume ratio that overflows must remain unavailable.
	rows[0].Volume, rows[1].Volume, rows[2].Volume, rows[3].Volume = 1e-300, 1e-300, 1e-300, 1e300
	if !math.IsNaN(extendedRaw(rows, FeatureSpec{Name: "RVOL", A: 3}, nil, nil)[3]) {
		t.Fatal("overflowing finite RVOL ratio became available")
	}
}

func TestExtendedVectorsAndDecisionsArePrefixCausal(t *testing.T) {
	c := DefaultConfig()
	c.Algorithm, c.MaxBarsBack = "original-online", 32
	rows := extendedCandles(180)
	env, _ := ComputeEnvelope(rows, DefaultEnvelopeConfig())
	daily := make([]market.DailyContext, len(rows))
	for i := range daily {
		daily[i] = market.DailyContext{Center: 100, Slope: math.Sin(float64(i / 24)), AvailableTime: int64(i/24+1) * 24 * 3600000, Ready: true}
	}
	specs := append(legacySpecs(c), FeatureSpec{"RVOL", 8, 1, "rolling-z", 12}, FeatureSpec{"ATR_PRICE", 5, 1, "rolling-z", 12}, FeatureSpec{"KERNEL_DEVIATION", 1, 1, "rolling-z", 12}, FeatureSpec{"D1_SLOPE", 1, 1, "rolling-z", 12})
	x, full, err := ComputeExtended(rows, c, specs, env, daily)
	if err != nil {
		t.Fatal(err)
	}
	points, _ := RunExtendedInputs(x, c, full)
	for _, end := range []int{30, 95, 130, 179} {
		x, prefix, err := ComputeExtended(rows[:end], c, specs, env[:end], daily[:end])
		if err != nil {
			t.Fatal(err)
		}
		for i := range prefix {
			for j := range prefix[i] {
				if !equalFloat(prefix[i][j], full[i][j]) {
					t.Fatalf("feature history revised %d/%d", i, j)
				}
			}
		}
		p, _ := RunExtendedInputs(x, c, prefix)
		if !reflect.DeepEqual(p, points[:end]) {
			t.Fatalf("extended decisions revised after prefix %d", end)
		}
	}
}

func TestExtendedPreservesClassicLabelsAndHoldsUnavailableQuery(t *testing.T) {
	c := DefaultConfig()
	c.Algorithm, c.Neighbors, c.MaxBarsBack, c.UseKernelFilter = "original-online", 3, 8, false
	x := fixtureInputs()
	v := make([][]float64, len(x))
	for i := range v {
		v[i] = []float64{0, 0, 0, 0, 0, 0}
	}
	control, _ := RunInputs(x, c)
	points, err := RunExtendedInputs(x, c, v)
	if err != nil || !reflect.DeepEqual(points, control) {
		t.Fatalf("new dimension changed Classic zero-distance FIFO/labels: %v", err)
	}
	v[6][0] = math.NaN()
	points, _ = RunExtendedInputs(x, c, v)
	if points[6].Signal != points[5].Signal || !points[6].FilterOK || points[6].StartLong || points[6].StartShort {
		t.Fatalf("unavailable query changed signal: %+v", points[6])
	}
	// The established short must still receive its four-bar exit while
	// unavailable; missing input gates entries, not lifecycle accounting.
	v[9][0] = math.NaN()
	points, _ = RunExtendedInputs(x, c, v)
	if !points[9].EndShort {
		t.Fatal("missing query suppressed existing exit")
	}
	for i := range v {
		v[i][0] = math.NaN()
	}
	points, _ = RunExtendedInputs(x, c, v)
	for _, p := range points {
		if p.Neighbors != 0 || p.Signal != 0 || p.StartLong || p.StartShort {
			t.Fatal("invalid historical vector became a neighbor")
		}
	}
}

func TestExtendedClassicMatchingPrefixBeforeChangedFutureVectors(t *testing.T) {
	c := DefaultConfig()
	c.Algorithm, c.UseKernelFilter, c.UseVolatilityFilter, c.UseRegimeFilter = "original-online", false, false, false
	x := fixtureInputs()
	vectors := make([][]float64, len(x))
	for i := range vectors {
		vectors[i] = append([]float64(nil), x[i].Features[:]...)
	}
	const end = 7
	prefix, err := RunExtendedInputs(x[:end], c, vectors[:end])
	if err != nil {
		t.Fatal(err)
	}
	for i := end; i < len(vectors); i++ {
		vectors[i][0] = float64(i * 100)
	}
	full, err := RunExtendedInputs(x, c, vectors)
	if err != nil || !reflect.DeepEqual(prefix, full[:end]) {
		t.Fatalf("changed future vector selected different prefix behavior: %v", err)
	}
}

func TestExtendedRejectsMalformedVectorsAndContext(t *testing.T) {
	c := DefaultConfig()
	c.Algorithm = "original-online"
	x := fixtureInputs()
	if _, err := RunExtendedInputs(x, c, nil); err == nil {
		t.Fatal("missing vectors accepted")
	}
	for _, vector := range [][]float64{{0}, make([]float64, 16)} {
		v := make([][]float64, len(x))
		for i := range v {
			v[i] = vector
		}
		if _, err := RunExtendedInputs(x, c, v); err == nil {
			t.Fatal("invalid width accepted")
		}
	}
	specs := []FeatureSpec{{"RSI", 14, 1, "legacy", 0}, {"D1_SLOPE", 1, 1, "rolling-z", 5}}
	if _, _, err := ComputeExtended(extendedCandles(10), c, specs, nil, nil); err == nil {
		t.Fatal("missing daily slice accepted")
	}
	specs[1].Name = "KERNEL_DEVIATION"
	if _, _, err := ComputeExtended(extendedCandles(10), c, specs, nil, nil); err == nil {
		t.Fatal("missing envelope accepted")
	}
	c.Algorithm = "aligned-knn"
	if _, err := RunExtendedInputs(nil, c, nil); err == nil {
		t.Fatal("later-stage algorithm accepted")
	}
}

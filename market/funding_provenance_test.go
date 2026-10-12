package market

import (
	"math"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

func TestFundingMarkProvenanceValidation(t *testing.T) {
	const bucket int64 = 28800000
	good := Funding{Time: bucket + 59999, Rate: -.0001, MarkPrice: 123.456789, MarkPriceSource: FundingMarkKlineOpen8h, MarkPriceTime: bucket}
	for _, row := range []Funding{good, {Time: bucket, MarkPrice: 1}, {Time: 0, MarkPrice: 1, MarkPriceSource: FundingMarkKlineOpen8h}} {
		if err := ValidateFunding([]Funding{row}); err != nil {
			t.Fatal(err)
		}
	}
	cases := map[string]func(*Funding){
		"unknown source":   func(f *Funding) { f.MarkPriceSource = "unverified" },
		"legacy timestamp": func(f *Funding) { f.MarkPriceSource = "" },
		"future":           func(f *Funding) { f.MarkPriceTime = 2 * bucket },
		"previous bucket":  func(f *Funding) { f.MarkPriceTime = 0 },
		"off grid":         func(f *Funding) { f.MarkPriceTime++ },
		"late settlement":  func(f *Funding) { f.Time = bucket + 60000 },
		"missing price":    func(f *Funding) { f.MarkPrice = 0 },
		"nonfinite price":  func(f *Funding) { f.MarkPrice = math.Inf(1) },
		"nonfinite rate":   func(f *Funding) { f.Rate = math.NaN() },
	}
	for name, mutate := range cases {
		t.Run(name, func(t *testing.T) {
			f := good
			mutate(&f)
			if err := ValidateFunding([]Funding{f}); err == nil {
				t.Fatal("accepted invalid funding provenance/value")
			}
		})
	}
	path := filepath.Join(t.TempDir(), "funding.json")
	if err := SaveFunding(path, []Funding{good}); err != nil {
		t.Fatal(err)
	}
	got, err := LoadFunding(path)
	if err != nil || !reflect.DeepEqual(got, []Funding{good}) {
		t.Fatalf("proxy round trip: %+v %v", got, err)
	}
	// Loading must enforce the same provenance contract as direct ledger input.
	raw := `[{"time":28800000,"rate":0.001,"mark_price":100,"mark_price_source":"unverified"}]`
	if err := os.WriteFile(path, []byte(raw), 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := LoadFunding(path); err == nil {
		t.Fatal("loaded unknown provenance")
	}
}

func TestLegacyFundingJSONCompatibility(t *testing.T) {
	path := filepath.Join(t.TempDir(), "funding.json")
	raw := `[{"time":10,"rate":-0.000123456789,"mark_price":1234.56789012345}]`
	if err := os.WriteFile(path, []byte(raw), 0600); err != nil {
		t.Fatal(err)
	}
	got, err := LoadFunding(path)
	if err != nil {
		t.Fatal(err)
	}
	want := []Funding{{Time: 10, Rate: -.000123456789, MarkPrice: 1234.56789012345}}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("legacy values changed: %+v", got)
	}
	if err := SaveFunding(path, got); err != nil {
		t.Fatal(err)
	}
	saved, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(saved), "mark_price_source") || strings.Contains(string(saved), "mark_price_time") {
		t.Fatal("legacy JSON gained optional fields")
	}
}

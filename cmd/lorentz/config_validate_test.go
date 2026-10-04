package main

import (
	"lorentzgo/strategy"
	"reflect"
	"testing"
)

func TestADXSearchSeedNormalizationParity(t *testing.T) {
	ds := enhancedFixture(t)
	a := strategy.DefaultConfig()
	b := a
	for i := range b.Classic.Features {
		if b.Classic.Features[i].Name == "ADX" {
			b.Classic.Features[i].B = 1
		}
	}
	b.Classifier = strategy.ClassicClassifier(b.Classic)
	x, e := ds.evaluate(a)
	if e != nil {
		t.Fatal(e)
	}
	y, e := ds.evaluate(b)
	if e != nil {
		t.Fatal(e)
	}
	for _, k := range []string{"folds", "aggregate", "executed_entries", "exit_reasons"} {
		if !reflect.DeepEqual(x[k], y[k]) {
			t.Fatalf("ADX B normalization changed %s", k)
		}
	}
}

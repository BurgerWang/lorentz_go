package main

import (
	"encoding/json"
	"flag"
	"os"
	"path/filepath"
	"testing"

	"lorentzgo/indicator"
)

func TestConfigPreservesUnexposedSettingsAndExplicitFlagOverrides(t *testing.T) {
	c := indicator.DefaultConfig()
	c.Algorithm = "original-online"
	c.Neighbors = 17
	c.IncludeFullHistory = true
	c.UseSMAFilter = true
	c.SMAPeriod = 150
	c.Features[1] = indicator.Feature{Name: "CCI", A: 30, B: 3}
	c.KernelR = 4.25
	b, _ := json.Marshal(c)
	p := filepath.Join(t.TempDir(), "config.json")
	if err := os.WriteFile(p, b, 0644); err != nil {
		t.Fatal(err)
	}
	f := flag.NewFlagSet("test", flag.ContinueOnError)
	m := modelOptions(f)
	if err := f.Parse([]string{"-config", p}); err != nil {
		t.Fatal(err)
	}
	got, err := m.model()
	if err != nil || got != c {
		t.Fatalf("config lost settings: %+v %v", got, err)
	}
	f = flag.NewFlagSet("test", flag.ContinueOnError)
	m = modelOptions(f)
	if err := f.Parse([]string{"-config", p, "-neighbors", "8", "-full-history=false"}); err != nil {
		t.Fatal(err)
	}
	c.Neighbors = 8
	c.IncludeFullHistory = false
	got, err = m.model()
	if err != nil || got != c {
		t.Fatalf("explicit override/default handling: %+v %v", got, err)
	}
}

func TestModelWithoutConfigRetainsExistingDefaults(t *testing.T) {
	f := flag.NewFlagSet("test", flag.ContinueOnError)
	m := modelOptions(f)
	f.Parse(nil)
	got, err := m.model()
	if err != nil || got != indicator.DefaultConfig() {
		t.Fatal(got, err)
	}
}

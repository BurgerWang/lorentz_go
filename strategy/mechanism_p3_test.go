//go:build mechanism_p3

package strategy

import (
	"encoding/json"
	"reflect"
	"strings"
	"testing"
)

func TestMechanismWrapperStrictCompleteValidation(t *testing.T) {
	w := MechanismConfig{MechanismConfigVersion, "classic-original", DefaultConfig()}
	raw, e := json.Marshal(w)
	if e != nil {
		t.Fatal(e)
	}
	if _, e = DecodeMechanismConfig(raw); e != nil {
		t.Fatal(e)
	}
	bad := []string{
		strings.Replace(string(raw), `"version":"mechanism-p3-config-v1"`, `"version":"mechanism-p3-config-v2"`, 1),
		strings.Replace(string(raw), `"variant":"classic-original"`, `"variant":"expanding"`, 1),
		strings.Replace(string(raw), `"version":"mechanism-p3-config-v1",`, "", 1),
		strings.Replace(string(raw), `"sample_stride":4`, `"sample_stride":8`, 1),
		strings.Replace(string(raw), `"schema_version":5`, `"schema_version":4`, 1),
		strings.Replace(string(raw), `"kernel_r":8,`, "", 1),
		strings.Replace(string(raw), `"version":"mechanism-p3-config-v1"`, `"version":"mechanism-p3-config-v1","version":"mechanism-p3-config-v1"`, 1),
		strings.Replace(string(raw), `"variant":"classic-original"`, `"variant":"classic-original","unknown":0`, 1),
		strings.Replace(string(raw), `"algorithm":"original-online"`, `"algorithm":"aligned-knn"`, 1),
		strings.Replace(string(raw), `"strategy":{`, `"strategy":null,"unused":{`, 1),
	}
	for i, s := range bad {
		if _, e := DecodeMechanismConfig([]byte(s)); e == nil {
			t.Fatalf("bad wrapper %d accepted", i)
		}
	}
	for _, variant := range []string{"rq-direction", "momentum-four", "classic-recent"} {
		w.Variant = variant
		b, _ := json.Marshal(w)
		if _, e := DecodeMechanismConfig(b); e != nil {
			t.Fatal(e)
		}
	}
}

func TestMechanismStrategyOriginalCompatibilityAndDailyPullbackPrefix(t *testing.T) {
	rows, daily := fixture(600)
	c := DefaultConfig()
	c.Classic.UseVolatilityFilter = false
	c.Classic.UseRegimeFilter = false
	c.Classic.UseKernelFilter = false
	c.Classic.MaxBarsBack = 80
	c.Daily.Enabled = true
	c.EnvelopeEnabled = true
	c.Pullback.Enabled = true
	c.EntryMode = "both"
	old, err := RunRobust(rows, daily, c)
	if err != nil {
		t.Fatal(err)
	}
	for _, variant := range []string{"classic-original", "rq-direction", "momentum-four", "classic-recent"} {
		w := MechanismConfig{MechanismConfigVersion, variant, c}
		full, a, err := RunMechanismRobust(rows, daily, w)
		if err != nil {
			t.Fatal(err)
		}
		if variant == "classic-original" && (!reflect.DeepEqual(full.Points, old.Points) || !reflect.DeepEqual(full.Events, old.Events) || !reflect.DeepEqual(full.FeatureReady, old.FeatureReady)) {
			t.Fatal("original full strategy changed")
		}
		for _, n := range []int{210, 375} {
			part, b, err := RunMechanismRobust(rows[:n], daily, w)
			if err != nil {
				t.Fatal(err)
			}
			if !reflect.DeepEqual(part.Points, full.Points[:n]) || !reflect.DeepEqual(b, a[:n]) {
				t.Fatalf("%s prefix strategy differs", variant)
			}
			var events []Event
			for _, event := range full.Events {
				if event.Bar < n {
					events = append(events, event)
				}
			}
			if len(events) == 0 {
				events = []Event{}
			}
			if !reflect.DeepEqual(part.Events, events) {
				t.Fatalf("%s daily/pullback prefix differs", variant)
			}
			for _, event := range part.Events {
				if event.AvailableTime > event.DecisionTime || event.DecisionTime != rows[event.Bar].CloseTime+1 {
					t.Fatal("noncausal event")
				}
			}
		}
	}
}

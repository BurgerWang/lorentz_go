package strategy

import (
	"encoding/json"
	"lorentzgo/indicator"
	"lorentzgo/market"
	"math"
	"reflect"
	"strings"
	"testing"
)

func fixture(n int) ([]market.Candle, []market.Candle) {
	const day int64 = 86400000
	const hour int64 = 3600000
	start := int64(1704067200000)
	rows := make([]market.Candle, n)
	for i := range rows {
		v := 100 + 8*math.Sin(float64(i)/11)
		t := start + int64(i)*hour
		rows[i] = market.Candle{OpenTime: t, CloseTime: t + hour - 1, Open: v, High: v + 1, Low: v - 1, Close: v, Volume: 100}
	}
	daily := make([]market.Candle, 500)
	for i := range daily {
		v := 100 + float64(i)/10
		t := start - 400*day + int64(i)*day
		daily[i] = market.Candle{OpenTime: t, CloseTime: t + day - 1, Open: v, High: v + 1, Low: v - 1, Close: v, Volume: 100}
	}
	return rows, daily
}
func TestStrictConfig(t *testing.T) {
	c := DefaultConfig()
	raw, _ := json.Marshal(c)
	if _, e := DecodeConfig(raw); e != nil {
		t.Fatal(e)
	}
	for _, s := range []string{strings.Replace(string(raw), `"schema_version":`, `"schema_versionx":`, 1), strings.Replace(string(raw), `"enabled":false`, `"enabled":false,"enabled":true`, 1), strings.Replace(string(raw), `"history_start":"2020-01-01"`, `"history_start":null`, 1), strings.Replace(string(raw), `"envelope_enabled":false,`, "", 1)} {
		if _, e := DecodeConfig([]byte(s)); e == nil {
			t.Fatal("accepted invalid config")
		}
	}
}
func TestDisabledParityDailyFilteringAndPrefix(t *testing.T) {
	rows, daily := fixture(800)
	c := DefaultConfig()
	c.Classic.UseVolatilityFilter = false
	c.Classic.UseRegimeFilter = false
	c.Classic.UseKernelFilter = false
	old, e := indicator.Run(rows, c.Classic)
	if e != nil {
		t.Fatal(e)
	}
	r, e := Run(rows, daily, c)
	if e != nil {
		t.Fatal(e)
	}
	if !reflect.DeepEqual(old, r.Points) {
		t.Fatal("disabled enhanced path changed Classic")
	}
	c.Daily.Enabled = true
	c.EnvelopeEnabled = true
	r, e = Run(rows, daily, c)
	if e != nil {
		t.Fatal(e)
	}
	blocked := 0
	for i, p := range r.Points {
		if p.EndLong != old[i].EndLong || p.EndShort != old[i].EndShort || p.Signal != old[i].Signal || p.Prediction != old[i].Prediction {
			t.Fatal("filter altered exits/classifier")
		}
		if old[i].StartShort {
			blocked++
			if p.StartShort {
				t.Fatal("D1 up allowed short")
			}
		}
	}
	if blocked == 0 {
		t.Fatal("no filtered entries exercised")
	}
	for _, n := range []int{100, 400, 700} {
		part, e := Run(rows[:n], daily, c)
		if e != nil {
			t.Fatal(e)
		}
		if !reflect.DeepEqual(part.Points, r.Points[:n]) {
			t.Fatal("future changed past decisions")
		}
		for _, event := range part.Events {
			if event.DecisionTime != rows[event.Bar].CloseTime+1 || event.AvailableTime > event.DecisionTime {
				t.Fatal("bad availability")
			}
		}
	}
}

func TestEntryConflictPriorityAndVersionCompatibility(t *testing.T) {
	rows, _ := fixture(3)
	c := DefaultConfig()
	c.EntryMode = "both"
	c.Pullback.Enabled = true
	c.EnvelopeEnabled = true
	r := Result{Points: make([]indicator.Point, 3), Events: []Event{{Kind: "pullback", Direction: 1, Bar: 0, Allowed: true}, {Kind: "main_entry", Direction: 1, Bar: 0, Allowed: true}, {Kind: "main_entry", Direction: 1, Bar: 1, Allowed: true}, {Kind: "pullback", Direction: -1, Bar: 1, Allowed: true}}}
	r.Points[1].EndLong = true
	SelectEntries(&r, rows, c)
	m := EntryMetadata(r, c)
	if !r.Points[0].StartLong || m[0].Kind != "main_entry" || r.Points[1].StartLong || r.Points[1].StartShort || !r.Points[1].EndLong {
		t.Fatal("event priority/conflict changed")
	}
	v1 := DefaultConfig()
	v1.SchemaVersion = 1
	raw, _ := json.Marshal(v1)
	got, e := DecodeConfig(raw)
	if e != nil || got.SchemaVersion != 1 || got.EntryMode != "main" || got.Pullback.Enabled {
		t.Fatal("v1 no longer replayable", e)
	}
}

func TestFeatureUnavailableInvalidatesPullback(t *testing.T) {
	rows, daily := fixture(800)
	for i := 250; i < 450; i++ {
		rows[i].Volume = 0
	}
	c := DefaultConfig()
	c.Classic.UseVolatilityFilter = false
	c.Classic.UseRegimeFilter = false
	c.Classic.UseKernelFilter = false
	c.EnvelopeEnabled = true
	c.Envelope.X = 0
	c.Envelope.ATRLength = 2
	c.Pullback.Enabled = true
	c.Pullback.MaxWaitBars = 96
	c.EntryMode = "both"
	c.Classifier.FeatureGroup.Name = "classic-rvol"
	c.Classifier.FeatureGroup.Features = append(c.Classifier.FeatureGroup.Features, indicator.FeatureSpec{Name: "RVOL", A: 3, B: 1, Normalization: "rolling-z", Window: 2})
	r, e := Run(rows, daily, c)
	if e != nil {
		t.Fatal(e)
	}
	missing := 0
	for _, ready := range r.FeatureReady {
		if !ready {
			missing++
		}
	}
	if missing < 190 {
		t.Fatal("zero-volume history did not create missing features")
	}
	for _, e := range r.Events {
		if e.Kind == "pullback" && !r.FeatureReady[e.Bar] {
			t.Fatal("feature gap leaked pullback")
		}
	}
}

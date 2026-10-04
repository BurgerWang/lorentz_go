package strategy

import (
	"fmt"
	"lorentzgo/indicator"
)

type ClassifierConfig struct {
	Family       string                      `json:"family"`
	FeatureGroup indicator.NamedFeatureGroup `json:"feature_group"`
}

func ClassicClassifier(c indicator.Config) ClassifierConfig {
	specs := []indicator.FeatureSpec{}
	for _, f := range c.Features[:c.FeatureCount] {
		specs = append(specs, indicator.FeatureSpec{Name: f.Name, A: f.A, B: f.B, Normalization: "legacy"})
	}
	return ClassifierConfig{Family: "classic-extended", FeatureGroup: indicator.NamedFeatureGroup{Name: "classic", Features: specs}}
}
func (c ClassifierConfig) Validate(base indicator.Config) error {
	if c.Family != "classic-extended" && c.Family != "aligned-extended" {
		return fmt.Errorf("unsupported classifier family %q", c.Family)
	}
	if c.Family == "classic-extended" && base.Algorithm != "original-online" || c.Family == "aligned-extended" && base.Algorithm != "aligned-knn" {
		return fmt.Errorf("classifier family and algorithm mismatch")
	}
	if c.Family == "aligned-extended" && base.IncludeFullHistory {
		return fmt.Errorf("aligned-extended uses a rolling window; include_full_history must be false")
	}
	if e := c.FeatureGroup.Validate(); e != nil {
		return e
	}
	expected := ClassicClassifier(base).FeatureGroup.Features
	extras := map[string][]string{"classic": {}, "classic-rvol": {"RVOL"}, "classic-atr-price": {"ATR_PRICE"}, "classic-kernel-deviation": {"KERNEL_DEVIATION"}, "classic-d1-slope": {"D1_SLOPE"}, "classic-rvol-atr-price": {"RVOL", "ATR_PRICE"}}
	names, ok := extras[c.FeatureGroup.Name]
	if !ok {
		return fmt.Errorf("unsupported named feature group")
	}
	if len(c.FeatureGroup.Features) != len(expected)+len(names) {
		return fmt.Errorf("feature group length mismatch")
	}
	for i, f := range expected {
		if f != c.FeatureGroup.Features[i] {
			return fmt.Errorf("control features must match Classic ordered active slots")
		}
	}
	for j, n := range names {
		if c.FeatureGroup.Features[len(expected)+j].Name != n {
			return fmt.Errorf("feature group type/order mismatch")
		}
	}
	return nil
}
func (c Config) NeedsDaily() bool {
	if c.Daily.Enabled {
		return true
	}
	if c.SchemaVersion >= 3 {
		for _, f := range c.Classifier.FeatureGroup.Features {
			if f.Name == "D1_SLOPE" {
				return true
			}
		}
	}
	return false
}
func (c Config) NeedsEnvelope() bool {
	if c.EnvelopeEnabled {
		return true
	}
	if c.SchemaVersion >= 3 {
		for _, f := range c.Classifier.FeatureGroup.Features {
			if f.Name == "KERNEL_DEVIATION" {
				return true
			}
		}
	}
	return false
}

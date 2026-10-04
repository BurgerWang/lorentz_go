//go:build mechanism_p3

package strategy

import (
	"encoding/json"
	"fmt"
	"lorentzgo/indicator"
	"lorentzgo/internal/strictjson"
	"lorentzgo/market"
)

const MechanismConfigVersion = "mechanism-p3-config-v1"

type MechanismConfig struct {
	Version  string `json:"version"`
	Variant  string `json:"variant"`
	Strategy Config `json:"strategy"`
}

func (c MechanismConfig) Validate() error {
	if c.Version != MechanismConfigVersion {
		return fmt.Errorf("unsupported mechanism config version")
	}
	switch c.Variant {
	case "classic-original", "rq-direction", "momentum-four", "classic-recent":
	default:
		return fmt.Errorf("unknown P3 variant %q", c.Variant)
	}
	if c.Strategy.SchemaVersion != 5 || c.Strategy.Classifier.Family != "classic-extended" || c.Strategy.Classic.Algorithm != "original-online" || c.Strategy.Classic.SampleStride != 4 {
		return fmt.Errorf("P3 requires schema5 classic-extended original-online with absolute Classic stride 4")
	}
	return c.Strategy.Validate()
}
func DecodeMechanismConfig(raw []byte) (MechanismConfig, error) {
	var wire struct {
		Version  string          `json:"version"`
		Variant  string          `json:"variant"`
		Strategy json.RawMessage `json:"strategy"`
	}
	if err := strictjson.Decode(raw, &wire); err != nil {
		return MechanismConfig{}, err
	}
	c, err := DecodeConfig(wire.Strategy)
	if err != nil {
		return MechanismConfig{}, err
	}
	w := MechanismConfig{wire.Version, wire.Variant, c}
	return w, w.Validate()
}

type mechanismInputRunner func([]indicator.Inputs, indicator.Config, [][]float64) ([]indicator.Point, error)

func RunMechanismRobust(rows, daily []market.Candle, c MechanismConfig) (Result, []indicator.MechanismObservation, error) {
	if err := c.Validate(); err != nil {
		return Result{}, nil, err
	}
	var observations []indicator.MechanismObservation
	runner := func(x []indicator.Inputs, cfg indicator.Config, vectors [][]float64) ([]indicator.Point, error) {
		points, audit, err := indicator.RunMechanismInputs(x, cfg, vectors, c.Variant)
		observations = audit
		return points, err
	}
	r, err := runWithMechanism(rows, daily, c.Strategy, true, runner)
	// The indicator sidecar exposes pre daily/risk/entry-selection state; its
	// point time is the actual causal close, independent of those entry gates.
	for i := range observations {
		observations[i].Point.Time = rows[i].CloseTime
	}
	return r, observations, err
}

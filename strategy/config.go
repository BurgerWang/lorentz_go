// Package strategy contains explicitly versioned offline enhancements. Classic
// configuration and output retain their original contracts.
package strategy

import (
	"encoding/json"
	"fmt"
	"lorentzgo/backtest"
	"lorentzgo/indicator"
	"lorentzgo/internal/strictjson"
	"math"
)

type DailyConfig struct {
	Enabled      bool    `json:"enabled"`
	H            int     `json:"h"`
	R            float64 `json:"r"`
	X            int     `json:"x"`
	HistoryStart string  `json:"history_start"`
}
type Config struct {
	SchemaVersion   int                      `json:"schema_version"`
	Classic         indicator.Config         `json:"classic"`
	EnvelopeEnabled bool                     `json:"envelope_enabled"`
	Envelope        indicator.EnvelopeConfig `json:"envelope"`
	Daily           DailyConfig              `json:"daily"`
	EntryMode       string                   `json:"entry_mode"`
	Pullback        PullbackConfig           `json:"pullback"`
	Classifier      ClassifierConfig         `json:"classifier"`
	Exit            ExitConfig               `json:"exit"`
	Risk            backtest.RiskConfig      `json:"risk"`
	Model           indicator.ModelConfig    `json:"model"`
}

func DefaultConfig() Config {
	c := indicator.DefaultConfig()
	c.Algorithm = "original-online"
	return Config{SchemaVersion: 5, Model: indicator.DefaultModelConfig(), Exit: DefaultExitConfig(), Risk: backtest.DefaultRiskConfig(), Classifier: ClassicClassifier(c), EntryMode: "main", Pullback: DefaultPullbackConfig(), Classic: c, Envelope: indicator.DefaultEnvelopeConfig(), Daily: DailyConfig{H: 8, R: 8, X: 25, HistoryStart: "2020-01-01"}}
}

type configV1 struct {
	SchemaVersion   int                      `json:"schema_version"`
	Classic         indicator.Config         `json:"classic"`
	EnvelopeEnabled bool                     `json:"envelope_enabled"`
	Envelope        indicator.EnvelopeConfig `json:"envelope"`
	Daily           DailyConfig              `json:"daily"`
}
type configV2 struct {
	SchemaVersion   int                      `json:"schema_version"`
	Classic         indicator.Config         `json:"classic"`
	EnvelopeEnabled bool                     `json:"envelope_enabled"`
	Envelope        indicator.EnvelopeConfig `json:"envelope"`
	Daily           DailyConfig              `json:"daily"`
	EntryMode       string                   `json:"entry_mode"`
	Pullback        PullbackConfig           `json:"pullback"`
}

type configV3 struct {
	SchemaVersion   int                      `json:"schema_version"`
	Classic         indicator.Config         `json:"classic"`
	EnvelopeEnabled bool                     `json:"envelope_enabled"`
	Envelope        indicator.EnvelopeConfig `json:"envelope"`
	Daily           DailyConfig              `json:"daily"`
	EntryMode       string                   `json:"entry_mode"`
	Pullback        PullbackConfig           `json:"pullback"`
	Classifier      ClassifierConfig         `json:"classifier"`
}

type configV4 struct {
	SchemaVersion   int                      `json:"schema_version"`
	Classic         indicator.Config         `json:"classic"`
	EnvelopeEnabled bool                     `json:"envelope_enabled"`
	Envelope        indicator.EnvelopeConfig `json:"envelope"`
	Daily           DailyConfig              `json:"daily"`
	EntryMode       string                   `json:"entry_mode"`
	Pullback        PullbackConfig           `json:"pullback"`
	Classifier      ClassifierConfig         `json:"classifier"`
	Exit            ExitConfig               `json:"exit"`
	Risk            backtest.RiskConfig      `json:"risk"`
}

type configV5 struct {
	SchemaVersion   int                      `json:"schema_version"`
	Classic         indicator.Config         `json:"classic"`
	EnvelopeEnabled bool                     `json:"envelope_enabled"`
	Envelope        indicator.EnvelopeConfig `json:"envelope"`
	Daily           DailyConfig              `json:"daily"`
	EntryMode       string                   `json:"entry_mode"`
	Pullback        PullbackConfig           `json:"pullback"`
	Classifier      ClassifierConfig         `json:"classifier"`
	Exit            ExitConfig               `json:"exit"`
	Risk            backtest.RiskConfig      `json:"risk"`
	Model           indicator.ModelConfig    `json:"model"`
}

func (c Config) MarshalJSON() ([]byte, error) {
	if c.SchemaVersion == 1 {
		return json.Marshal(configV1{c.SchemaVersion, c.Classic, c.EnvelopeEnabled, c.Envelope, c.Daily})
	}
	if c.SchemaVersion >= 5 {
		return json.Marshal(configV5{c.SchemaVersion, c.Classic, c.EnvelopeEnabled, c.Envelope, c.Daily, c.EntryMode, c.Pullback, c.Classifier, c.Exit, c.Risk, c.Model})
	}
	if c.SchemaVersion >= 4 {
		return json.Marshal(configV4{c.SchemaVersion, c.Classic, c.EnvelopeEnabled, c.Envelope, c.Daily, c.EntryMode, c.Pullback, c.Classifier, c.Exit, c.Risk})
	}
	if c.SchemaVersion >= 3 {
		return json.Marshal(configV3{c.SchemaVersion, c.Classic, c.EnvelopeEnabled, c.Envelope, c.Daily, c.EntryMode, c.Pullback, c.Classifier})
	}
	return json.Marshal(configV2{c.SchemaVersion, c.Classic, c.EnvelopeEnabled, c.Envelope, c.Daily, c.EntryMode, c.Pullback})
}
func DecodeConfig(raw []byte) (Config, error) {
	var version struct {
		SchemaVersion int `json:"schema_version"`
	}
	if e := json.Unmarshal(raw, &version); e != nil {
		return Config{}, e
	}
	c := DefaultConfig()
	switch version.SchemaVersion {
	case 1:
		var w configV1
		if e := strictjson.Decode(raw, &w); e != nil {
			return c, e
		}
		c.SchemaVersion, c.Classic, c.EnvelopeEnabled, c.Envelope, c.Daily = w.SchemaVersion, w.Classic, w.EnvelopeEnabled, w.Envelope, w.Daily
	case 2:
		var w configV2
		if e := strictjson.Decode(raw, &w); e != nil {
			return c, e
		}
		c.SchemaVersion, c.Classic, c.EnvelopeEnabled, c.Envelope, c.Daily, c.EntryMode, c.Pullback = w.SchemaVersion, w.Classic, w.EnvelopeEnabled, w.Envelope, w.Daily, w.EntryMode, w.Pullback
	case 3:
		var w configV3
		if e := strictjson.Decode(raw, &w); e != nil {
			return c, e
		}
		c.SchemaVersion, c.Classic, c.EnvelopeEnabled, c.Envelope, c.Daily, c.EntryMode, c.Pullback, c.Classifier = w.SchemaVersion, w.Classic, w.EnvelopeEnabled, w.Envelope, w.Daily, w.EntryMode, w.Pullback, w.Classifier
	case 4:
		var w configV4
		if e := strictjson.Decode(raw, &w); e != nil {
			return c, e
		}
		c.SchemaVersion, c.Classic, c.EnvelopeEnabled, c.Envelope, c.Daily, c.EntryMode, c.Pullback, c.Classifier, c.Exit, c.Risk = w.SchemaVersion, w.Classic, w.EnvelopeEnabled, w.Envelope, w.Daily, w.EntryMode, w.Pullback, w.Classifier, w.Exit, w.Risk
	case 5:
		var w configV5
		if e := strictjson.Decode(raw, &w); e != nil {
			return c, e
		}
		c.SchemaVersion, c.Classic, c.EnvelopeEnabled, c.Envelope, c.Daily, c.EntryMode, c.Pullback, c.Classifier, c.Exit, c.Risk, c.Model = w.SchemaVersion, w.Classic, w.EnvelopeEnabled, w.Envelope, w.Daily, w.EntryMode, w.Pullback, w.Classifier, w.Exit, w.Risk, w.Model
	default:
		return c, fmt.Errorf("unsupported strategy schema %d", version.SchemaVersion)
	}
	if c.SchemaVersion < 3 {
		c.Classifier = ClassicClassifier(c.Classic)
	}
	return c, c.Validate()
}
func (c Config) Validate() error {
	if c.SchemaVersion < 1 || c.SchemaVersion > 5 {
		return fmt.Errorf("unsupported strategy schema %d", c.SchemaVersion)
	}
	if c.EntryMode != "main" && c.EntryMode != "pullback" && c.EntryMode != "both" {
		return fmt.Errorf("invalid entry mode")
	}
	if e := c.Pullback.Validate(); e != nil {
		return e
	}
	if c.SchemaVersion == 1 && (c.Pullback.Enabled || c.EntryMode != "main") {
		return fmt.Errorf("v1 cannot enable pullback")
	}
	if c.Pullback.Enabled && !c.EnvelopeEnabled {
		return fmt.Errorf("pullback requires envelope")
	}
	if c.EntryMode != "main" && !c.Pullback.Enabled {
		return fmt.Errorf("entry mode requires pullback")
	}
	if e := c.Classic.Validate(); e != nil {
		return e
	}
	if c.SchemaVersion < 5 && c.Classic.Algorithm != "original-online" {
		return fmt.Errorf("strategy schema1-4 requires original-online")
	}
	if c.SchemaVersion < 5 && c.Classifier.Family != "classic-extended" {
		return fmt.Errorf("aligned-extended requires schema5")
	}
	if c.SchemaVersion < 5 && c.Model != indicator.DefaultModelConfig() {
		return fmt.Errorf("model variants require schema5")
	}
	if c.SchemaVersion >= 5 {
		if e := c.Model.Validate(); e != nil {
			return e
		}
		if c.Classifier.Family == "classic-extended" && c.Model != indicator.DefaultModelConfig() {
			return fmt.Errorf("classic-extended cannot use aligned model parameters")
		}
	}
	if c.SchemaVersion >= 3 {
		if e := c.Classifier.Validate(c.Classic); e != nil {
			return e
		}
	}
	if c.SchemaVersion < 4 && (c.Risk != backtest.DefaultRiskConfig() || c.Exit != DefaultExitConfig()) {
		return fmt.Errorf("risk/exit require schema4+")
	}
	if c.SchemaVersion >= 4 {
		if e := c.Exit.Validate(); e != nil {
			return e
		}
		if e := c.Risk.Validate(); e != nil {
			return e
		}
	}
	if e := c.Envelope.Validate(); e != nil {
		return e
	}
	d := c.Daily
	if d.H < 3 || d.X < 0 || d.X > 1000 || !finite(d.R) || d.R <= 0 || d.HistoryStart == "" {
		return fmt.Errorf("invalid daily kernel/initialization")
	}
	return nil
}
func finite(v float64) bool { return !math.IsNaN(v) && !math.IsInf(v, 0) }

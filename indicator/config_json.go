package indicator

import (
	"fmt"

	"lorentzgo/internal/strictjson"
)

// DecodeConfig requires a complete configuration. Missing/null fields and silent
// truncation of feature arrays would make exported experiments irreproducible.
func DecodeConfig(data []byte) (Config, error) {
	var c Config
	if err := strictjson.Decode(data, &c); err != nil {
		return c, fmt.Errorf("indicator config: %w", err)
	}
	return c, c.Validate()
}

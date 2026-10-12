package indicator

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestDecodeConfigFullRoundTripAndRejectedAmbiguities(t *testing.T) {
	c := DefaultConfig()
	b, _ := json.Marshal(c)
	got, err := DecodeConfig(b)
	if err != nil || got != c {
		t.Fatalf("round trip: %+v %v", got, err)
	}
	for _, mutate := range []func(map[string]any){
		func(m map[string]any) { delete(m, "use_adx_filter") },
		func(m map[string]any) { m["use_adx_filter"] = nil },
		func(m map[string]any) { m["unknown"] = true },
		func(m map[string]any) { m["features"] = m["features"].([]any)[:4] },
		func(m map[string]any) {
			m["features"] = append(m["features"].([]any), map[string]any{"name": "RSI", "a": 14, "b": 1})
		},
		func(m map[string]any) { m["features"].([]any)[0].(map[string]any)["b"] = nil },
		func(m map[string]any) { m["features"].([]any)[0].(map[string]any)["extra"] = 1 },
	} {
		var m map[string]any
		json.Unmarshal(b, &m)
		mutate(m)
		bad, _ := json.Marshal(m)
		if _, err := DecodeConfig(bad); err == nil {
			t.Fatalf("accepted %s", bad)
		}
	}
	for _, bad := range [][]byte{[]byte("null"), append(append([]byte{}, b...), []byte(" {}")...)} {
		if _, err := DecodeConfig(bad); err == nil {
			t.Fatal("accepted null/trailing input")
		}
	}
}

func TestDecodeConfigRejectsDuplicateAndCaseAliasKeys(t *testing.T) {
	raw, err := json.Marshal(DefaultConfig())
	if err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct{ name, old, replacement string }{
		{"config duplicate", `"neighbors":8`, `"neighbors":8,"neighbors":99`},
		{"config escaped duplicate", `"neighbors":8`, `"neighbors":8,"neighb\u006frs":99`},
		{"config case alias", `"neighbors":8`, `"neighbors":8,"NEIGHBORS":99`},
		{"config renamed key", `"neighbors":8`, `"Neighbors":8`},
		{"feature duplicate", `"name":"RSI"`, `"name":"RSI","name":"WT"`},
		{"feature escaped duplicate", `"name":"RSI"`, `"name":"RSI","n\u0061me":"WT"`},
		{"feature case alias", `"name":"RSI"`, `"name":"RSI","NAME":"WT"`},
		{"feature renamed key", `"name":"RSI"`, `"NAME":"RSI"`},
		{"feature missing", `"name":"RSI",`, ``},
		{"feature null object", `{"name":"RSI","a":14,"b":1}`, `null`},
	} {
		t.Run(tc.name, func(t *testing.T) {
			bad := strings.Replace(string(raw), tc.old, tc.replacement, 1)
			if bad == string(raw) {
				t.Fatal("mutation did not apply")
			}
			if _, err := DecodeConfig([]byte(bad)); err == nil {
				t.Fatalf("accepted ambiguous config: %s", bad)
			}
		})
	}
	// JSON escapes are valid spelling when the decoded key occurs exactly once.
	escaped := strings.Replace(string(raw), `"neighbors"`, `"neighb\u006frs"`, 1)
	got, err := DecodeConfig([]byte(escaped))
	if err != nil || got != DefaultConfig() {
		t.Fatalf("valid escaped key round trip: %+v %v", got, err)
	}
}

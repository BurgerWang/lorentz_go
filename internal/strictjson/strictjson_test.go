package strictjson

import (
	"encoding/json"
	"testing"
)

func TestDecodeCompleteSchemaAndDeferredRawMessage(t *testing.T) {
	type schema struct {
		Count int             `json:"count"`
		Flag  bool            `json:"flag"`
		Raw   json.RawMessage `json:"raw"`
	}
	var value schema
	// The raw field retains its exact bytes for validation by its own decoder.
	valid := `{"count":0,"flag":false,"raw":{"key":1,"key":2}}`
	if err := Decode([]byte(valid), &value); err != nil || value.Count != 0 || value.Flag || string(value.Raw) != `{"key":1,"key":2}` {
		t.Fatalf("complete explicit zero/false/raw values: %+v %v", value, err)
	}
	for _, bad := range []string{
		`null`,
		`[]`,
		`{"count":0,"flag":false}`,
		`{"count":0,"flag":false,"raw":null}`,
		`{"count":null,"flag":false,"raw":{}}`,
		`{"count":0,"flag":null,"raw":{}}`,
		`{"count":"0","flag":false,"raw":{}}`,
		`{"count":0.5,"flag":false,"raw":{}}`,
		`{"count":0,"flag":0,"raw":{}}`,
		`{"count":0,"flag":false,"raw":{},"extra":1}`,
		`{"count":0,"flag":false,"raw":{},"COUNT":1}`,
		`{"count":0,"flag":false,"raw":{},"co\u0075nt":1}`,
		`{"count":0,"flag":false,"raw":{}} {}`,
		`{"count":0,"flag":false,"raw":{}} garbage`,
	} {
		if err := Decode([]byte(bad), &value); err == nil {
			t.Fatalf("accepted %s", bad)
		}
	}
}

func TestDecodeArrayLengthsAndNullEntries(t *testing.T) {
	type entry struct {
		Name string `json:"name"`
	}
	type schema struct {
		Items [1]entry `json:"items"`
	}
	var value schema
	if err := Decode([]byte(`{"items":[{"name":"a"}]}`), &value); err != nil || value.Items[0].Name != "a" {
		t.Fatalf("complete array: %+v %v", value, err)
	}
	for _, bad := range []string{
		`{"items":[]}`,
		`{"items":[{"name":"a"},{"name":"b"}]}`,
		`{"items":null}`,
		`{"items":[null]}`,
		`{"items":[{}]}`,
		`{"items":[{"name":"a","name":"b"}]}`,
	} {
		if err := Decode([]byte(bad), &value); err == nil {
			t.Fatalf("accepted %s", bad)
		}
	}
}

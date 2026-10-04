// Package strictjson decodes complete, explicitly tagged JSON schemas.
package strictjson

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"reflect"
)

// Decode requires every schema field, exact key spelling and unique object keys.
// RawMessage fields are deliberately deferred to their own schema decoder.
func Decode(data []byte, dst any) error {
	d := json.NewDecoder(bytes.NewReader(data))
	d.DisallowUnknownFields()
	if err := d.Decode(dst); err != nil {
		return err
	}
	var extra any
	if err := d.Decode(&extra); err != io.EOF {
		return fmt.Errorf("unexpected trailing JSON")
	}
	return required(data, reflect.TypeOf(dst).Elem())
}

func required(raw json.RawMessage, typ reflect.Type) error {
	if bytes.Equal(bytes.TrimSpace(raw), []byte("null")) {
		return fmt.Errorf("null is not a complete value")
	}
	if typ == reflect.TypeOf(json.RawMessage{}) {
		return nil
	}
	switch typ.Kind() {
	case reflect.Struct:
		fields := make(map[string]reflect.Type, typ.NumField())
		for i := 0; i < typ.NumField(); i++ {
			f := typ.Field(i)
			fields[f.Tag.Get("json")] = f.Type
		}
		d := json.NewDecoder(bytes.NewReader(raw))
		token, err := d.Token()
		if err != nil {
			return err
		}
		if token != json.Delim('{') {
			return fmt.Errorf("expected an object")
		}
		seen := make(map[string]bool, len(fields))
		for d.More() {
			token, err := d.Token()
			if err != nil {
				return err
			}
			name, ok := token.(string)
			if !ok {
				return fmt.Errorf("expected an object key")
			}
			fieldType, ok := fields[name]
			if !ok {
				return fmt.Errorf("unknown field %q", name)
			}
			if seen[name] {
				return fmt.Errorf("duplicate field %q", name)
			}
			seen[name] = true
			var value json.RawMessage
			if err := d.Decode(&value); err != nil {
				return err
			}
			if err := required(value, fieldType); err != nil {
				return fmt.Errorf("%s: %w", name, err)
			}
		}
		if _, err := d.Token(); err != nil {
			return err
		}
		for i := 0; i < typ.NumField(); i++ {
			name := typ.Field(i).Tag.Get("json")
			if !seen[name] {
				return fmt.Errorf("missing field %s", name)
			}
		}
	case reflect.Array, reflect.Slice:
		var items []json.RawMessage
		if err := json.Unmarshal(raw, &items); err != nil {
			return err
		}
		if typ.Kind() == reflect.Array && len(items) != typ.Len() {
			return fmt.Errorf("expected %d array entries", typ.Len())
		}
		for i, item := range items {
			if err := required(item, typ.Elem()); err != nil {
				return fmt.Errorf("entry %d: %w", i, err)
			}
		}
	}
	return nil
}

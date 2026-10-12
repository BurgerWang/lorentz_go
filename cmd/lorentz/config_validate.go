package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"lorentzgo/strategy"
	"os"
)

// No dataset or strategy execution is needed for configuration validation.
func configValidate(args []string) error {
	f := flag.NewFlagSet("config-validate", flag.ContinueOnError)
	path := f.String("config", "", "complete strategy config")
	batch := f.Bool("batch", false, "validate a JSON array from stdin")
	if e := f.Parse(args); e != nil {
		return e
	}
	if f.NArg() != 0 || (*batch && *path != "") {
		return fmt.Errorf("invalid configuration validation arguments")
	}
	enc := json.NewEncoder(os.Stdout)
	if !*batch && *path == "" {
		return enc.Encode(strategy.DefaultConfig())
	}
	var raw []byte
	var e error
	if *batch {
		raw, e = io.ReadAll(io.LimitReader(os.Stdin, 32*1024*1024))
	} else {
		raw, e = os.ReadFile(*path)
	}
	if e != nil {
		return e
	}
	if !*batch {
		c, e := strategy.DecodeConfig(raw)
		if e != nil {
			return e
		}
		return enc.Encode(c)
	}
	var configs []json.RawMessage
	if e = json.Unmarshal(raw, &configs); e != nil {
		return e
	}
	if len(configs) == 0 {
		return fmt.Errorf("empty configuration batch")
	}
	result := make([]strategy.Config, 0, len(configs))
	for i, v := range configs {
		c, e := strategy.DecodeConfig(v)
		if e != nil {
			return fmt.Errorf("config %d: %w", i, e)
		}
		result = append(result, c)
	}
	return enc.Encode(result)
}

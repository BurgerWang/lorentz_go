//go:build mechanism_p3

package main

import (
	"bufio"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"lorentzgo/backtest"
	"lorentzgo/indicator"
	"lorentzgo/market"
	"lorentzgo/strategy"
	"os"
	"path/filepath"
	"time"
)

var mechanismVariants = []string{"classic-original", "rq-direction", "momentum-four", "classic-recent"}

func mechanismReady(ds *robustDataset) map[string]any {
	r := ds.ready()
	r["mechanism_contract"] = indicator.MechanismP3Contract
	r["supported_variants"] = mechanismVariants
	r["default_config"] = strategy.MechanismConfig{Version: strategy.MechanismConfigVersion, Variant: "classic-original", Strategy: strategy.DefaultConfig()}
	return r
}
func mechanismValidate(ds *robustDataset, c strategy.MechanismConfig) error {
	if err := c.Validate(); err != nil {
		return err
	}
	if ds.plan.AccountMode != "fixed-continuous-v1" {
		return errors.New("P3 requires fixed-continuous-v1")
	}
	return ds.validate(c.Strategy)
}

func mechanismEvaluate(ds *robustDataset, c strategy.MechanismConfig, traceDir string) (map[string]any, error) {
	if err := mechanismValidate(ds, c); err != nil {
		return nil, err
	}
	// Local copy owns its runner and observations; no cross-request mutable mode.
	local := *ds
	var observations []indicator.MechanismObservation
	local.runner = func(rows, daily []market.Candle, _ strategy.Config) (strategy.Result, error) {
		r, audit, err := strategy.RunMechanismRobust(rows, daily, c)
		observations = audit
		return r, err
	}
	result, err := local.evaluate(c.Strategy, traceDir)
	if err != nil {
		return nil, err
	}
	result["config"] = c
	result["mechanism_contract"] = indicator.MechanismP3Contract
	path, hash := "", ""
	if traceDir != "" {
		path, err = filepath.Abs(filepath.Join(traceDir, "mechanism-indicator.jsonl"))
		if err != nil {
			return nil, err
		}
		f, e := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0644)
		if e != nil {
			return nil, e
		}
		h := sha256.New()
		enc := json.NewEncoder(io.MultiWriter(f, h))
		for _, row := range observations {
			if err = enc.Encode(row); err != nil {
				break
			}
		}
		syncErr, closeErr := f.Sync(), f.Close()
		if err == nil {
			err = syncErr
		}
		if err == nil {
			err = closeErr
		}
		if err != nil {
			return nil, err
		}
		hash = hex.EncodeToString(h.Sum(nil))
	}
	result["mechanism_trace"] = map[string]any{"version": "mechanism-indicator-v1", "path": path, "sha256": hash, "rows": len(observations), "emitted": traceDir != "", "variant": c.Variant}
	return result, nil
}

func mechanismEval(args []string) error {
	f := flag.NewFlagSet("mechanism-eval", flag.ContinueOnError)
	pp := f.String("plan", "", "robust-eval-v1 fixed continuous plan")
	cp := f.String("config", "", "complete mechanism-p3-config-v1 wrapper; omit for protocol3 server")
	inspect := f.Bool("inspect", false, "validate data/configuration without strategy evaluation")
	td := f.String("trace-dir", "", "exclusive ledger and mechanism state trace directory")
	if err := f.Parse(args); err != nil {
		return err
	}
	if *pp == "" || f.NArg() != 0 {
		return errors.New("mechanism-eval requires -plan")
	}
	raw, err := os.ReadFile(*pp)
	if err != nil {
		return err
	}
	var p RobustPlan
	if err = optimizeDecode(raw, &p); err != nil {
		return err
	}
	if p.AccountMode != "fixed-continuous-v1" {
		return errors.New("P3 requires fixed-continuous-v1")
	}
	started := time.Now()
	ds, err := robustLoad(p)
	if err != nil {
		return err
	}
	ready := mechanismReady(ds)
	ready["load_seconds"] = time.Since(started).Seconds()
	enc := json.NewEncoder(os.Stdout)
	if *cp != "" {
		raw, err = os.ReadFile(*cp)
		if err != nil {
			return err
		}
		c, e := strategy.DecodeMechanismConfig(raw)
		if e != nil {
			return e
		}
		if err = mechanismValidate(ds, c); err != nil {
			return err
		}
		ready["inspected_config"] = c
		ready["variant"] = c.Variant
		if *inspect {
			return enc.Encode(ready)
		}
		result, e := mechanismEvaluate(ds, c, *td)
		if e != nil {
			return e
		}
		result["load_seconds"] = ready["load_seconds"]
		return enc.Encode(result)
	}
	if *inspect {
		return enc.Encode(ready)
	}
	if err = enc.Encode(ready); err != nil {
		return err
	}
	scan := bufio.NewScanner(os.Stdin)
	scan.Buffer(make([]byte, 65536), 4*1024*1024)
	for scan.Scan() {
		var req struct {
			ID       int             `json:"id"`
			Config   json.RawMessage `json:"config"`
			TraceDir string          `json:"trace_dir"`
		}
		kind := "request"
		err = optimizeDecode(scan.Bytes(), &req)
		var c strategy.MechanismConfig
		if err == nil {
			kind = "config"
			c, err = strategy.DecodeMechanismConfig(req.Config)
			if err == nil {
				err = mechanismValidate(ds, c)
			}
		}
		var result map[string]any
		if err == nil {
			kind = "evaluation"
			result, err = mechanismEvaluate(ds, c, req.TraceDir)
		}
		if err != nil {
			var insufficient robustInsufficientError
			if errors.As(err, &insufficient) {
				kind = "configuration_insufficient"
			}
			var protection backtest.EquityProtectionError
			if errors.As(err, &protection) {
				kind = "equity_protection"
			}
			result = map[string]any{"type": "error", "error": err.Error(), "error_kind": kind}
		}
		result["id"] = req.ID
		if err = enc.Encode(result); err != nil {
			return err
		}
	}
	return scan.Err()
}

func mechanismConfigValidate(args []string) error {
	f := flag.NewFlagSet("mechanism-config-validate", flag.ContinueOnError)
	cp := f.String("config", "", "complete P3 wrapper")
	batch := f.Bool("batch", false, "wrapper JSON array from stdin")
	if err := f.Parse(args); err != nil {
		return err
	}
	if f.NArg() != 0 || (*batch && *cp != "") || (!*batch && *cp == "") {
		return fmt.Errorf("require -config or -batch")
	}
	var raw []byte
	var err error
	if *batch {
		raw, err = io.ReadAll(io.LimitReader(os.Stdin, 32*1024*1024))
	} else {
		raw, err = os.ReadFile(*cp)
	}
	if err != nil {
		return err
	}
	enc := json.NewEncoder(os.Stdout)
	if !*batch {
		c, e := strategy.DecodeMechanismConfig(raw)
		if e != nil {
			return e
		}
		return enc.Encode(c)
	}
	var items []json.RawMessage
	if err = json.Unmarshal(raw, &items); err != nil {
		return err
	}
	if len(items) == 0 {
		return errors.New("empty wrapper batch")
	}
	out := make([]strategy.MechanismConfig, 0, len(items))
	for i, item := range items {
		c, e := strategy.DecodeMechanismConfig(item)
		if e != nil {
			return fmt.Errorf("config %d: %w", i, e)
		}
		out = append(out, c)
	}
	return enc.Encode(out)
}

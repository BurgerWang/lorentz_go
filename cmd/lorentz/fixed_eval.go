package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"lorentzgo/strategy"
	"os"
)

// A fixed evaluation retains the complete walkforward time/exposure contract,
// but baseline is the one frozen configuration and candidates must be empty.
// Inner windows remain contract information; they never incur ledger calls.
func fixedNormalize(p walkPlan) (walkPlan, OptimizePlan, int, error) {
	if len(p.Candidates) != 0 {
		return p, OptimizePlan{}, 0, errors.New("fixed continuous evaluation requires an empty candidates list")
	}
	c, e := strategy.DecodeConfig(p.Baseline)
	if e != nil {
		return p, OptimizePlan{}, 0, fmt.Errorf("fixed configuration: %w", e)
	}
	if c.SchemaVersion != 5 {
		return p, OptimizePlan{}, 0, errors.New("fixed continuous evaluation requires a complete schema5 configuration")
	}
	const required = 3
	if p.Budget != required {
		return p, OptimizePlan{}, required, fmt.Errorf("budget must equal %d continuous ledger evaluations, one for each cost scenario", required)
	}
	// Reuse all existing window, initialization, selection-contract and cost
	// guards without adding a separate, diverging time-plan validator. The
	// duplicate is only a normalization adapter; no strategy is run here.
	adapter := p
	adapter.Candidates = []walkCandidate{{Name: "fixed-normalization-control", Config: p.Baseline}}
	adapter.Budget = 2*len(p.CostMultipliers) + 2*len(p.Outer)
	for _, o := range p.Outer {
		adapter.Budget += 2 * len(o.Inner)
	}
	normalized, base, _, e := walkNormalize(adapter)
	if e != nil {
		return p, OptimizePlan{}, 0, e
	}
	normalized.Candidates = []walkCandidate{}
	normalized.Budget = required
	return normalized, base, required, nil
}

func fixedPrepare(p walkPlan, ds *enhancedDataset, required int, exposureIdentity string) (*walkDataset, error) {
	w, e := walkPrepare(p, ds, required, exposureIdentity)
	if e != nil {
		return nil, e
	}
	return w, nil
}

func fixedLoad(p walkPlan) (*walkDataset, error) {
	p, base, required, e := fixedNormalize(p)
	if e != nil {
		return nil, e
	}
	raw, e := os.ReadFile(p.ExposureFile)
	if e != nil {
		return nil, e
	}
	if e := walkExposure(raw, p); e != nil {
		return nil, e
	}
	ds, e := enhancedLoad(base)
	if e != nil {
		return nil, e
	}
	h := sha256.Sum256(raw)
	return fixedPrepare(p, ds, required, hex.EncodeToString(h[:]))
}

func fixedIdentity(v any) string {
	// All values have already passed strict decoding and finite-number guards.
	raw, _ := json.Marshal(v)
	h := sha256.Sum256(raw)
	return hex.EncodeToString(h[:])
}

func fixedReady(w *walkDataset) map[string]any {
	r := w.ready()
	r["evaluation_kind"] = "fixed-continuous"
	r["strategy_schema_version"] = 5
	r["config"] = w.baseline
	r["plan_identity"] = fixedIdentity(w.plan)
	r["config_identity"] = fixedIdentity(w.baseline)
	r["daily_aggregation"] = w.ds.dailyAggregation
	return r
}

func fixedEvaluate(w *walkDataset) (map[string]any, error) {
	if w.required != 3 || w.plan.Budget != w.required || len(w.candidates) != 0 || w.baseline.SchemaVersion != 5 {
		return nil, errors.New("invalid fixed evaluation contract or reserved budget")
	}
	start, end := w.outer[0].start, w.outer[len(w.outer)-1].end
	signals, e := w.runTo(end, w.baseline)
	if e != nil {
		return nil, e
	}
	// Summary diagnostics include only scored decisions, as in enhanced-eval.
	scored := signals
	scored.Events = []strategy.Event{}
	for _, event := range signals.Events {
		if event.DecisionTime > start && event.DecisionTime <= end {
			scored.Events = append(scored.Events, event)
		}
	}
	scenarios := make([]map[string]any, 0, len(w.plan.CostMultipliers))
	used := 0
	for _, multiplier := range w.plan.CostMultipliers {
		// Exactly one common Go ledger per cost: no per-window resets, selection,
		// extra control accounts, or alternate accounting implementation.
		report, e := w.ledger(signals, w.baseline, start, end, multiplier, nil)
		used++
		if e != nil {
			return nil, e
		}
		summary, _, e := w.summarize(report, scored)
		if e != nil {
			return nil, e
		}
		scenarios = append(scenarios, map[string]any{"cost_multiplier": multiplier, "fixed": summary})
	}
	if used != w.required {
		return nil, fmt.Errorf("fixed evaluation accounting mismatch %d/%d", used, w.required)
	}
	r := fixedReady(w)
	r["type"] = "result"
	r["evaluations_used"] = used
	r["continuous_scenarios"] = scenarios
	r["data_use"] = w.plan.DataUse
	r["decision"] = map[string]any{"status": "fixed_evaluation_only", "data_use": w.plan.DataUse, "reasons": []string{"fixed account results require separately frozen comparisons and adoption criteria"}}
	r["assumptions"] = []string{
		"Budget counts exactly one continuous Go ledger account per cost multiplier (1, 1.5, 2). Failed batches reserve all three calls; no automatic retry.",
		"The baseline field is the single fixed schema5 configuration. Inner windows and selection rules are retained only as frozen time-plan information; no candidate selection or fold-reset account is evaluated.",
		"Position, cash, risk lines and funding remain continuous across outer windows. Close decisions execute at the next open; only the final exclusive end liquidates the account.",
		"Retrospective data exposure remains visible and cannot establish independent final performance. This entry does not decide adoption or compare against a separate global baseline.",
	}
	return r, nil
}

func fixedContinuousEval(args []string) error {
	f := flag.NewFlagSet("fixed-continuous-eval", flag.ContinueOnError)
	path := f.String("plan", "", "complete walkforward plan with one fixed schema5 baseline, empty candidates and budget=3")
	inspect := f.Bool("inspect", false, "validate contract/data and report actual ledger budget without evaluating strategy")
	if e := f.Parse(args); e != nil {
		return e
	}
	if *path == "" || f.NArg() != 0 {
		return errors.New("fixed-continuous-eval requires -plan and no positional args")
	}
	raw, e := os.ReadFile(*path)
	if e != nil {
		return e
	}
	var p walkPlan
	if e := optimizeDecode(raw, &p); e != nil {
		return e
	}
	w, e := fixedLoad(p)
	if e != nil {
		return e
	}
	enc := json.NewEncoder(os.Stdout)
	if *inspect {
		return enc.Encode(fixedReady(w))
	}
	r, e := fixedEvaluate(w)
	if e != nil {
		return e
	}
	return enc.Encode(r)
}

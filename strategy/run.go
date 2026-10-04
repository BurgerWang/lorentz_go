package strategy

import (
	"fmt"
	"lorentzgo/backtest"
	"lorentzgo/indicator"
	"lorentzgo/market"
)

type Event struct {
	Kind          string `json:"kind"`
	Direction     int    `json:"direction"`
	Bar           int    `json:"bar"`
	DecisionTime  int64  `json:"decision_time"`
	AvailableTime int64  `json:"available_time"`
	Phase         string `json:"phase"`
	Allowed       bool   `json:"allowed"`
	Reason        string `json:"reason"`
}
type Result struct {
	Points       []indicator.Point
	Events       []Event
	Envelope     []indicator.EnvelopePoint
	Daily        []market.DailyContext
	FeatureReady []bool
	Scores       []indicator.ModelScore
	RiskATR      []float64
}

// Run never changes Classic classifier state; only currently emitted entries
// are filtered. A blocked event is not replayed when daily direction changes.
func Run(rows, daily []market.Candle, c Config) (Result, error) {
	return run(rows, daily, c, false)
}

// RunRobust uses the same strategy with explicit unavailable D1 startup.
func RunRobust(rows, daily []market.Candle, c Config) (Result, error) {
	return run(rows, daily, c, true)
}
func run(rows, daily []market.Candle, c Config, initialization bool) (Result, error) {
	if e := c.Validate(); e != nil {
		return Result{}, e
	}
	if len(rows) == 0 {
		return Result{}, fmt.Errorf("empty strategy history")
	}
	r := Result{Events: []Event{}}
	var e error
	if c.NeedsEnvelope() {
		r.Envelope, e = indicator.ComputeEnvelope(rows, c.Envelope)
		if e != nil {
			return Result{}, e
		}
	}
	if c.NeedsDaily() {
		src := make([]float64, len(daily))
		for i := range daily {
			src[i] = daily[i].Close
		}
		centers, err := indicator.RationalQuadratic(src, c.Daily.H, c.Daily.R, c.Daily.X)
		if err != nil {
			return Result{}, err
		}
		if initialization {
			r.Daily, e = market.AlignDailyInitialization(rows, daily, centers)
		} else {
			r.Daily, e = market.AlignDaily(rows, daily, centers)
		}
		if e != nil {
			return Result{}, e
		}
	}
	var p []indicator.Point
	if c.SchemaVersion >= 3 {
		x, vectors, err := indicator.ComputeExtended(rows, c.Classic, c.Classifier.FeatureGroup.Features, r.Envelope, r.Daily)
		if err != nil {
			return Result{}, err
		}
		r.FeatureReady = make([]bool, len(vectors))
		for i, vector := range vectors {
			r.FeatureReady[i] = finite(x[i].Source)
			for _, v := range vector {
				r.FeatureReady[i] = r.FeatureReady[i] && finite(v)
			}
		}
		if c.Classifier.Family == "aligned-extended" {
			p, r.Scores, e = indicator.RunModelInputs(x, c.Classic, vectors, c.Model)
			for i, score := range r.Scores {
				r.FeatureReady[i] = r.FeatureReady[i] && score.Ready
			}
		} else {
			p, e = indicator.RunExtendedInputs(x, c.Classic, vectors)
		}
		for i := range p {
			p[i].Time = rows[i].CloseTime
		}
	} else {
		p, e = indicator.Run(rows, c.Classic)
	}
	if e != nil {
		return Result{}, e
	}
	r.Points = p
	if c.SchemaVersion >= 4 && c.Risk.Enabled {
		r.RiskATR, e = backtest.RiskATR(rows, c.Risk.ATRPeriod)
		if e != nil {
			return Result{}, e
		}
	}
	for i, v := range p {
		t := rows[i].CloseTime + 1
		add := func(kind string, d int, allowed bool, reason string) {
			r.Events = append(r.Events, Event{kind, d, i, t, t, "", allowed, reason})
		}
		for _, entry := range []struct {
			on bool
			d  int
		}{{v.StartLong, 1}, {v.StartShort, -1}} {
			if !entry.on {
				continue
			}
			allowed := true
			reason := ""
			if c.Daily.Enabled && (!r.Daily[i].Ready || r.Daily[i].Direction != entry.d) {
				allowed = false
				reason = "daily_direction"
			}
			if c.SchemaVersion >= 4 && c.Risk.Enabled && (!finite(r.RiskATR[i]) || r.RiskATR[i] <= 0) {
				allowed = false
				reason = "risk_atr_unavailable"
			}
			add("main_entry", entry.d, allowed, reason)
			if !allowed {
				if entry.d == 1 {
					r.Points[i].StartLong = false
				} else {
					r.Points[i].StartShort = false
				}
			}
		}
		if v.EndLong {
			add("exit", 1, true, "global_signal")
		}
		if v.EndShort {
			add("exit", -1, true, "global_signal")
		}
		if c.EnvelopeEnabled && r.Envelope[i].Ready {
			ep := r.Envelope[i]
			kind := ""
			d := 0
			if rows[i].Close > ep.FarUpper {
				kind = "strong_deviation"
				d = -1
			} else if rows[i].Close < ep.FarLower {
				kind = "strong_deviation"
				d = 1
			} else if rows[i].Close > ep.NearUpper {
				kind = "deviation"
				d = -1
			} else if rows[i].Close < ep.NearLower {
				kind = "deviation"
				d = 1
			}
			if kind != "" {
				add(kind, d, false, "context_only")
			}
		}
	}
	if c.Pullback.Enabled {
		machine := NewPullbackMachine(c.Pullback)
		for i, p := range r.Points {
			var d market.DailyContext
			if c.Daily.Enabled {
				d = r.Daily[i]
			}
			env := r.Envelope[i]
			if c.SchemaVersion >= 3 {
				env.Ready = env.Ready && r.FeatureReady[i]
			}
			if c.SchemaVersion >= 4 && c.Risk.Enabled {
				env.Ready = env.Ready && finite(r.RiskATR[i]) && r.RiskATR[i] > 0
			}
			r.Events = append(r.Events, machine.Step(i, rows[i].Close, p, env, d, c.Daily.Enabled, rows[i].CloseTime+1)...)
		}
	}
	SelectEntries(&r, rows, c)
	return r, nil
}
func Counts(events []Event) map[string]int {
	out := map[string]int{}
	for _, e := range events {
		out[e.Kind]++
		if !e.Allowed {
			out[e.Kind+"_filtered"]++
		}
	}
	return out
}

// EntryMetadata connects selected market events to the single cash ledger.
// Main entries have priority over same-direction pullbacks; a directional
// conflict suppresses entries while preserving exits.
func SelectEntries(r *Result, rows []market.Candle, c Config) {
	if c.SchemaVersion < 2 {
		return
	}
	selected := map[int][]Event{}
	for _, e := range r.Events {
		if !e.Allowed {
			continue
		}
		if e.Kind == "main_entry" && c.EntryMode != "pullback" || e.Kind == "pullback" && c.EntryMode != "main" {
			selected[e.Bar] = append(selected[e.Bar], e)
		}
	}
	for i := range r.Points {
		p := &r.Points[i]
		p.StartLong, p.StartShort = false, false
		events := selected[i]
		direction := 0
		conflict := false
		for _, e := range events {
			if direction != 0 && direction != e.Direction {
				conflict = true
			}
			direction = e.Direction
		}
		if conflict {
			r.Events = append(r.Events, Event{"entry_conflict", 0, i, rows[i].CloseTime + 1, rows[i].CloseTime + 1, "", false, "opposite_entries"})
			continue
		}
		p.StartLong = direction == 1
		p.StartShort = direction == -1
	}
}
func EntryMetadata(r Result, c Config) []backtest.EntryInfo {
	out := make([]backtest.EntryInfo, len(r.Points))
	for _, e := range r.Events {
		if (e.Kind == "main_entry" && c.EntryMode == "pullback") || (e.Kind == "pullback" && c.EntryMode == "main") {
			continue
		}
		if !e.Allowed || (e.Kind != "main_entry" && e.Kind != "pullback") {
			continue
		}
		p := r.Points[e.Bar]
		if !(p.StartLong && e.Direction == 1 || p.StartShort && e.Direction == -1) {
			continue
		}
		if out[e.Bar].Kind == "main_entry" {
			continue
		}
		out[e.Bar] = backtest.EntryInfo{Kind: e.Kind, Direction: e.Direction, DecisionTime: e.DecisionTime, AvailableTime: e.AvailableTime}
	}
	return out
}

func ReasonCounts(events []Event) map[string]int {
	out := map[string]int{}
	for _, e := range events {
		if e.Reason != "" {
			out[e.Kind+":"+e.Reason]++
		}
	}
	return out
}

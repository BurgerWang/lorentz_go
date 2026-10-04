package strategy

import (
	"lorentzgo/indicator"
	"lorentzgo/market"
	"reflect"
	"testing"
)

func pbEnv(center float64) indicator.EnvelopePoint {
	return indicator.EnvelopePoint{Ready: true, Center: center, NearLower: center - 2, NearUpper: center + 2, FarLower: center - 10, FarUpper: center + 10}
}
func pbMain(bar, direction int, entry bool) indicator.Point {
	return indicator.Point{Time: int64(bar+1)*1000 - 1, Signal: direction, StartLong: entry && direction == 1, StartShort: entry && direction == -1}
}
func pbMachine(wait int) *PullbackMachine {
	return NewPullbackMachine(PullbackConfig{Enabled: true, MaxWaitBars: wait})
}
func pbStep(m *PullbackMachine, bar, d int, entry bool, close, center float64) []Event {
	return m.Step(bar, close, pbMain(bar, d, entry), pbEnv(center), market.DailyContext{}, false, int64(bar+1)*1000)
}
func pbKind(events []Event, kind string) bool {
	for _, e := range events {
		if e.Kind == kind {
			return true
		}
	}
	return false
}

func TestPullbackBothSidesAndConsumption(t *testing.T) {
	for _, d := range []int{1, -1} {
		m := pbMachine(24)
		start := pbStep(m, 0, d, true, 100+float64(d), 100)
		if len(start) != 1 || start[0].Kind != "phase_started" || m.state != "armed" {
			t.Fatalf("start %d %+v", d, start)
		}
		phase := start[0].Phase
		if events := pbStep(m, 1, d, false, 100-float64(d), 100); len(events) != 0 || m.state != "retraced" {
			t.Fatalf("retrace %d %+v", d, events)
		}
		recovery := pbStep(m, 2, d, false, 100+2*float64(d), 100+float64(d))
		if len(recovery) != 1 || recovery[0].Kind != "pullback" || !recovery[0].Allowed || recovery[0].Phase != phase || recovery[0].DecisionTime != 3000 {
			t.Fatalf("recovery %d %+v", d, recovery)
		}
		// No execution acknowledgement is needed to consume the phase.
		pbStep(m, 3, d, false, 100, 101)
		if events := pbStep(m, 4, d, false, 105, 102); pbKind(events, "pullback") || m.state != "consumed" {
			t.Fatalf("reissued consumed event %d %+v", d, events)
		}
	}
}

func TestPullbackReplacementAndAllStateGuards(t *testing.T) {
	setup := func(state string) *PullbackMachine {
		m := pbMachine(24)
		pbStep(m, 0, 1, true, 101, 100)
		if state != "armed" {
			pbStep(m, 1, 1, false, 99, 100)
		}
		if state == "consumed" {
			pbStep(m, 2, 1, false, 103, 101)
		}
		return m
	}
	for _, state := range []string{"armed", "retraced", "consumed"} {
		t.Run(state+"_replacement", func(t *testing.T) {
			m := setup(state)
			old := m.phase
			bar := m.previousBar + 1
			e := pbStep(m, bar, 1, true, 105, 102)
			if len(e) != 2 || e[0].Reason != "replaced" || e[1].Kind != "phase_started" || m.phase == old || m.state != "armed" {
				t.Fatalf("replacement %+v", e)
			}
		})
		for _, reason := range []string{"data_unavailable", "main_direction", "daily_direction", "far_boundary", "data_gap"} {
			t.Run(state+"_"+reason, func(t *testing.T) {
				m := setup(state)
				bar := m.previousBar + 1
				env := pbEnv(102)
				main := pbMain(bar, 1, false)
				daily := market.DailyContext{Ready: true, Direction: 1}
				dailyEnabled := false
				close := 105.0
				switch reason {
				case "data_unavailable":
					env.Ready = false
				case "main_direction":
					main.Signal = 0
				case "daily_direction":
					dailyEnabled = true
					daily.Direction = 0
				case "far_boundary":
					close = 91
				case "data_gap":
					bar++
				}
				e := m.Step(bar, close, main, env, daily, dailyEnabled, int64(bar+1)*1000)
				if len(e) != 1 || e[0].Kind != "phase_invalidated" || e[0].Reason != reason || m.state != "idle" {
					t.Fatalf("guard %+v state %s", e, m.state)
				}
			})
		}
	}
}

func TestPullbackTimeoutDoesNotResetOnRetracement(t *testing.T) {
	m := pbMachine(2)
	pbStep(m, 0, 1, true, 101, 100)
	pbStep(m, 1, 1, false, 102, 100)
	pbStep(m, 2, 1, false, 99, 100)
	if m.state != "retraced" {
		t.Fatal("expected retraced")
	}
	e := pbStep(m, 3, 1, false, 103, 101)
	if len(e) != 1 || e[0].Reason != "timeout" || pbKind(e, "pullback") {
		t.Fatalf("timeout %+v", e)
	}
	// Expiry cannot reactivate a remembered entry.
	e = m.Step(4, 104, pbMain(0, 1, true), pbEnv(102), market.DailyContext{}, false, 5000)
	if len(e) != 1 || e[0].Kind != "duplicate_suppressed" || m.state != "idle" {
		t.Fatalf("duplicate %+v", e)
	}
}

func TestPullbackIdempotenceNewStageAfterInvalidationAndNoSameBarRecovery(t *testing.T) {
	m := pbMachine(24)
	start := pbStep(m, 0, 1, true, 101, 100)
	if e := pbStep(m, 0, 1, true, 101, 100); len(e) != 0 || m.sequence != 1 {
		t.Fatal("same bar must be idempotent")
	}
	e := pbStep(m, 1, -1, true, 99, 100)
	if len(e) != 2 || e[0].Reason != "main_direction" || e[1].Kind != "phase_started" || e[1].Phase == start[0].Phase {
		t.Fatalf("opposite replacement %+v", e)
	}
	if m.state != "armed" {
		t.Fatal("new stage must only arm")
	}
	// A retracement row crossing its old center cannot also recover.
	pbStep(m, 2, -1, false, 100, 99)
	if m.state != "retraced" {
		t.Fatal("short retracement should confirm")
	}
	if e := pbStep(m, 2, -1, false, 98, 99); len(e) != 0 || m.state != "retraced" {
		t.Fatal("same bar recovery rejected")
	}
}

func TestPullbackQualificationAndDisabled(t *testing.T) {
	for _, d := range []int{1, -1} {
		for _, ready := range []bool{true, false} {
			m := pbMachine(24)
			env := pbEnv(100)
			env.Ready = ready
			e := m.Step(0, 100, pbMain(0, d, true), env, market.DailyContext{Ready: true, Direction: d}, true, 1000)
			if len(e) != 0 {
				t.Fatal("center equality cannot establish phase")
			}
		}
	}
	m := NewPullbackMachine(DefaultPullbackConfig())
	if e := pbStep(m, 0, 1, true, 101, 100); len(e) != 0 || m.hasPrevious {
		t.Fatal("disabled machine must have no effects")
	}
	if (PullbackConfig{MaxWaitBars: 0}).Validate() == nil {
		t.Fatal("accepted invalid wait")
	}
}

func TestPullbackPrefixStability(t *testing.T) {
	run := func(n int) [][]Event {
		m := pbMachine(24)
		out := make([][]Event, n)
		for i := 0; i < n; i++ {
			entry := i%5 == 0
			center := 100 + float64(i)
			close := center + 2
			if i%5 == 1 {
				close = center - 1
			}
			out[i] = pbStep(m, i, 1, entry, close, center)
		}
		return out
	}
	short, long := run(8), run(20)
	if !reflect.DeepEqual(short, long[:8]) {
		t.Fatal("future append changed prefix")
	}
}

func TestPullbackRequiresConfirmedZoneAndRecoverySlope(t *testing.T) {
	m := pbMachine(24)
	pbStep(m, 0, 1, true, 101, 100)
	// An intrabar touch is not represented by the finalized close; a shallow
	// close above center and a close below the near zone do not confirm.
	pbStep(m, 1, 1, false, 100.5, 100)
	pbStep(m, 2, 1, false, 97, 100)
	if m.state != "armed" {
		t.Fatal("unconfirmed closes armed retracement")
	}
	pbStep(m, 3, 1, false, 99, 100)
	if e := pbStep(m, 4, 1, false, 101, 100); pbKind(e, "pullback") {
		t.Fatal("flat kernel slope cannot recover")
	}
	// A rising center without an adjacent crossing also cannot recover.
	if e := pbStep(m, 5, 1, false, 103, 101); pbKind(e, "pullback") {
		t.Fatal("recovery requires an adjacent close crossing")
	}
	for _, daily := range []market.DailyContext{{Ready: false, Direction: 1}, {Ready: true, Direction: -1}, {Ready: true, Direction: 0}} {
		m := pbMachine(24)
		pbStep(m, 0, 1, true, 101, 100)
		e := m.Step(1, 99, pbMain(1, 1, false), pbEnv(100), daily, true, 2000)
		if len(e) != 1 || e[0].Reason != "daily_direction" {
			t.Fatalf("daily guard %+v", e)
		}
	}
}

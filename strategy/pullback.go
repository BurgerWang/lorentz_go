package strategy

import (
	"fmt"
	"lorentzgo/indicator"
	"lorentzgo/market"
)

type PullbackConfig struct {
	Enabled     bool `json:"enabled"`
	MaxWaitBars int  `json:"max_wait_bars"`
}

func DefaultPullbackConfig() PullbackConfig { return PullbackConfig{MaxWaitBars: 24} }
func (c PullbackConfig) Validate() error {
	if c.MaxWaitBars < 1 {
		return fmt.Errorf("pullback max_wait_bars must be positive")
	}
	return nil
}

type pullbackEntry struct {
	time      int64
	direction int
}

// PullbackMachine tracks market phases, not orders or positions. A consumed
// signal stays consumed even when the execution layer rejects its entry.
type PullbackMachine struct {
	cfg            PullbackConfig
	state          string
	direction      int
	phase          string
	stageBar       int
	sequence       uint64
	seen           map[pullbackEntry]bool
	hasPrevious    bool
	previousBar    int
	previousTime   int64
	previousClose  float64
	previousCenter float64
	previousReady  bool
}

func NewPullbackMachine(cfg PullbackConfig) *PullbackMachine {
	return &PullbackMachine{cfg: cfg, state: "idle", seen: make(map[pullbackEntry]bool)}
}

// Step consumes adjacent, finalized rows in order. Repeated or older rows do
// not mutate state. Callers validate the config and supply daily-filtered main
// entries; this machine also guards active phases against daily invalidation.
func (m *PullbackMachine) Step(bar int, close float64, main indicator.Point, env indicator.EnvelopePoint, daily market.DailyContext, dailyEnabled bool, decisionTime int64) []Event {
	if !m.cfg.Enabled || m.cfg.Validate() != nil {
		return nil
	}
	if m.hasPrevious && (bar <= m.previousBar || decisionTime <= m.previousTime) {
		return nil
	}
	var events []Event
	emit := func(kind string, allowed bool, reason string) {
		events = append(events, Event{Kind: kind, Direction: m.direction, Bar: bar, DecisionTime: decisionTime, AvailableTime: decisionTime, Phase: m.phase, Allowed: allowed, Reason: reason})
	}
	ready := env.Ready && finite(close) && finite(env.Center) && finite(env.NearLower) && finite(env.NearUpper) && finite(env.FarLower) && finite(env.FarUpper)
	adjacent := m.hasPrevious && bar == m.previousBar+1
	defer func() {
		m.hasPrevious = true
		m.previousBar = bar
		m.previousTime = decisionTime
		m.previousClose = close
		m.previousCenter = env.Center
		m.previousReady = ready
	}()
	if m.state != "idle" {
		reason := ""
		switch {
		case !adjacent:
			reason = "data_gap"
		case !ready:
			reason = "data_unavailable"
		case main.Signal != m.direction:
			reason = "main_direction"
		case dailyEnabled && (!daily.Ready || daily.Direction != m.direction):
			reason = "daily_direction"
		case (m.direction == 1 && close < env.FarLower) || (m.direction == -1 && close > env.FarUpper):
			reason = "far_boundary"
		case m.state != "consumed" && bar-m.stageBar > m.cfg.MaxWaitBars:
			reason = "timeout"
		}
		if reason != "" {
			emit("phase_invalidated", false, reason)
			m.state = "idle"
			m.direction = 0
			m.phase = ""
		}
	}
	entryDir := 0
	if main.StartLong && !main.StartShort && main.Signal == 1 {
		entryDir = 1
	}
	if main.StartShort && !main.StartLong && main.Signal == -1 {
		entryDir = -1
	}
	qualified := entryDir != 0 && ready && ((entryDir == 1 && close > env.Center) || (entryDir == -1 && close < env.Center)) && (!dailyEnabled || (daily.Ready && daily.Direction == entryDir))
	if qualified {
		key := pullbackEntry{time: main.Time + 1, direction: entryDir}
		if m.seen[key] {
			// The same source event cannot arm a second phase after timeout or
			// consumption. Keep its source direction in the diagnostic.
			events = append(events, Event{Kind: "duplicate_suppressed", Direction: entryDir, Bar: bar, DecisionTime: decisionTime, AvailableTime: decisionTime, Phase: m.phase, Allowed: false, Reason: "duplicate_main"})
		} else {
			if m.state != "idle" {
				emit("phase_invalidated", false, "replaced")
			}
			m.seen[key] = true
			m.sequence++
			m.direction = entryDir
			m.stageBar = bar
			m.state = "armed"
			m.phase = fmt.Sprintf("%d:%d:%d", key.time, key.direction, m.sequence)
			emit("phase_started", false, "main_entry")
			return events
		}
	}
	if m.state == "armed" && bar > m.stageBar && ready {
		if (m.direction == 1 && close >= env.NearLower && close <= env.Center) || (m.direction == -1 && close <= env.NearUpper && close >= env.Center) {
			m.state = "retraced"
		}
	} else if m.state == "retraced" && adjacent && m.previousReady && ready {
		recovered := (m.direction == 1 && m.previousClose <= m.previousCenter && close > env.Center && env.Center > m.previousCenter) || (m.direction == -1 && m.previousClose >= m.previousCenter && close < env.Center && env.Center < m.previousCenter)
		if recovered {
			emit("pullback", true, "center_recovery")
			m.state = "consumed"
		}
	}
	return events
}

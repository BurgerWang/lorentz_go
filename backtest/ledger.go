// Package backtest evaluates next-open signals with a fixed-quantity, 1x cash ledger.
// It is not a leverage, liquidation, order-book, or exchange margin simulator.
package backtest

import (
	"fmt"
	"lorentzgo/indicator"
	"lorentzgo/market"
	"math"
)

type Config struct {
	InitialEquity    float64 `json:"initial_equity"`
	PositionFraction float64 `json:"position_fraction"`
	FeeBPS           float64 `json:"fee_bps"`
	SlippageBPS      float64 `json:"slippage_bps"`
	StartTime        int64   `json:"start_time"`
	EndTime          int64   `json:"end_time"`
	ExitPolicy       string  `json:"exit_policy"`
	MaxHoldBars      int     `json:"max_hold_bars"`
}

func DefaultConfig() Config {
	return Config{InitialEquity: 10000, PositionFraction: 1, FeeBPS: 5, SlippageBPS: 2, ExitPolicy: "signals", MaxHoldBars: 4}
}

type Metrics struct {
	Trades         int     `json:"trades"`
	Wins           int     `json:"wins"`
	Losses         int     `json:"losses"`
	Breakeven      int     `json:"breakeven"`
	WinRatePct     float64 `json:"win_rate_pct"`
	NetReturnPct   float64 `json:"net_return_pct"`
	MaxDrawdownPct float64 `json:"max_drawdown_pct"`
	ExpectancyPct  float64 `json:"expectancy_pct"`
	GrossPnL       float64 `json:"gross_pnl"`
	Fees           float64 `json:"fees"`
	// Funding is signed cash received: positive receipts, negative payments.
	Funding      float64  `json:"funding"`
	NetPnL       float64  `json:"net_pnl"`
	ProfitFactor *float64 `json:"profit_factor"`
}
type Trade struct {
	RiskLine            float64 `json:"risk_line,omitempty"`
	InitialRiskDistance float64 `json:"initial_risk_distance,omitempty"`
	EntryKind           string  `json:"entry_kind,omitempty"`
	EntryDecisionTime   int64   `json:"entry_decision_time,omitempty"`
	EntryAvailableTime  int64   `json:"entry_available_time,omitempty"`
	EntryTime           int64   `json:"entry_time"`
	ExitTime            int64   `json:"exit_time"`
	Side                string  `json:"side"`
	Quantity            float64 `json:"quantity"`
	EntryPrice          float64 `json:"entry_price"`
	ExitPrice           float64 `json:"exit_price"`
	GrossPnL            float64 `json:"gross_pnl"`
	Fees                float64 `json:"fees"`
	Funding             float64 `json:"funding"`
	NetPnL              float64 `json:"net_pnl"`
	// ReturnPct uses equity immediately before entry (not notional) as denominator.
	ReturnPct float64 `json:"return_pct"`
	Reason    string  `json:"reason"`
}
type EquityPoint struct {
	Time   int64   `json:"time"`
	Equity float64 `json:"equity"`
}
type Report struct {
	Config      Config        `json:"config"`
	Metrics     Metrics       `json:"metrics"`
	Trades      []Trade       `json:"trades"`
	Equity      []EquityPoint `json:"equity"`
	Assumptions []string      `json:"assumptions"`
}

func finite(v float64) bool { return !math.IsNaN(v) && !math.IsInf(v, 0) }
func (c Config) Validate() error {
	if !finite(c.InitialEquity) || c.InitialEquity <= 0 || !finite(c.PositionFraction) || c.PositionFraction < 0 || c.PositionFraction > 1 || !finite(c.FeeBPS) || c.FeeBPS < 0 || c.FeeBPS >= 10000 || !finite(c.SlippageBPS) || c.SlippageBPS < 0 || c.SlippageBPS >= 10000 {
		return fmt.Errorf("invalid equity, position fraction, fee, or slippage")
	}
	if c.StartTime < 0 || c.EndTime < 0 || (c.EndTime != 0 && c.EndTime <= c.StartTime) {
		return fmt.Errorf("invalid scoring window")
	}
	if c.ExitPolicy != "signals" && c.ExitPolicy != "four-bars" {
		return fmt.Errorf("invalid exit policy %q", c.ExitPolicy)
	}
	if c.MaxHoldBars < 0 {
		return fmt.Errorf("negative max hold bars")
	}
	return nil
}

// Evaluate uses only fully closed candles inside [StartTime, EndTime); zero
// EndTime means unbounded. Signals outside the window cannot create a trade.
// Funding rows must be strictly ordered and include every settlement in the
// evaluated market; missing settlements cannot be inferred from OHLC data.
type EntryInfo struct {
	Kind                        string
	Direction                   int
	DecisionTime, AvailableTime int64
}

func Evaluate(candles []market.Candle, signals []indicator.Point, funding []market.Funding, cfg Config) (Report, error) {
	return evaluate(candles, signals, funding, cfg, nil, RiskConfig{}, nil, EvaluationOptions{})
}
func EvaluateWithEntries(candles []market.Candle, signals []indicator.Point, funding []market.Funding, cfg Config, entries []EntryInfo) (Report, error) {
	if len(entries) != len(candles) {
		return Report{}, fmt.Errorf("entry metadata length mismatch")
	}
	return evaluate(candles, signals, funding, cfg, entries, RiskConfig{}, nil, EvaluationOptions{})
}
func evaluate(candles []market.Candle, signals []indicator.Point, funding []market.Funding, cfg Config, entries []EntryInfo, risk RiskConfig, atrAtDecision []float64, options EvaluationOptions) (Report, error) {
	if cfg.ExitPolicy == "four-bars" && cfg.MaxHoldBars == 0 {
		cfg.MaxHoldBars = 4
	}
	r := Report{Config: cfg, Trades: []Trade{}, Equity: []EquityPoint{}, Assumptions: []string{
		"Fees and adverse slippage are assumptions per side, not verified account fees; fixed quantity, at most 1x entry exposure, no pyramiding.",
		"Max drawdown samples execution boundaries and bar closes; OHLC cannot establish intrabar path or exact intrabar drawdown. Nonpositive equity at an adverse OHLC extreme is an explicit error, not simulated liquidation.",
		"Funding is signed cash received; all supplied settlements in (entry time, exit time] apply using their own mark price. Funding coverage must be checked by the caller.",
	}}
	if risk.Enabled {
		r.Assumptions = append(r.Assumptions, "ATR risk lines are close-confirmed and execute at the next actual open with adverse slippage. They do not protect intrabar prices or guarantee net breakeven; future funding and gaps can cause losses.")
	}
	fail := func(err error) (Report, error) { return Report{}, err }
	if err := cfg.Validate(); err != nil {
		return fail(err)
	}
	if len(candles) != len(signals) {
		return fail(fmt.Errorf("candle/signal length mismatch"))
	}
	for i, c := range candles {
		if c.OpenTime < 0 || c.CloseTime < c.OpenTime || (i > 0 && c.OpenTime <= candles[i-1].CloseTime) || !finite(c.Open) || !finite(c.High) || !finite(c.Low) || !finite(c.Close) || !finite(c.Volume) || c.Low <= 0 || c.High < c.Low || c.Open < c.Low || c.Open > c.High || c.Close < c.Low || c.Close > c.High || c.Volume < 0 {
			return fail(fmt.Errorf("invalid candle %d", i))
		}
		if signals[i].Time != 0 && signals[i].Time != c.CloseTime {
			return fail(fmt.Errorf("signal timestamp mismatch at %d", i))
		}
		if entries != nil && (signals[i].StartLong || signals[i].StartShort) {
			info := entries[i]
			want := 1
			if signals[i].StartShort {
				want = -1
			}
			if (info.Kind != "main_entry" && info.Kind != "pullback") || info.Direction != want || info.DecisionTime != c.CloseTime+1 || info.AvailableTime > info.DecisionTime || info.AvailableTime < 0 || (i+1 < len(candles) && info.DecisionTime > candles[i+1].OpenTime) {
				return fail(fmt.Errorf("invalid entry timing/type at %d", i))
			}
		}
		if signals[i].StartLong && signals[i].StartShort {
			return fail(fmt.Errorf("conflicting entry signals at %d", i))
		}
	}
	if err := market.ValidateFunding(funding); err != nil {
		return fail(err)
	}
	for _, f := range funding {
		if f.MarkPriceSource == market.FundingMarkKlineOpen8h {
			r.Assumptions = append(r.Assumptions, "Some funding amounts are estimates: historical actual funding rates use official Binance eight-hour mark-price kline opening prices as settlement mark proxies, not exact settlement amounts.")
			break
		}
	}
	inRange := func(t int64) bool { return t >= cfg.StartTime && (cfg.EndTime == 0 || t < cfg.EndTime) }
	last := -1
	for i, c := range candles {
		if inRange(c.CloseTime) {
			last = i
		}
	}
	cash, peak := cfg.InitialEquity, cfg.InitialEquity
	var pos *Trade
	direction, entryBar := 0, 0
	entryEquity := 0.0
	pendingRisk, breakevenActive := false, false
	fi := 0
	equity := func(price float64) float64 {
		if pos == nil {
			return cash
		}
		return cash + float64(direction)*pos.Quantity*(price-pos.EntryPrice)
	}
	check := func(v float64) error {
		if !finite(v) || v <= 0 {
			return EquityProtectionError{"nonfinite or nonpositive equity: bankruptcy/liquidation is not modeled"}
		}
		return nil
	}
	mark := func(v float64) error {
		if err := check(v); err != nil {
			return err
		}
		if v > peak {
			peak = v
		}
		r.Metrics.MaxDrawdownPct = math.Max(r.Metrics.MaxDrawdownPct, 100*(peak-v)/peak)
		return nil
	}
	sequence := 0
	emit := func(t int64, phase string, price float64, sampled bool, fee, gross, fundingCash float64, f *market.Funding, trade *Trade) error {
		if options.Trace == nil {
			return nil
		}
		sequence++
		event := TraceEvent{Version: "ledger-trace-v1", Sequence: sequence, Time: t, Phase: phase, Sampled: sampled, Cash: cash, Equity: equity(price), Price: price, Direction: direction, TradeID: len(r.Trades) + 1, Fee: fee, GrossPnL: gross, FundingCash: fundingCash, Funding: f, Trade: trade}
		if pos != nil {
			event.Quantity = pos.Quantity
			event.EntryPrice = pos.EntryPrice
		}
		if trade != nil {
			event.TradeID = len(r.Trades)
		}
		return options.Trace(event)
	}
	sample := func(t int64, phase string, price float64, fee, gross float64, trade *Trade) error {
		if err := mark(equity(price)); err != nil {
			return err
		}
		return emit(t, phase, price, true, fee, gross, 0, nil, trade)
	}
	if err := emit(cfg.StartTime, "initial", 0, true, 0, 0, 0, nil, nil); err != nil {
		return fail(err)
	}
	settle := func(t int64, adverse float64) error {
		for fi < len(funding) && funding[fi].Time <= t {
			f := funding[fi]
			fi++
			if pos == nil || f.Time <= pos.EntryTime {
				continue
			}
			markPrice := f.MarkPrice
			if options.FundingMark != nil {
				var err error
				markPrice, err = options.FundingMark(f, direction, pos.Quantity)
				if err != nil {
					return err
				}
				if !finite(markPrice) || markPrice <= 0 {
					return fmt.Errorf("invalid scenario funding mark")
				}
			}
			payment := -float64(direction) * pos.Quantity * markPrice * f.Rate
			cash += payment
			pos.Funding += payment
			if err := check(equity(markPrice)); err != nil {
				return err
			}
			if err := emit(f.Time, "funding", markPrice, false, 0, 0, payment, &f, nil); err != nil {
				return err
			}
			if err := check(equity(adverse)); err != nil {
				return fmt.Errorf("conservative post-funding OHLC solvency check: %w", err)
			}
		}
		return nil
	}
	closePosition := func(t int64, price float64, reason string) error {
		if pos == nil {
			return nil
		}
		p := pos
		p.ExitTime = t
		p.ExitPrice = price * (1 - float64(direction)*cfg.SlippageBPS/10000)
		p.Reason = reason
		p.GrossPnL = float64(direction) * p.Quantity * (p.ExitPrice - p.EntryPrice)
		fee := p.Quantity * p.ExitPrice * cfg.FeeBPS / 10000
		p.Fees += fee
		cash += p.GrossPnL - fee
		p.NetPnL = p.GrossPnL - p.Fees + p.Funding
		p.ReturnPct = 100 * p.NetPnL / entryEquity
		if !finite(p.NetPnL) || !finite(p.ReturnPct) {
			return fmt.Errorf("nonfinite trade ledger")
		}
		pos = nil
		direction = 0
		pendingRisk, breakevenActive = false, false
		r.Trades = append(r.Trades, *p)
		return sample(t, "exit", price, fee, p.GrossPnL, p)
	}
	for i, c := range candles {
		if !inRange(c.CloseTime) {
			continue
		}
		if err := settle(c.OpenTime, c.Open); err != nil {
			return fail(err)
		}
		if err := sample(c.OpenTime, "open", c.Open, 0, 0, nil); err != nil {
			return fail(err)
		}
		var s indicator.Point
		if i > 0 && inRange(candles[i-1].CloseTime) && inRange(c.OpenTime) {
			s = signals[i-1]
		}
		if pos != nil {
			reason := ""
			if (direction == 1 && s.EndLong) || (direction == -1 && s.EndShort) {
				reason = "signal_exit"
			} else if (direction == 1 && s.StartShort) || (direction == -1 && s.StartLong) {
				reason = "reverse"
			} else if risk.Enabled && pendingRisk {
				reason = "risk_exit"
			} else if cfg.ExitPolicy == "four-bars" && i-entryBar >= cfg.MaxHoldBars {
				reason = "max_hold_bars"
			}
			if reason != "" {
				if err := closePosition(c.OpenTime, c.Open, reason); err != nil {
					return fail(err)
				}
			}
		}
		if pos == nil && cfg.PositionFraction > 0 && (s.StartLong || s.StartShort) {
			direction = 1
			side := "long"
			if s.StartShort {
				direction = -1
				side = "short"
			}
			entryEquity = cash
			price := c.Open * (1 + float64(direction)*cfg.SlippageBPS/10000)
			riskDistance := 0.0
			if risk.Enabled {
				if i == 0 || !finite(atrAtDecision[i-1]) || atrAtDecision[i-1] <= 0 {
					return fail(fmt.Errorf("entry at %d lacks positive decision-time ATR", i))
				}
				riskDistance = risk.ATRMultiplier * atrAtDecision[i-1]
				line := price - float64(direction)*riskDistance
				if !finite(riskDistance) || riskDistance <= 0 || !finite(line) || line <= 0 {
					return fail(fmt.Errorf("invalid initial risk line at %d", i))
				}
			}
			qty := cash * cfg.PositionFraction / price
			fee := qty * price * cfg.FeeBPS / 10000
			if !finite(qty) || qty <= 0 {
				return fail(fmt.Errorf("nonfinite or nonpositive position quantity"))
			}
			pos = &Trade{EntryTime: c.OpenTime, Side: side, Quantity: qty, EntryPrice: price, Fees: fee}
			if risk.Enabled {
				pos.InitialRiskDistance = riskDistance
				pos.RiskLine = price - float64(direction)*riskDistance
			}
			if entries != nil && i > 0 {
				info := entries[i-1]
				pos.EntryKind = info.Kind
				pos.EntryDecisionTime = info.DecisionTime
				pos.EntryAvailableTime = info.AvailableTime
			}
			entryBar = i
			cash -= fee
			if err := sample(c.OpenTime, "entry", c.Open, fee, 0, nil); err != nil {
				return fail(err)
			}
		}
		// Check the adverse OHLC extreme with current cash, without claiming path accuracy.
		adverse := c.Close
		if pos != nil {
			adverse = c.Low
			if direction == -1 {
				adverse = c.High
			}
			if err := check(equity(adverse)); err != nil {
				return fail(err)
			}
		}
		if err := settle(c.CloseTime, adverse); err != nil {
			return fail(err)
		}
		if risk.Enabled && pos != nil {
			tighten := func(line float64) error {
				if !finite(line) || line <= 0 {
					return fmt.Errorf("invalid tightened risk line at %d", i)
				}
				if (direction == 1 && line > pos.RiskLine) || (direction == -1 && line < pos.RiskLine) {
					pos.RiskLine = line
				}
				return nil
			}
			if risk.Trail && finite(atrAtDecision[i]) && atrAtDecision[i] > 0 {
				line := c.Close - float64(direction)*risk.TrailMultiplier*atrAtDecision[i]
				// A nonpositive long trailing candidate cannot tighten the valid
				// positive initial line, so it is safely ignored.
				if line > 0 {
					if err := tighten(line); err != nil {
						return fail(err)
					}
				}
			}
			if risk.BreakevenR > 0 && float64(direction)*(c.Close-pos.EntryPrice)/pos.InitialRiskDistance >= risk.BreakevenR {
				breakevenActive = true
			}
			if breakevenActive {
				line, err := netBreakevenLine(pos, direction, cfg)
				if err != nil {
					return fail(err)
				}
				if err := tighten(line); err != nil {
					return fail(err)
				}
			}
			pendingRisk = (direction == 1 && c.Close <= pos.RiskLine) || (direction == -1 && c.Close >= pos.RiskLine)
		}
		if i == last {
			if err := closePosition(c.CloseTime, c.Close, "end_of_window"); err != nil {
				return fail(err)
			}
		}
		eq := equity(c.Close)
		if err := sample(c.CloseTime, "close", c.Close, 0, 0, nil); err != nil {
			return fail(err)
		}
		r.Equity = append(r.Equity, EquityPoint{c.CloseTime, eq})
	}
	m := &r.Metrics
	m.Trades = len(r.Trades)
	profits, losses := 0.0, 0.0
	for _, t := range r.Trades {
		m.GrossPnL += t.GrossPnL
		m.Fees += t.Fees
		m.Funding += t.Funding
		m.NetPnL += t.NetPnL
		m.ExpectancyPct += t.ReturnPct
		if t.NetPnL > 0 {
			m.Wins++
			profits += t.NetPnL
		} else if t.NetPnL < 0 {
			m.Losses++
			losses -= t.NetPnL
		} else {
			m.Breakeven++
		}
	}
	if m.Trades > 0 {
		m.WinRatePct = 100 * float64(m.Wins) / float64(m.Trades)
		m.ExpectancyPct /= float64(m.Trades)
	}
	m.NetReturnPct = 100 * (cash - cfg.InitialEquity) / cfg.InitialEquity
	if losses > 0 {
		pf := profits / losses
		m.ProfitFactor = &pf
	}
	for _, v := range []float64{m.GrossPnL, m.Fees, m.Funding, m.NetPnL, m.NetReturnPct, m.ExpectancyPct, m.MaxDrawdownPct, profits, losses} {
		if !finite(v) {
			return fail(fmt.Errorf("nonfinite aggregate metrics"))
		}
	}
	if m.ProfitFactor != nil && !finite(*m.ProfitFactor) {
		return fail(fmt.Errorf("nonfinite profit factor"))
	}
	return r, nil
}

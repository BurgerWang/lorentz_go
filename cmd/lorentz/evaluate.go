package main

import (
	"errors"
	"flag"
	"fmt"
	"path/filepath"
	"sort"
	"time"

	"lorentzgo/backtest"
	"lorentzgo/indicator"
	"lorentzgo/market"
)

type result struct {
	Interval                 string           `json:"interval"`
	Algorithm                indicator.Config `json:"indicator"`
	FullDataQuality          market.Quality   `json:"full_data_quality"`
	ChartStart, ChartEnd     int64
	ScoringStart, ScoringEnd int64
	Report                   backtest.Report `json:"report"`
}

func evaluate(rows []market.Candle, q market.Quality, funding []market.Funding, interval string, c indicator.Config, b backtest.Config) (result, error) {
	if err := fundingCoverage(funding, b.StartTime, b.EndTime); err != nil {
		return result{}, err
	}
	p, err := indicator.Run(rows, c)
	if err != nil {
		return result{}, err
	}
	r, err := backtest.Evaluate(rows, p, funding, b)
	if err != nil {
		return result{}, err
	}
	return result{interval, c, q, rows[0].OpenTime, rows[len(rows)-1].CloseTime + 1, b.StartTime, b.EndTime, r}, nil
}

// ETHUSDT's settlement spacing can shorten. This eight-hour upper bound catches
// gross missing coverage; authoritative pagination remains necessary to establish
// completeness at any shorter interval. It is checked on every cached-data run.
func fundingCoverage(rows []market.Funding, start, end int64) error {
	const tolerance = int64(8*3600000 + 36000)
	if len(rows) == 0 || start >= end {
		return errors.New("missing funding history or invalid funding range")
	}
	first := sort.Search(len(rows), func(i int) bool { return rows[i].Time >= start })
	last := sort.Search(len(rows), func(i int) bool { return rows[i].Time >= end })
	if (first < len(rows) && rows[first].Time-start > tolerance) || last == 0 || end-rows[last-1].Time > tolerance {
		return errors.New("funding history does not cover the scoring window")
	}
	for i := max(1, first); i < last; i++ {
		if rows[i].Time-rows[i-1].Time > tolerance {
			return fmt.Errorf("funding coverage gap before %s", iso(rows[i].Time))
		}
	}
	return nil
}
func runBacktest(args []string) error {
	f := flag.NewFlagSet("backtest", flag.ContinueOnError)
	m := modelOptions(f)
	fee := f.Float64("fee-bps", 5, "per-side fee assumption in basis points")
	slip := f.Float64("slippage-bps", 2, "per-side adverse slippage assumption")
	exit := f.String("exit", "signals", "signals or four-bars")
	out := f.String("out", "results/backtest.json", "output JSON with trades and equity")
	if e := f.Parse(args); e != nil {
		return e
	}
	rows, q, c, e := m.load()
	if e != nil {
		return e
	}
	funding, e := market.LoadFunding(filepath.Join(*m.dir, "funding.json"))
	if e != nil {
		return e
	}
	meta, e := verifiedDataset(*m.dir)
	if e != nil {
		return e
	}
	if len(funding) != meta.FundingRows {
		return errors.New("funding row count differs from dataset metadata")
	}
	b := backtest.DefaultConfig()
	b.FeeBPS = *fee
	b.SlippageBPS = *slip
	b.ExitPolicy = *exit
	b.StartTime = rows[len(rows)-*m.scoreBars].OpenTime
	b.EndTime = rows[len(rows)-1].CloseTime + 1
	r, e := evaluate(rows, q, funding, *m.interval, c, b)
	if e != nil {
		return e
	}
	if e = writeJSON(*out, r); e != nil {
		return e
	}
	printMetrics(*m.interval, c.Algorithm, r.Report.Metrics)
	return nil
}
func printMetrics(interval, name string, m backtest.Metrics) {
	fmt.Printf("%-4s %-24s trades=%3d win=%6.2f%% net=%8.2f%% DD=%6.2f%%\n", interval, name, m.Trades, m.WinRatePct, m.NetReturnPct, m.MaxDrawdownPct)
}

type candidate struct {
	Name  string
	Model indicator.Config
	Exit  string
}
type candidateScore struct {
	Name    string           `json:"name"`
	Config  indicator.Config `json:"indicator"`
	Exit    string           `json:"exit_policy"`
	Metrics backtest.Metrics `json:"metrics"`
}
type intervalResearch struct {
	Interval                                           string `json:"interval"`
	ValidationStart, ValidationEnd, TestStart, TestEnd int64
	Validation                                         []candidateScore `json:"validation_candidates"`
	Selected                                           string           `json:"selected"`
	BaselineTest                                       backtest.Metrics `json:"baseline_test"`
	SelectedTest                                       backtest.Metrics `json:"selected_test"`
	BothImproved                                       bool             `json:"both_improved_on_test"`
	ChartDiagnostic                                    backtest.Metrics `json:"original_chart_diagnostic"`
}
type researchReport struct {
	GeneratedAt             string             `json:"generated_at"`
	Method                  []string           `json:"method"`
	Intervals               []intervalResearch `json:"intervals"`
	MinimumValidationTrades int                `json:"minimum_validation_trades"`
}

func candidates() []candidate {
	b := indicator.DefaultConfig()
	b.Algorithm = "original-online"
	a := indicator.DefaultConfig()
	a.Algorithm = "aligned-knn"
	r := []candidate{{"original-online", b, "signals"}, {"aligned-k8", a, "signals"}}
	for _, k := range []int{16, 32} {
		c := a
		c.Neighbors = k
		r = append(r, candidate{fmt.Sprintf("aligned-k%d", k), c, "signals"})
	}
	c := a
	c.MinVoteFraction = .25
	r = append(r, candidate{"aligned-k8-vote25", c, "signals"})
	c = a
	c.UseEMAFilter = true
	r = append(r, candidate{"aligned-k8-ema200", c, "signals"})
	r = append(r, candidate{"aligned-k8-hold4", a, "four-bars"})
	return r
}

func window(all []market.Candle, start, end int64, bars, score int) ([]market.Candle, int64, error) {
	stop := sort.Search(len(all), func(i int) bool { return all[i].CloseTime >= end })
	if stop < 2 {
		return nil, 0, errors.New("empty research period")
	}
	begin := max(0, stop-bars)
	rows := all[begin:stop]
	scoreStart := rows[max(0, len(rows)-score)].OpenTime
	scoreStart = max(scoreStart, start)
	if scoreStart >= end {
		return nil, 0, errors.New("no candles in scoring period")
	}
	return rows, scoreStart, nil
}

func research(args []string) error {
	f := flag.NewFlagSet("research", flag.ContinueOnError)
	dir := f.String("dir", "data/ETHUSDT", "dataset directory")
	out := f.String("out", "results/research.json", "summary output")
	valStartS := f.String("validation-start", "2024-01-01", "UTC validation start")
	valEndS := f.String("validation-end", "2025-01-01", "UTC validation end / test start")
	testEndS := f.String("test-end", "2026-10-01", "UTC final test end")
	bars := f.Int("bars", 10000, "bounded initialization history per period")
	score := f.Int("score-bars", 2000, "last at most N bars inside each scoring period")
	minTrades := f.Int("min-trades", 30, "minimum validation trades to select an upgrade")
	fee := f.Float64("fee-bps", 5, "fee per side")
	slip := f.Float64("slippage-bps", 2, "adverse slippage per side")
	if e := f.Parse(args); e != nil {
		return e
	}
	vs, e := date(*valStartS)
	if e != nil {
		return e
	}
	ve, e := date(*valEndS)
	if e != nil {
		return e
	}
	te, e := date(*testEndS)
	if e != nil {
		return e
	}
	if vs >= ve || ve >= te || *bars < *score || *score < 2 || *minTrades < 1 {
		return errors.New("invalid research period/history/trade count")
	}
	funding, e := market.LoadFunding(filepath.Join(*dir, "funding.json"))
	if e != nil {
		return e
	}
	meta, e := verifiedDataset(*dir)
	if e != nil {
		return e
	}
	if len(funding) != meta.FundingRows {
		return errors.New("funding row count differs from dataset metadata")
	}
	r := researchReport{GeneratedAt: time.Now().UTC().Format(time.RFC3339), MinimumValidationTrades: *minTrades, Method: []string{
		"Predeclared seven configurations; choose using validation only. Final test is evaluated only for the online baseline and selected configuration. No parameter adaptation after final test.",
		fmt.Sprintf("For each timeframe/period, initialize from at most %d supplied historical candles and score at most the final %d candles inside the requested calendar period. Actual timestamps are recorded; this is a bounded snapshot experiment, not a full-history walk-forward or proof of live edge.", *bars, *score),
		"Selection: at least the minimum validation trades, positive validation net return, higher net return AND higher win rate than the baseline; among eligible candidates choose highest net return. Otherwise retain baseline.",
		"original-online reproduces sequential closed-bar source semantics from the supplied starting point; original-chart separately diagnoses a reload with a fixed final chart index. Neither has been matched against exported TradingView vectors.",
		"One position at a time, 1x initial exposure per entry, next-open executions, adverse per-side slippage, per-side fees, historical mark-price funding; final positions closed with costs. No leverage/liquidation or intrabar execution model.",
	}}
	cs := candidates()
	for _, interval := range []string{"15m", "1h", "4h", "1d"} {
		all, q, err := market.LoadCandles(filepath.Join(*dir, interval+".csv"), interval)
		if err != nil {
			return err
		}
		if meta.Candles[interval] != q {
			return errors.New("candle coverage differs from dataset metadata")
		}
		if q.Start > vs || q.End < te {
			return fmt.Errorf("%s dataset does not cover research dates", interval)
		}
		vr, vstart, err := window(all, vs, ve, *bars, *score)
		if err != nil {
			return err
		}
		tr, tstart, err := window(all, ve, te, *bars, *score)
		if err != nil {
			return err
		}
		b := backtest.DefaultConfig()
		b.FeeBPS = *fee
		b.SlippageBPS = *slip
		b.StartTime = vstart
		b.EndTime = ve
		ir := intervalResearch{Interval: interval, ValidationStart: vstart, ValidationEnd: ve, TestStart: tstart, TestEnd: te}
		selected := 0
		for i, c := range cs {
			b.ExitPolicy = c.Exit
			a, err := evaluate(vr, q, funding, interval, c.Model, b)
			if err != nil {
				return fmt.Errorf("%s %s validation: %w", interval, c.Name, err)
			}
			m := a.Report.Metrics
			ir.Validation = append(ir.Validation, candidateScore{c.Name, c.Model, c.Exit, m})
			printMetrics(interval, "validation "+c.Name, m)
			if i > 0 && m.Trades >= *minTrades && m.NetReturnPct > 0 && m.NetReturnPct > ir.Validation[0].Metrics.NetReturnPct && m.WinRatePct > ir.Validation[0].Metrics.WinRatePct && (selected == 0 || m.NetReturnPct > ir.Validation[selected].Metrics.NetReturnPct) {
				selected = i
			}
		}
		ir.Selected = cs[selected].Name
		b.StartTime = tstart
		b.EndTime = te
		for _, idx := range []int{0, selected} {
			c := cs[idx]
			b.ExitPolicy = c.Exit
			a, err := evaluate(tr, q, funding, interval, c.Model, b)
			if err != nil {
				return err
			}
			if idx == 0 {
				ir.BaselineTest = a.Report.Metrics
			}
			if idx == selected {
				ir.SelectedTest = a.Report.Metrics
			}
			if err = writeJSON(filepath.Join(filepath.Dir(*out), interval+"-test-"+c.Name+".json"), a); err != nil {
				return err
			}
			printMetrics(interval, "test "+c.Name, a.Report.Metrics)
			if selected == 0 {
				break
			}
		}
		ir.BothImproved = selected != 0 && ir.SelectedTest.Trades >= *minTrades && ir.SelectedTest.NetReturnPct > 0 && ir.SelectedTest.NetReturnPct > ir.BaselineTest.NetReturnPct && ir.SelectedTest.WinRatePct > ir.BaselineTest.WinRatePct
		c := indicator.DefaultConfig()
		b.ExitPolicy = "signals"
		a, err := evaluate(tr, q, funding, interval, c, b)
		if err != nil {
			return err
		}
		ir.ChartDiagnostic = a.Report.Metrics
		if err = writeJSON(filepath.Join(filepath.Dir(*out), interval+"-chart.json"), a); err != nil {
			return err
		}
		r.Intervals = append(r.Intervals, ir)
	}
	return writeJSON(*out, r)
}

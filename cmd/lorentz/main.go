package main

import (
	"context"
	"encoding/csv"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"lorentzgo/indicator"
	"lorentzgo/market"
)

func main() {
	if len(os.Args) < 2 {
		fmt.Fprintln(os.Stderr, "usage: lorentz download|signals|backtest|research|optimize-eval|enhanced-eval|config-validate|walkforward-eval|fixed-continuous-eval|robust-eval [flags]")
		os.Exit(2)
	}
	var err error
	switch os.Args[1] {
	case "robust-eval":
		err = robustEval(os.Args[2:])
	case "download":
		ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
		defer stop()
		err = download(ctx, os.Args[2:])
	case "signals":
		err = signals(os.Args[2:])
	case "backtest":
		err = runBacktest(os.Args[2:])
	case "research":
		err = research(os.Args[2:])
	case "walkforward-eval":
		err = walkforwardEval(os.Args[2:])
	case "enhanced-eval":
		err = enhancedEval(os.Args[2:])
	case "config-validate":
		err = configValidate(os.Args[2:])
	case "fixed-continuous-eval":
		err = fixedContinuousEval(os.Args[2:])
	case "optimize-eval":
		err = optimizeEval(os.Args[2:])
	default:
		err = fmt.Errorf("unknown command %q", os.Args[1])
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		os.Exit(1)
	}
}

func date(s string) (int64, error) { t, e := time.Parse("2006-01-02", s); return t.UnixMilli(), e }
func iso(t int64) string           { return time.UnixMilli(t).UTC().Format(time.RFC3339) }
func writeJSON(path string, v any) error {
	if err := os.MkdirAll(filepath.Dir(path), 0755); err != nil {
		return err
	}
	b, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		return err
	}
	f, err := os.CreateTemp(filepath.Dir(path), ".report-*")
	if err != nil {
		return err
	}
	name := f.Name()
	defer os.Remove(name)
	if _, err = f.Write(append(b, '\n')); err != nil {
		f.Close()
		return err
	}
	if err = f.Close(); err != nil {
		return err
	}
	return os.Rename(name, path)
}

type datasetMetadata struct {
	Source                   string `json:"source"`
	Symbol                   string `json:"symbol"`
	DownloadedAt             string `json:"downloaded_at"`
	Start, End               int64
	FundingRequestedStart    int64                     `json:"funding_requested_start"`
	Candles                  map[string]market.Quality `json:"candles"`
	FundingRows              int                       `json:"funding_rows"`
	FundingEstimatedRows     int                       `json:"funding_estimated_rows,omitempty"`
	FundingStart, FundingEnd int64
	MaxFundingGapHours       float64 `json:"max_funding_gap_hours"`
}

func verifiedDataset(dir string) (datasetMetadata, error) {
	var m datasetMetadata
	raw, err := os.ReadFile(filepath.Join(dir, "metadata.json"))
	if err != nil {
		return m, err
	}
	if err = json.Unmarshal(raw, &m); err != nil {
		return m, err
	}
	if m.Symbol != "ETHUSDT" || m.Source != "https://fapi.binance.com (USD-M perpetual REST)" || m.FundingRows == 0 {
		return m, errors.New("this research requires a completed Binance ETHUSDT perpetual dataset with funding metadata")
	}
	return m, nil
}

func download(ctx context.Context, args []string) error {
	f := flag.NewFlagSet("download", flag.ContinueOnError)
	startS := f.String("start", "2020-01-01", "UTC start date inclusive")
	fundingStartS := f.String("funding-start", "2024-01-01", "UTC funding start; earlier Binance records may lack mark prices")
	endS := f.String("end", time.Now().UTC().Format("2006-01-02"), "UTC end date exclusive")
	symbol := f.String("symbol", "ETHUSDT", "USDT perpetual symbol")
	intervals := f.String("intervals", "15m,1h,4h,1d", "comma separated intervals")
	dir := f.String("dir", "data/ETHUSDT", "output directory")
	if e := f.Parse(args); e != nil {
		return e
	}
	start, e := date(*startS)
	if e != nil {
		return e
	}
	end, e := date(*endS)
	if e != nil {
		return e
	}
	if end > time.Now().UTC().Truncate(24*time.Hour).UnixMilli() || start >= end {
		return errors.New("use a nonempty range ending no later than the last completed UTC day")
	}
	client := &http.Client{Timeout: 30 * time.Second}
	fs, e := date(*fundingStartS)
	if e != nil {
		return e
	}
	fs = max(fs, start)
	if fs >= end {
		return errors.New("funding-start must precede end")
	}
	meta := datasetMetadata{Source: "https://fapi.binance.com (USD-M perpetual REST)", Symbol: *symbol, Start: start, End: end, DownloadedAt: time.Now().UTC().Format(time.RFC3339), Candles: map[string]market.Quality{}}
	meta.FundingRequestedStart = fs
	var previous datasetMetadata
	if raw, err := os.ReadFile(filepath.Join(*dir, "metadata.json")); err == nil {
		json.Unmarshal(raw, &previous)
	}
	canReuse := previous.Symbol == meta.Symbol && previous.Source == meta.Source && previous.Start == start && previous.End == end
	for _, interval := range strings.Split(*intervals, ",") {
		path := filepath.Join(*dir, interval+".csv")
		// Reuse only data whose validated coverage matches this exact request.
		if rows, q, err := market.LoadCandles(path, interval); canReuse && err == nil && q.Start == start && q.End == end && previous.Candles[interval] == q {
			meta.Candles[interval] = q
			fmt.Printf("%s reuse %d candles %s .. %s\n", interval, len(rows), iso(q.Start), iso(q.End))
			if err = writeJSON(filepath.Join(*dir, "metadata.json"), meta); err != nil {
				return err
			}
			continue
		}
		fmt.Printf("%s downloading %s .. %s\n", interval, iso(start), iso(end))
		rows, err := market.DownloadCandles(ctx, client, *symbol, interval, start, end)
		if err != nil {
			return err
		}
		q, err := market.ValidateCandles(rows, interval)
		if err != nil {
			return err
		}
		if q.Start != start || q.End != end {
			return fmt.Errorf("%s requested coverage incomplete: %+v", interval, q)
		}
		if err = market.SaveCandles(path, rows); err != nil {
			return err
		}
		meta.Candles[interval] = q
		if err = writeJSON(filepath.Join(*dir, "metadata.json"), meta); err != nil {
			return err
		}
		fmt.Printf("%s saved %d candles\n", interval, len(rows))
	}
	funding, err := market.DownloadFunding(ctx, client, *symbol, fs, end)
	if err != nil {
		return err
	}
	if len(funding) == 0 {
		return errors.New("empty funding history")
	}
	// ETHUSDT normally settles at least every eight hours. A larger gap must be
	// investigated; silently omitting funding is not an acceptable net-PnL result.
	for i, r := range funding {
		if i > 0 {
			gap := float64(r.Time-funding[i-1].Time) / 3600000
			meta.MaxFundingGapHours = max(meta.MaxFundingGapHours, gap)
		}
	}
	if err = fundingCoverage(funding, fs, end); err != nil {
		return fmt.Errorf("funding coverage requires investigation: %w", err)
	}
	if err = market.SaveFunding(filepath.Join(*dir, "funding.json"), funding); err != nil {
		return err
	}
	meta.FundingRows = len(funding)
	meta.FundingStart = funding[0].Time
	meta.FundingEnd = funding[len(funding)-1].Time
	fmt.Printf("funding saved %d settlements; largest gap %.4fh\n", len(funding), meta.MaxFundingGapHours)
	return writeJSON(filepath.Join(*dir, "metadata.json"), meta)
}

type modelFlags struct {
	dir, interval, algorithm, source     *string
	configPath                           *string
	flags                                *flag.FlagSet
	bars, scoreBars, neighbors, lookback *int
	fullHistory, dynamic, ema, smoothing *bool
	minVote                              *float64
}

func modelOptions(f *flag.FlagSet) modelFlags {
	return modelFlags{flags: f, configPath: f.String("config", "", "complete indicator JSON; explicit model flags override it"), dir: f.String("dir", "data/ETHUSDT", "dataset directory"), interval: f.String("interval", "15m", "timeframe"), algorithm: f.String("algorithm", "original-chart", "original-chart, original-online, aligned-knn"), source: f.String("source", "close", "close, hlc3, ohlc4"), bars: f.Int("bars", 10000, "last N chart candles; 0 uses all"), scoreBars: f.Int("score-bars", 2000, "last N candles to trade"), neighbors: f.Int("neighbors", 8, "neighbor count"), lookback: f.Int("lookback", 2000, "model history parameter"), fullHistory: f.Bool("full-history", false, "preserve original full-history switch"), dynamic: f.Bool("dynamic-exits", false, "source kernel dynamic exits"), ema: f.Bool("ema-filter", false, "EMA200 entry filter"), smoothing: f.Bool("kernel-smoothing", false, "source crossover smoothing"), minVote: f.Float64("min-vote", 0, "aligned-knn minimum absolute vote fraction")}
}
func (m modelFlags) model() (indicator.Config, error) {
	c := indicator.DefaultConfig()
	if *m.configPath != "" {
		data, err := os.ReadFile(*m.configPath)
		if err != nil {
			return c, err
		}
		c, err = indicator.DecodeConfig(data)
		if err != nil {
			return c, err
		}
	}
	m.flags.Visit(func(f *flag.Flag) {
		switch f.Name {
		case "algorithm":
			c.Algorithm = *m.algorithm
		case "source":
			c.Source = *m.source
		case "neighbors":
			c.Neighbors = *m.neighbors
		case "lookback":
			c.MaxBarsBack = *m.lookback
		case "full-history":
			c.IncludeFullHistory = *m.fullHistory
		case "dynamic-exits":
			c.UseDynamicExits = *m.dynamic
		case "ema-filter":
			c.UseEMAFilter = *m.ema
		case "kernel-smoothing":
			c.KernelSmoothing = *m.smoothing
		case "min-vote":
			c.MinVoteFraction = *m.minVote
		}
	})
	return c, c.Validate()
}
func (m modelFlags) load() ([]market.Candle, market.Quality, indicator.Config, error) {
	c, err := m.model()
	if err != nil {
		return nil, market.Quality{}, c, err
	}
	if *m.bars < 0 || *m.scoreBars < 2 {
		return nil, market.Quality{}, c, errors.New("bars must be nonnegative; score-bars at least 2")
	}
	rows, q, err := market.LoadCandles(filepath.Join(*m.dir, *m.interval+".csv"), *m.interval)
	if err != nil {
		return nil, q, c, err
	}
	meta, err := verifiedDataset(*m.dir)
	if err != nil {
		return nil, q, c, err
	}
	if meta.Candles[*m.interval] != q {
		return nil, q, c, errors.New("candle coverage differs from dataset metadata")
	}
	if *m.bars > 0 && len(rows) > *m.bars {
		rows = rows[len(rows)-*m.bars:]
	}
	if len(rows) < *m.scoreBars {
		return nil, q, c, errors.New("not enough candles for score-bars")
	}
	return rows, q, c, nil
}

func signals(args []string) error {
	f := flag.NewFlagSet("signals", flag.ContinueOnError)
	m := modelOptions(f)
	out := f.String("out", "results/signals.csv", "CSV trace path")
	if e := f.Parse(args); e != nil {
		return e
	}
	rows, _, c, e := m.load()
	if e != nil {
		return e
	}
	x, e := indicator.Compute(rows, c)
	if e != nil {
		return e
	}
	p, e := indicator.RunInputs(x, c)
	if e != nil {
		return e
	}
	if e = os.MkdirAll(filepath.Dir(*out), 0755); e != nil {
		return e
	}
	file, e := os.Create(*out)
	if e != nil {
		return e
	}
	defer file.Close()
	w := csv.NewWriter(file)
	defer w.Flush()
	if e = w.Write([]string{"open_time", "close", "f1", "f2", "f3", "f4", "f5", "kernel_rq", "kernel_gaussian", "filter_ok", "prediction", "signal", "start_long", "start_short", "end_long", "end_short", "search_start", "search_end", "max_neighbor_index"}); e != nil {
		return e
	}
	for i, v := range p {
		r := []string{strconv.FormatInt(rows[i].OpenTime, 10), strconv.FormatFloat(rows[i].Close, 'g', 17, 64)}
		for _, z := range x[i].Features {
			r = append(r, strconv.FormatFloat(z, 'g', 17, 64))
		}
		r = append(r, fmt.Sprint(x[i].KernelRQ), fmt.Sprint(x[i].KernelGaussian), fmt.Sprint(v.FilterOK), fmt.Sprint(v.Prediction), fmt.Sprint(v.Signal), fmt.Sprint(v.StartLong), fmt.Sprint(v.StartShort), fmt.Sprint(v.EndLong), fmt.Sprint(v.EndShort), fmt.Sprint(v.SearchStart), fmt.Sprint(v.SearchEnd), fmt.Sprint(v.MaxNeighborIndex))
		if e = w.Write(r); e != nil {
			return e
		}
	}
	w.Flush()
	if e = w.Error(); e != nil {
		return e
	}
	fmt.Printf("wrote %d signal rows to %s\n", len(p), *out)
	return nil
}

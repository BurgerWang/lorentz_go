// Finite comparison against unmodified published TradingView exports. Missing
// targets are reported as coverage gaps; never manufactured from the Go output.
package main

import (
	"encoding/csv"
	"encoding/json"
	"fmt"
	"lorentzgo/indicator"
	"lorentzgo/market"
	"math"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

func main() {
	files, e := filepath.Glob("reference/tradingview/*.csv")
	if e != nil || len(files) != 4 {
		panic("need four real official CSVs")
	}
	summaries := []map[string]any{}
	for _, path := range files {
		f, e := os.Open(path)
		if e != nil {
			panic(e)
		}
		csvRows, e := csv.NewReader(f).ReadAll()
		f.Close()
		if e != nil {
			panic(e)
		}
		cols := map[string]int{}
		for j, h := range csvRows[0] {
			cols[h] = j
		}
		raw := csvRows[1:]
		rows := make([]market.Candle, len(raw))
		number := func(s string) float64 {
			v, e := strconv.ParseFloat(s, 64)
			if e != nil {
				panic(e)
			}
			return v
		}
		for i, r := range raw {
			t := int64(number(r[cols["time"]]) * 1000)
			rows[i] = market.Candle{OpenTime: t, CloseTime: t, Open: number(r[cols["open"]]), High: number(r[cols["high"]]), Low: number(r[cols["low"]]), Close: number(r[cols["close"]])}
		}
		c := indicator.DefaultConfig()
		c.IncludeFullHistory = strings.Contains(path, "_full_history")
		x, e := indicator.Compute(rows, c)
		if e != nil {
			panic(e)
		}
		p, e := indicator.RunInputs(x, c)
		if e != nil {
			panic(e)
		}
		compared := map[string]int{}
		mismatch := map[string]int{}
		unavailable := map[string]int{}
		maxError := map[string]float64{}
		floatNames := []string{"F1_RSI", "F2_WT", "F3_CCI", "F4_ADX", "F5_RSI9", "Kernel Regression Estimate"}
		for j, name := range floatNames {
			for i, r := range raw {
				v := r[cols[name]]
				if v == "" {
					continue
				}
				compared[name]++
				want := number(v)
				got := x[i].KernelRQ
				if j < 5 {
					got = x[i].Features[j]
				}
				if math.IsNaN(got) || math.IsInf(got, 0) {
					unavailable[name]++
					mismatch[name]++
					continue
				}
				delta := math.Abs(got - want)
				maxError[name] = math.Max(delta, maxError[name])
				if delta > 1e-6 {
					mismatch[name]++
				}
			}
		}
		gate := max(0, len(rows)-1-c.MaxBarsBack)
		for i := gate; i < len(raw); i++ {
			r := raw[i]
			for _, v := range []struct {
				name  string
				value int
			}{{"Prediction", p[i].Prediction}, {"Direction", p[i].Signal}} {
				if r[cols[v.name]] == "" {
					continue
				}
				compared[v.name]++
				if number(r[cols[v.name]]) != float64(v.value) {
					mismatch[v.name]++
				}
			}
			for _, v := range []struct {
				name  string
				value bool
			}{{"Buy", p[i].StartLong}, {"Sell", p[i].StartShort}} {
				compared[v.name]++
				want := r[cols[v.name]] != "" && number(r[cols[v.name]]) != 0
				if v.value != want {
					mismatch[v.name]++
				}
			}
		}
		summaries = append(summaries, map[string]any{"file": path, "bars": len(rows), "signal_comparison_start_index": gate, "config": c, "compared": compared, "mismatches": mismatch, "go_unavailable": unavailable, "maximum_absolute_error": maxError, "gaps": []string{"StopBuy/StopSell exports absent (show_exits=false); no exit parity claim", "No backtest stream exported", "H1 features all absent; trimmed kernel starts before fixed-window readiness", "Pine price-scale arithmetic not specified in legacy Go schema; deviations reported, Classic unchanged"}})
	}
	if e := json.NewEncoder(os.Stdout).Encode(map[string]any{"tolerance": 1e-6, "source": "https://github.com/artificial-intelligence-edge/lorentzian-classification/tree/main/tests/parity/baselines", "comparisons": summaries, "scope": "original-chart only; no trading/online equivalence claim"}); e != nil {
		panic(fmt.Sprint(e))
	}
}

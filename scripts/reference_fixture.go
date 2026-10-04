package main

import (
	"encoding/json"
	"lorentzgo/indicator"
	"os"
)

func main() {
	x := make([]indicator.Inputs, 31)
	for i := range x {
		v := float64((i * 7) % 13)
		x[i] = indicator.Inputs{Source: 100 + v, Features: [5]float64{float64(i%7) / 10, float64(i%3) / 10, 0, 0, 0}, VolatilityOK: true, RegimeOK: true, ADXOK: true, EMAUp: true, EMADown: true, SMAUp: true, SMADown: true, KernelRQ: v, KernelGaussian: v + float64(i%3-1)}
	}
	results := map[string][]indicator.Point{}
	for _, a := range []string{"original-chart", "original-online", "aligned-knn"} {
		for _, f := range []bool{false, true} {
			c := indicator.DefaultConfig()
			c.Algorithm = a
			c.IncludeFullHistory = f
			c.Neighbors = 3
			c.MaxBarsBack = 8
			c.UseDynamicExits = true
			o, e := indicator.RunInputs(x, c)
			if e != nil {
				panic(e)
			}
			key := a
			if f {
				key += "-full"
			}
			results[key] = o
		}
	}
	if e := json.NewEncoder(os.Stdout).Encode(results); e != nil {
		panic(e)
	}
}

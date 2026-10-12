package main

import (
	"encoding/json"
	"lorentzgo/market"
	"os"
	"path/filepath"
	"testing"
)

func TestDatasetRejectsWrongSymbolAndIncompleteDownload(t *testing.T) {
	dir := t.TempDir()
	m := datasetMetadata{Source: "https://fapi.binance.com (USD-M perpetual REST)", Symbol: "BTCUSDT", FundingRows: 10}
	write := func() {
		t.Helper()
		b, _ := json.Marshal(m)
		if err := os.WriteFile(filepath.Join(dir, "metadata.json"), b, 0644); err != nil {
			t.Fatal(err)
		}
	}
	write()
	if _, err := verifiedDataset(dir); err == nil {
		t.Fatal("wrong market would be labeled ETHUSDT")
	}
	m.Symbol = "ETHUSDT"
	m.FundingRows = 0
	write()
	if _, err := verifiedDataset(dir); err == nil {
		t.Fatal("incomplete dataset accepted")
	}
	m.FundingRows = 10
	write()
	if _, err := verifiedDataset(dir); err != nil {
		t.Fatal(err)
	}
}

func TestFundingCoverageRejectsCachedTruncation(t *testing.T) {
	h := int64(3600000)
	rows := []market.Funding{{Time: 0, MarkPrice: 100}, {Time: 8 * h, MarkPrice: 100}, {Time: 16 * h, MarkPrice: 100}}
	if err := fundingCoverage(rows, 0, 24*h); err != nil {
		t.Fatal(err)
	}
	// A short final window legitimately contains no funding settlement.
	if err := fundingCoverage(rows, 23*h+30*60000, 24*h); err != nil {
		t.Fatal(err)
	}
	for _, bad := range [][]market.Funding{nil, rows[:1], {rows[0], rows[2]}} {
		if err := fundingCoverage(bad, 0, 24*h); err == nil {
			t.Fatal("accepted incomplete cached funding")
		}
	}
}

func TestWindowCalendarBoundsAndWarmup(t *testing.T) {
	a := make([]market.Candle, 20)
	for i := range a {
		a[i].OpenTime = int64(i * 100)
		a[i].CloseTime = int64(i*100 + 99)
	}
	r, start, err := window(a, 800, 1500, 10, 4)
	if err != nil {
		t.Fatal(err)
	}
	if len(r) != 10 || r[0].OpenTime != 500 || r[len(r)-1].CloseTime != 1499 || start != 1100 {
		t.Fatalf("window=%+v start=%d", r, start)
	}
	r, start, err = window(a, 1300, 1500, 10, 4)
	if err != nil {
		t.Fatal(err)
	}
	if start != 1300 || len(r) != 10 {
		t.Fatal("calendar scoring start lost")
	}
}

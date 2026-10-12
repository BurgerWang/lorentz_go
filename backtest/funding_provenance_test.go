package backtest

import (
	"lorentzgo/indicator"
	"lorentzgo/market"
	"reflect"
	"strings"
	"testing"
)

func TestFundingProxyLedgerDisclosureAndUnchangedExecution(t *testing.T) {
	c := candles(100, 110, 120)
	s := make([]indicator.Point, len(c))
	s[0].StartLong = true
	s[1].EndLong = true
	f := []market.Funding{{Time: 150, Rate: .001, MarkPrice: 105}}
	exact := run(t, c, s, f, config())
	f[0].MarkPriceSource = market.FundingMarkKlineOpen8h
	estimated := run(t, c, s, f, config())
	if !reflect.DeepEqual(exact.Trades, estimated.Trades) || !reflect.DeepEqual(exact.Equity, estimated.Equity) || exact.Metrics != estimated.Metrics {
		t.Fatal("provenance changed execution or accounting")
	}
	near(t, estimated.Trades[0].Funding, -(10000.0/110)*.001*105)
	if len(estimated.Assumptions) != len(exact.Assumptions)+1 || !strings.Contains(estimated.Assumptions[len(exact.Assumptions)], "historical actual funding rates") || !strings.Contains(estimated.Assumptions[len(exact.Assumptions)], "not exact settlement amounts") {
		t.Fatal("missing explicit estimated funding disclosure")
	}
	for _, bad := range []market.Funding{
		{Time: 150, MarkPrice: 105, MarkPriceSource: "unknown"},
		{Time: 150, MarkPrice: 105, MarkPriceSource: market.FundingMarkKlineOpen8h, MarkPriceTime: 28800000},
		{Time: 28800000, MarkPrice: 105, MarkPriceSource: market.FundingMarkKlineOpen8h},
		{Time: 150, MarkPrice: 105, MarkPriceTime: 1},
		{Time: 150, MarkPriceSource: market.FundingMarkKlineOpen8h},
	} {
		if _, err := Evaluate(c, s, []market.Funding{bad}, config()); err == nil {
			t.Fatalf("direct Evaluate accepted invalid funding: %+v", bad)
		}
	}
}

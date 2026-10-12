package market

import (
	"testing"
)

func TestDailyConsistencyRejectsOtherMarket(t *testing.T) {
	low := make([]Candle, 48)
	for i := range low {
		at := int64(i) * 3600000
		low[i] = Candle{OpenTime: at, CloseTime: at + 3600000 - 1, Open: 100, High: 101, Low: 99, Close: 100, Volume: 10}
	}
	daily := []Candle{{OpenTime: 0, CloseTime: 86400000 - 1, Open: 100, High: 101, Low: 99, Close: 100, Volume: 240}, {OpenTime: 86400000, CloseTime: 2*86400000 - 1, Open: 100, High: 101, Low: 99, Close: 100, Volume: 240}}
	if _, e := ValidateDailyAggregation(low, daily, "1h"); e != nil {
		t.Fatal(e)
	}
	daily[0].Close = 100.1
	if _, e := ValidateDailyAggregation(low, daily, "1h"); e == nil {
		t.Fatal("accepted inconsistent price")
	}
	daily[0].Close = 100
	if _, e := ValidateDailyAggregation(low[1:], daily, "1h"); e == nil {
		t.Fatal("accepted partial day")
	}
}

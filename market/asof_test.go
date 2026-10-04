package market

import (
	"math"
	"testing"
)

const asofDay int64 = 86400000
const asofHour int64 = 3600000

func asofRows(start, step int64, n int) []Candle {
	rows := make([]Candle, n)
	for i := range rows {
		open := start + int64(i)*step
		rows[i] = Candle{OpenTime: open, CloseTime: open + step - 1, Open: 100, High: 101, Low: 99, Close: 100, Volume: 1}
	}
	return rows
}

func TestAlignDailyExactAvailabilityAndDirection(t *testing.T) {
	daily := asofRows(0, asofDay, 5)
	low := asofRows(2*asofDay-2*asofHour, asofHour, 28)
	ctx, err := AlignDaily(low, daily, []float64{100, 110, 110, 90, 200})
	if err != nil {
		t.Fatal(err)
	}
	if ctx[0].Ready || ctx[0].AvailableTime != asofDay || ctx[0].Center != 100 {
		t.Fatalf("pre-boundary %+v", ctx[0])
	}
	if !ctx[1].Ready || ctx[1].Direction != 1 || ctx[1].Slope != 10 || ctx[1].AvailableTime != 2*asofDay {
		t.Fatalf("equal boundary %+v", ctx[1])
	}
	if !ctx[25].Ready || ctx[25].Direction != 0 || ctx[25].Slope != 0 {
		t.Fatalf("flat boundary %+v", ctx[25])
	}
	// Daily future data and center changes are invisible before availability.
	altered := []float64{100, 110, 110, -999, 999}
	other, _ := AlignDaily(low, daily, altered)
	for i := range ctx {
		if ctx[i] != other[i] && !(math.IsNaN(ctx[i].Slope) && math.IsNaN(other[i].Slope) && ctx[i].Center == other[i].Center) {
			t.Fatalf("future changed prefix %d", i)
		}
	}
	short, _ := AlignDaily(low[:20], daily[:2], []float64{100, 110})
	for i := 1; i < len(short); i++ {
		if short[i] != ctx[i] {
			t.Fatalf("append changed prefix %d", i)
		}
	}
}

func TestAlignDailyWarmupAndDown(t *testing.T) {
	daily := asofRows(0, asofDay, 4)
	low := asofRows(2*asofDay-asofHour, asofHour, 49)
	ctx, err := AlignDaily(low, daily, []float64{math.NaN(), 100, 90, 80})
	if err != nil {
		t.Fatal(err)
	}
	if ctx[0].Ready || !math.IsNaN(ctx[0].Slope) {
		t.Fatal("kernel warmup must not be ready")
	}
	if !ctx[24].Ready || ctx[24].Direction != -1 || ctx[24].Slope != -10 {
		t.Fatalf("down direction %+v", ctx[24])
	}
}

func TestAlignDailyRejectsMissingAndWrongCache(t *testing.T) {
	daily := asofRows(0, asofDay, 4)
	low := asofRows(2*asofDay-asofHour, asofHour, 49)
	centers := []float64{100, 101, 102, 103}
	if _, err := AlignDaily(low, daily, centers[:3]); err == nil {
		t.Fatal("accepted missing center")
	}
	if _, err := AlignDaily(low, daily[2:], centers[2:]); err == nil {
		t.Fatal("accepted missing leading day")
	}
	if _, err := AlignDaily(low, daily[:3], centers[:3]); err == nil {
		t.Fatal("accepted missing final required day")
	}
	gap := append([]Candle(nil), daily...)
	gap[2] = gap[3]
	if _, err := AlignDaily(low, gap, centers); err == nil {
		t.Fatal("accepted daily gap")
	}
	shift := append([]Candle(nil), daily...)
	shift[0].OpenTime++
	shift[0].CloseTime++
	if _, err := AlignDaily(low, shift, centers); err == nil {
		t.Fatal("accepted non-UTC daily alignment")
	}
	badlow := append([]Candle(nil), low...)
	badlow[1] = badlow[2]
	if _, err := AlignDaily(badlow, daily, centers); err == nil {
		t.Fatal("accepted low gap")
	}
}

package market

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strconv"
	"strings"
	"testing"
	"time"
)

const testStep int64 = 900000

func candle(t int64) Candle {
	return Candle{OpenTime: t, CloseTime: t + testStep - 1, Open: 10, High: 12, Low: 9, Close: 11, Volume: 2}
}
func kline(t int64) []any {
	return []any{t, "10", "12", "9", "11", "2", t + testStep - 1, "0", 1, "0", "0", "0"}
}

func TestCandlePagination(t *testing.T) {
	var starts []int64
	s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start, _ := strconv.ParseInt(r.URL.Query().Get("startTime"), 10, 64)
		starts = append(starts, start)
		if r.URL.Path != "/fapi/v1/klines" || r.URL.Query().Get("interval") != "15m" || r.URL.Query().Get("limit") != "1500" || r.URL.Query().Get("endTime") != strconv.FormatInt(1502*testStep+123-1, 10) {
			t.Errorf("bad query: %s", r.URL)
		}
		var page [][]any
		for i := start / testStep; i <= 1502 && len(page) < 1500; i++ {
			page = append(page, kline(i*testStep))
		}
		json.NewEncoder(w).Encode(page)
	}))
	defer s.Close()
	rows, err := NewDownloader(s.Client(), s.URL).DownloadCandles(context.Background(), "ETHUSDT", "15m", 0, 1502*testStep+123)
	if err != nil {
		t.Fatal(err)
	}
	if len(rows) != 1502 || !reflect.DeepEqual(starts, []int64{0, 1500 * testStep}) {
		t.Fatalf("rows=%d starts=%v", len(rows), starts)
	}
	if rows[len(rows)-1].CloseTime >= 1502*testStep+123 {
		t.Fatal("unclosed candle retained")
	}
}

func TestHTTPFailuresRetryAndCancel(t *testing.T) {
	for _, code := range []int{400, 429, 500} {
		t.Run(strconv.Itoa(code), func(t *testing.T) {
			calls := 0
			s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls++
				w.WriteHeader(code)
				fmt.Fprint(w, `{"code":-1,"msg":"bad"}`)
			}))
			defer s.Close()
			d := NewDownloader(s.Client(), s.URL)
			d.retryDelay = time.Millisecond
			_, err := d.DownloadFunding(context.Background(), "ETHUSDT", 0, 1000)
			want := 1
			if code != 400 {
				want = 4
			}
			if err == nil || calls != want {
				t.Fatalf("err=%v calls=%d", err, calls)
			}
		})
	}
	calls := 0
	s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if calls == 1 {
			w.WriteHeader(429)
			return
		}
		fmt.Fprint(w, `[]`)
	}))
	defer s.Close()
	d := NewDownloader(s.Client(), s.URL)
	d.retryDelay = time.Millisecond
	if _, err := d.DownloadFunding(context.Background(), "ETHUSDT", 0, 1000); err != nil || calls != 2 {
		t.Fatalf("retry err=%v calls=%d", err, calls)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := d.DownloadFunding(ctx, "ETHUSDT", 0, 1000); err == nil {
		t.Fatal("cancel ignored")
	}
	backoff := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(429) }))
	defer backoff.Close()
	ctx, cancel = context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	if _, err := NewDownloader(backoff.Client(), backoff.URL).DownloadFunding(ctx, "ETHUSDT", 0, 1000); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("backoff cancellation: %v", err)
	}
}

func TestQualityDefects(t *testing.T) {
	cases := map[string][]Candle{"gap": {candle(0), candle(2 * testStep)}, "duplicate": {candle(0), candle(0)}, "reverse": {candle(testStep), candle(0)}, "misaligned": {candle(1)}, "empty": nil}
	bad := candle(0)
	bad.CloseTime++
	cases["close time"] = []Candle{bad}
	bad = candle(0)
	bad.High = 9
	cases["ohlc"] = []Candle{bad}
	bad = candle(0)
	bad.Volume = -1
	cases["volume"] = []Candle{bad}
	for name, rows := range cases {
		t.Run(name, func(t *testing.T) {
			if _, err := ValidateCandles(rows, "15m"); err == nil {
				t.Fatal("accepted invalid series")
			}
		})
	}
	q, err := ValidateCandles([]Candle{candle(0), candle(testStep)}, "15m")
	if err != nil || q.Rows != 2 || q.End != 2*testStep {
		t.Fatalf("q=%+v err=%v", q, err)
	}
}

func TestParseStrict(t *testing.T) {
	for _, value := range []string{"\"NaN\"", "\"Inf\"", "null", "\"bad\""} {
		if _, err := rawFloat(json.RawMessage(value)); err == nil {
			t.Fatalf("accepted %s", value)
		}
	}
	if _, err := rawInt(json.RawMessage("1.1")); err == nil {
		t.Fatal("fractional timestamp accepted")
	}
	if _, err := parseKline([]json.RawMessage{}); err == nil {
		t.Fatal("short kline accepted")
	}
}

func TestStorageRoundTrip(t *testing.T) {
	dir := t.TempDir()
	rows := []Candle{candle(0), candle(testStep)}
	path := filepath.Join(dir, "new", "nested", "candles.csv")
	if err := SaveCandles(path, rows); err != nil {
		t.Fatal(err)
	}
	got, q, err := LoadCandles(path, "15m")
	if err != nil || !reflect.DeepEqual(got, rows) || q.Rows != 2 {
		t.Fatalf("got=%v q=%+v err=%v", got, q, err)
	}
	if err := os.WriteFile(path, []byte("0,10,12,9,11,2,899999,0,1,0,0,0\n"), 0600); err != nil {
		t.Fatal(err)
	}
	if _, _, err := LoadCandles(path, "15m"); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte("0,10,12,9,11,NaN,899999\n"), 0600); err != nil {
		t.Fatal(err)
	}
	if _, _, err := LoadCandles(path, "15m"); err == nil {
		t.Fatal("accepted NaN")
	}
	funds := []Funding{{Time: 0, Rate: -0.0001, MarkPrice: 1000}, {Time: 100, Rate: 0.0001, MarkPrice: 1001}}
	path = filepath.Join(dir, "funding.json")
	if err := SaveFunding(path, funds); err != nil {
		t.Fatal(err)
	}
	fg, err := LoadFunding(path)
	if err != nil || !reflect.DeepEqual(fg, funds) {
		t.Fatalf("funding=%v err=%v", fg, err)
	}
}

func TestFundingPaginationAndDefects(t *testing.T) {
	var starts []int64
	s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start, _ := strconv.ParseInt(r.URL.Query().Get("startTime"), 10, 64)
		starts = append(starts, start)
		if r.URL.Query().Get("limit") != "1000" {
			t.Error("funding limit")
		}
		var page []map[string]any
		for i := start; i < 1003 && len(page) < 1000; i++ {
			page = append(page, map[string]any{"symbol": "ETHUSDT", "fundingTime": i, "fundingRate": "-0.001", "markPrice": "1000"})
		}
		json.NewEncoder(w).Encode(page)
	}))
	defer s.Close()
	rows, err := NewDownloader(s.Client(), s.URL).DownloadFunding(context.Background(), "ETHUSDT", 0, 1003)
	if err != nil || len(rows) != 1003 || !reflect.DeepEqual(starts, []int64{0, 1000}) {
		t.Fatalf("rows=%d starts=%v err=%v", len(rows), starts, err)
	}
	for _, rows := range [][]Funding{{{Time: 1, MarkPrice: 1}, {Time: 1, MarkPrice: 1}}, {{Time: 2, MarkPrice: 1}, {Time: 1, MarkPrice: 1}}, {{Time: 1, MarkPrice: 0}}} {
		if err := ValidateFunding(rows); err == nil {
			t.Fatal("accepted invalid funding")
		}
	}
}

func TestMissingTailAndBadPages(t *testing.T) {
	for _, body := range []string{`[]`, `[[0,"10","12","9","11","2",899999,"0",1,"0","0","0"]]`, `[[0,"bad","12","9","11","2",899999,"0",1,"0","0","0"]]`} {
		calls := 0
		s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			calls++
			if calls > 1 {
				fmt.Fprint(w, `[]`)
			} else {
				fmt.Fprint(w, body)
			}
		}))
		_, err := NewDownloader(s.Client(), s.URL).DownloadCandles(context.Background(), "ETHUSDT", "15m", 0, 2*testStep)
		s.Close()
		if err == nil {
			t.Fatalf("accepted missing/bad page %s", strings.TrimSpace(body))
		}
	}
}

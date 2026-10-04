package market

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"
)

const binanceURL = "https://fapi.binance.com"

// Downloader allows an alternate public endpoint for reproducible HTTP tests.
// Production wrappers always use Binance HTTPS. Client controls HTTP timeouts.
type Downloader struct {
	Client     *http.Client
	BaseURL    string
	retryDelay time.Duration
}

func NewDownloader(client *http.Client, baseURL string) *Downloader {
	if client == nil {
		client = &http.Client{Timeout: 30 * time.Second}
	}
	return &Downloader{Client: client, BaseURL: strings.TrimRight(baseURL, "/"), retryDelay: time.Second}
}

func DownloadCandles(ctx context.Context, client *http.Client, symbol, interval string, start, end int64) ([]Candle, error) {
	return NewDownloader(client, binanceURL).DownloadCandles(ctx, symbol, interval, start, end)
}
func DownloadFunding(ctx context.Context, client *http.Client, symbol string, start, end int64) ([]Funding, error) {
	return NewDownloader(client, binanceURL).DownloadFunding(ctx, symbol, start, end)
}

func (d *Downloader) get(ctx context.Context, path string, query url.Values, out any) error {
	for attempt := 0; attempt < 4; attempt++ {
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, d.BaseURL+path+"?"+query.Encode(), nil)
		if err != nil {
			return err
		}
		resp, err := d.Client.Do(req)
		if err != nil {
			return fmt.Errorf("request %s: %w", path, err)
		}
		body, readErr := io.ReadAll(io.LimitReader(resp.Body, 16*1024*1024+1))
		resp.Body.Close()
		if readErr != nil {
			return fmt.Errorf("read %s: %w", path, readErr)
		}
		if len(body) > 16*1024*1024 {
			return fmt.Errorf("response too large: %s", path)
		}
		if resp.StatusCode == http.StatusOK {
			if err := json.Unmarshal(body, out); err != nil {
				return fmt.Errorf("decode %s: %w", path, err)
			}
			return nil
		}
		if (resp.StatusCode == 429 || resp.StatusCode >= 500) && attempt < 3 {
			wait := d.retryDelay * time.Duration(1<<attempt)
			if v := resp.Header.Get("Retry-After"); v != "" {
				if seconds, e := strconv.Atoi(v); e == nil && seconds > 0 {
					wait = time.Duration(seconds) * time.Second
				} else if date, e := http.ParseTime(v); e == nil && time.Until(date) > 0 {
					wait = time.Until(date)
				}
			}
			if wait > 30*time.Second {
				wait = 30 * time.Second
			}
			timer := time.NewTimer(wait)
			select {
			case <-ctx.Done():
				timer.Stop()
				return ctx.Err()
			case <-timer.C:
			}
			continue
		}
		if len(body) > 512 {
			body = body[:512]
		}
		return fmt.Errorf("%s HTTP %d: %s", path, resp.StatusCode, strings.TrimSpace(string(body)))
	}
	return fmt.Errorf("retry limit exceeded")
}

func rawInt(raw json.RawMessage) (int64, error) { return strconv.ParseInt(string(raw), 10, 64) }
func rawFloat(raw json.RawMessage) (float64, error) {
	s := string(raw)
	if len(s) > 0 && s[0] == '"' {
		if err := json.Unmarshal(raw, &s); err != nil {
			return 0, err
		}
	}
	v, err := strconv.ParseFloat(s, 64)
	if err != nil {
		return 0, err
	}
	if !finite(v) {
		return 0, fmt.Errorf("nonfinite number %q", s)
	}
	return v, nil
}

func parseKline(raw []json.RawMessage) (Candle, error) {
	var c Candle
	if len(raw) != 12 {
		return c, fmt.Errorf("expected 12 kline fields, got %d", len(raw))
	}
	var err error
	if c.OpenTime, err = rawInt(raw[0]); err != nil {
		return c, err
	}
	if c.CloseTime, err = rawInt(raw[6]); err != nil {
		return c, err
	}
	values := []*float64{&c.Open, &c.High, &c.Low, &c.Close, &c.Volume}
	for i, p := range values {
		if *p, err = rawFloat(raw[i+1]); err != nil {
			return c, fmt.Errorf("kline field %d: %w", i+1, err)
		}
	}
	return c, nil
}

func rangeQuery(symbol string, start, end int64, limit int) url.Values {
	return url.Values{"symbol": {symbol}, "startTime": {strconv.FormatInt(start, 10)}, "endTime": {strconv.FormatInt(end-1, 10)}, "limit": {strconv.Itoa(limit)}}
}

func (d *Downloader) DownloadCandles(ctx context.Context, symbol, interval string, start, end int64) ([]Candle, error) {
	step, err := IntervalMillis(interval)
	if err != nil {
		return nil, err
	}
	if symbol == "" || start < 0 || end <= start {
		return nil, fmt.Errorf("invalid symbol or time range")
	}
	var rows []Candle
	for cursor := start; cursor < end; {
		q := rangeQuery(symbol, cursor, end, 1500)
		q.Set("interval", interval)
		var page [][]json.RawMessage
		if err := d.get(ctx, "/fapi/v1/klines", q, &page); err != nil {
			return nil, err
		}
		if len(page) == 0 {
			break
		}
		var last int64
		for i, raw := range page {
			c, err := parseKline(raw)
			if err != nil {
				return nil, fmt.Errorf("parse page row %d: %w", i, err)
			}
			if c.OpenTime < cursor || c.OpenTime >= end || (i > 0 && c.OpenTime <= last) {
				return nil, fmt.Errorf("kline page timestamp outside range or not increasing: %d", c.OpenTime)
			}
			last = c.OpenTime
			if c.CloseTime < end {
				rows = append(rows, c)
			}
		}
		cursor = last + step
	}
	if _, err := ValidateCandles(rows, interval); err != nil {
		return nil, err
	}
	if last := rows[len(rows)-1]; last.CloseTime+1 != end/step*step {
		return nil, fmt.Errorf("missing terminal candle: coverage ends %d, expected %d", last.CloseTime+1, end/step*step)
	}
	return rows, nil
}

func (d *Downloader) DownloadFunding(ctx context.Context, symbol string, start, end int64) ([]Funding, error) {
	if symbol == "" || start < 0 || end <= start {
		return nil, fmt.Errorf("invalid symbol or time range")
	}
	var rows []Funding
	for cursor := start; cursor < end; {
		var page []struct {
			Symbol string          `json:"symbol"`
			Time   int64           `json:"fundingTime"`
			Rate   json.RawMessage `json:"fundingRate"`
			Price  json.RawMessage `json:"markPrice"`
		}
		if err := d.get(ctx, "/fapi/v1/fundingRate", rangeQuery(symbol, cursor, end, 1000), &page); err != nil {
			return nil, err
		}
		if len(page) == 0 {
			break
		}
		for _, r := range page {
			if r.Symbol != symbol || r.Time < cursor || r.Time >= end {
				return nil, fmt.Errorf("funding row symbol or timestamp outside requested range")
			}
			rate, err := rawFloat(r.Rate)
			if err != nil {
				return nil, fmt.Errorf("funding rate: %w", err)
			}
			price, err := rawFloat(r.Price)
			if err != nil {
				return nil, fmt.Errorf("funding mark price: %w", err)
			}
			rows = append(rows, Funding{Time: r.Time, Rate: rate, MarkPrice: price})
		}
		if err := ValidateFunding(rows); err != nil {
			return nil, err
		}
		cursor = page[len(page)-1].Time + 1
	}
	return rows, nil
}

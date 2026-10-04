package market

import (
	"encoding/csv"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strconv"
)

// SaveCandles writes the seven-column CSV header
// open_time,open,high,low,close,volume,close_time. Floats round trip exactly.
func SaveCandles(path string, rows []Candle) error {
	return atomicFile(path, func(w io.Writer) error {
		cw := csv.NewWriter(w)
		if err := cw.Write([]string{"open_time", "open", "high", "low", "close", "volume", "close_time"}); err != nil {
			return err
		}
		for _, c := range rows {
			record := []string{strconv.FormatInt(c.OpenTime, 10)}
			for _, v := range []float64{c.Open, c.High, c.Low, c.Close, c.Volume} {
				record = append(record, strconv.FormatFloat(v, 'g', -1, 64))
			}
			record = append(record, strconv.FormatInt(c.CloseTime, 10))
			if err := cw.Write(record); err != nil {
				return err
			}
		}
		cw.Flush()
		return cw.Error()
	})
}

// LoadCandles accepts our CSV or Binance's headerless 12-column kline CSV.
// The first seven fields in either format have the same semantics.
func LoadCandles(path, interval string) ([]Candle, Quality, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, Quality{}, err
	}
	defer f.Close()
	r := csv.NewReader(f)
	r.FieldsPerRecord = -1
	var rows []Candle
	for line := 1; ; line++ {
		record, err := r.Read()
		if err == io.EOF {
			break
		}
		if err != nil {
			return nil, Quality{}, fmt.Errorf("CSV line %d: %w", line, err)
		}
		if line == 1 && len(record) == 7 && record[0] == "open_time" {
			expected := []string{"open_time", "open", "high", "low", "close", "volume", "close_time"}
			for i, v := range expected {
				if record[i] != v {
					return nil, Quality{}, fmt.Errorf("unexpected CSV header field %d", i)
				}
			}
			continue
		}
		if len(record) != 7 && len(record) != 12 {
			return nil, Quality{}, fmt.Errorf("CSV line %d: expected 7 or 12 columns", line)
		}
		var c Candle
		if c.OpenTime, err = strconv.ParseInt(record[0], 10, 64); err != nil {
			return nil, Quality{}, fmt.Errorf("CSV line %d open time: %w", line, err)
		}
		if c.CloseTime, err = strconv.ParseInt(record[6], 10, 64); err != nil {
			return nil, Quality{}, fmt.Errorf("CSV line %d close time: %w", line, err)
		}
		values := []*float64{&c.Open, &c.High, &c.Low, &c.Close, &c.Volume}
		for i, p := range values {
			if *p, err = strconv.ParseFloat(record[i+1], 64); err != nil {
				return nil, Quality{}, fmt.Errorf("CSV line %d column %d: %w", line, i+1, err)
			}
		}
		rows = append(rows, c)
	}
	q, err := ValidateCandles(rows, interval)
	return rows, q, err
}

func SaveFunding(path string, rows []Funding) error {
	if err := ValidateFunding(rows); err != nil {
		return err
	}
	return atomicFile(path, func(w io.Writer) error { return json.NewEncoder(w).Encode(rows) })
}

func LoadFunding(path string) ([]Funding, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	var rows []Funding
	d := json.NewDecoder(f)
	d.DisallowUnknownFields()
	if err := d.Decode(&rows); err != nil {
		return nil, err
	}
	var extra any
	if err := d.Decode(&extra); err != io.EOF {
		return nil, fmt.Errorf("unexpected trailing funding JSON")
	}
	if err := ValidateFunding(rows); err != nil {
		return nil, err
	}
	return rows, nil
}

func atomicFile(path string, write func(io.Writer) error) error {
	if err := os.MkdirAll(filepath.Dir(path), 0755); err != nil {
		return err
	}
	f, err := os.CreateTemp(filepath.Dir(path), ".market-*")
	if err != nil {
		return err
	}
	tmp := f.Name()
	defer os.Remove(tmp)
	if err := write(f); err != nil {
		f.Close()
		return err
	}
	if err := f.Sync(); err != nil {
		f.Close()
		return err
	}
	if err := f.Close(); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

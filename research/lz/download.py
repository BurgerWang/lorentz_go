"""Binance market data downloader and parquet generator."""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
import io
from pathlib import Path
import threading
import time
import zipfile
import numpy as np
import pandas as pd
import requests

from lz.config import ALL_ASSETS, BAR_MS, DATA_DIR

BASE_URL = "https://data.binance.vision/data/futures/um"
MAX_WORKERS, TIMEOUT, RETRIES = 8, 30, 3
_tls = threading.local()


def month_range(start_str: str, end_str: str) -> list[str]:
    dates = pd.date_range(pd.to_datetime(start_str, format="%Y-%m"), pd.to_datetime(end_str, format="%Y-%m"), freq="MS")
    return [d.strftime("%Y-%m") for d in dates]


def day_range(start_str: str, end_str: str) -> list[str]:
    return [d.strftime("%Y-%m-%d") for d in pd.date_range(pd.to_datetime(start_str), pd.to_datetime(end_str), freq="D")]


def get_session() -> requests.Session:
    if not hasattr(_tls, "session"):
        _tls.session = requests.Session()
    return _tls.session


def is_valid_zip(path: Path) -> bool:
    if not path.exists() or path.stat().st_size == 0:
        return False
    try:
        with zipfile.ZipFile(path, "r") as zf:
            return len(zf.namelist()) > 0
    except (zipfile.BadZipFile, OSError):
        return False


def download_file(url: str, dest: Path, kind: str, sym: str, period: str, missing: list) -> bool:
    if is_valid_zip(dest):
        return True
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp = dest.with_suffix(".tmp")
    sess = get_session()
    for attempt in range(RETRIES):
        try:
            r = sess.get(url, timeout=TIMEOUT)
            if r.status_code == 200:
                temp.write_bytes(r.content)
                if is_valid_zip(temp):
                    temp.replace(dest)
                    return True
                if temp.exists():
                    temp.unlink()
            elif r.status_code == 404:
                missing.append({"kind": kind, "sym": sym, "period": period})
                return False
        except (requests.RequestException, OSError):
            pass
        time.sleep(1.0 * (attempt + 1))
    if temp.exists():
        temp.unlink()
    return False


def iter_zip_csv_rows(zf_path: Path):
    if not is_valid_zip(zf_path):
        return
    try:
        with zipfile.ZipFile(zf_path, "r") as zf:
            for member in zf.namelist():
                if member.endswith(".csv"):
                    with zf.open(member) as f:
                        for row in csv.reader(io.TextIOWrapper(f, encoding="utf-8")):
                            if row:
                                yield row
    except Exception:
        pass


def process_klines(sym: str, raw_dir: Path, out_dir: Path) -> pd.DataFrame:
    rows = []
    for zf in sorted((raw_dir / "klines" / sym).glob(f"{sym}-4h-*.zip")):
        for r in iter_zip_csv_rows(zf):
            if r[0].isdigit():
                rows.append([int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]), float(r[7]), int(r[8]), float(r[9])])
    cols = ["open_time", "open", "high", "low", "close", "volume", "quote_volume", "count", "taker_buy_volume"]
    df = pd.DataFrame(rows, columns=cols).drop_duplicates(subset=["open_time"]).sort_values("open_time").reset_index(drop=True) if rows else pd.DataFrame(columns=cols)
    out_file = out_dir / "klines_4h" / f"{sym}.parquet"
    out_file.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out_file, index=False)
    return df


def process_funding(sym: str, raw_dir: Path, out_dir: Path) -> pd.DataFrame:
    rows = []
    for zf in sorted((raw_dir / "funding" / sym).glob(f"{sym}-fundingRate-*.zip")):
        for r in iter_zip_csv_rows(zf):
            if r[0].isdigit():
                ct = int(r[0])
                rows.append([ct, int(r[1]) if r[1].isdigit() else 8, float(r[2]), int(round(ct / 3.6e6) * 3.6e6)])
    cols = ["calc_time", "interval_h", "rate", "settle_hour"]
    df = pd.DataFrame(rows, columns=cols).drop_duplicates(subset=["calc_time"]).sort_values("calc_time").reset_index(drop=True) if rows else pd.DataFrame(columns=cols)
    out_file = out_dir / "funding" / f"{sym}.parquet"
    out_file.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out_file, index=False)
    return df


def _p_flt(v: str) -> float:
    try:
        return float(v) if v else np.nan
    except ValueError:
        return np.nan


def process_metrics(sym: str, raw_dir: Path, out_dir: Path) -> pd.DataFrame:
    rows = []
    for zf in sorted((raw_dir / "metrics" / sym).glob(f"{sym}-metrics-*.zip")):
        for r in iter_zip_csv_rows(zf):
            if len(r) >= 8 and r[0] != "create_time" and r[0].strip():
                try:
                    ts = int(pd.Timestamp(r[0].strip(), tz="UTC").timestamp() * 1000)
                    rows.append([ts, _p_flt(r[2]), _p_flt(r[3]), _p_flt(r[4]), _p_flt(r[5]), _p_flt(r[6]), _p_flt(r[7])])
                except Exception:
                    pass
    cols = ["ts", "oi", "oi_value", "top_ls_count", "top_ls_sum", "ls_count", "taker_ls_vol"]
    df = pd.DataFrame(rows, columns=cols).drop_duplicates(subset=["ts"]).sort_values("ts").reset_index(drop=True) if rows else pd.DataFrame(columns=cols)
    out_file = out_dir / "metrics_5m" / f"{sym}.parquet"
    out_file.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out_file, index=False)
    return df


def run_download(start_month: str = "2021-09", end_month: str = "2026-09"):
    data_dir, raw_dir = Path(DATA_DIR), Path(DATA_DIR) / "raw"
    data_dir.mkdir(parents=True, exist_ok=True)
    months = month_range(start_month, end_month)
    metrics_days = day_range("2021-12-01", (pd.to_datetime(end_month, format="%Y-%m") + pd.offsets.MonthEnd(1)).strftime("%Y-%m-%d"))

    missing, tasks = [], []
    for s in ALL_ASSETS:
        tasks.extend([(f"{BASE_URL}/monthly/klines/{s}/4h/{s}-4h-{ym}.zip", raw_dir / "klines" / s / f"{s}-4h-{ym}.zip", "klines", s, ym) for ym in months])
        tasks.extend([(f"{BASE_URL}/monthly/fundingRate/{s}/{s}-fundingRate-{ym}.zip", raw_dir / "funding" / s / f"{s}-fundingRate-{ym}.zip", "funding", s, ym) for ym in months])
        tasks.extend([(f"{BASE_URL}/daily/metrics/{s}/{s}-metrics-{d}.zip", raw_dir / "metrics" / s / f"{s}-metrics-{d}.zip", "metrics", s, d) for d in metrics_days])

    pending = [t for t in tasks if not is_valid_zip(t[1])]
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        list(pool.map(lambda t: download_file(t[0], t[1], t[2], t[3], t[4], missing), pending))

    if missing:
        pd.DataFrame(missing).drop_duplicates().to_csv(data_dir / "missing.csv", index=False)

    cov_rows = []
    for s in ALL_ASSETS:
        dk, df, dm = process_klines(s, raw_dir, data_dir), process_funding(s, raw_dir, data_dir), process_metrics(s, raw_dir, data_dir)
        k_gaps = int((dk["open_time"].diff().dropna() != BAR_MS).sum()) if len(dk) > 0 else 0
        cov_rows.append({
            "sym": s, "kline_rows": len(dk),
            "kline_start": pd.to_datetime(dk["open_time"].min(), unit="ms", utc=True).strftime("%Y-%m-%d %H:%M") if len(dk) > 0 else "",
            "kline_end": pd.to_datetime(dk["open_time"].max(), unit="ms", utc=True).strftime("%Y-%m-%d %H:%M") if len(dk) > 0 else "",
            "kline_gaps": k_gaps, "metrics_rows": len(dm),
            "metrics_start": pd.to_datetime(dm["ts"].min(), unit="ms", utc=True).strftime("%Y-%m-%d %H:%M") if len(dm) > 0 else "",
            "metrics_end": pd.to_datetime(dm["ts"].max(), unit="ms", utc=True).strftime("%Y-%m-%d %H:%M") if len(dm) > 0 else "",
            "metrics_missing_days": len(metrics_days) - len(set(pd.to_datetime(dm["ts"], unit="ms", utc=True).dt.strftime("%Y-%m-%d"))) if len(dm) > 0 else len(metrics_days),
            "funding_count": len(df),
            "funding_start": pd.to_datetime(df["calc_time"].min(), unit="ms", utc=True).strftime("%Y-%m-%d %H:%M") if len(df) > 0 else "",
            "funding_end": pd.to_datetime(df["calc_time"].max(), unit="ms", utc=True).strftime("%Y-%m-%d %H:%M") if len(df) > 0 else "",
        })
    pd.DataFrame(cov_rows).to_csv(data_dir / "coverage.csv", index=False)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--start", default="2021-09")
    p.add_argument("--end", default="2026-09")
    args = p.parse_args()
    run_download(start_month=args.start, end_month=args.end)


if __name__ == "__main__":
    main()

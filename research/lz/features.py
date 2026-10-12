"""Feature calculation and causal alignment for Lorentzian v2."""

from pathlib import Path
import numpy as np
import pandas as pd

from lz.config import (
    BAR_MS,
    DATA_DIR,
    DEV_ASSETS,
    FEATURE_NAMES,
    OUT_DIR,
    Z_CLIP,
    Z_MIN,
    Z_WIN,
)
from lz.labels import compute_labels
from lz.load import load_asset


def calc_zscore(
    series: pd.Series,
    win: int = Z_WIN,
    minp: int = Z_MIN,
    clip_val: float = Z_CLIP,
) -> pd.Series:
    """Compute strictly causal rolling z-score: clip((x - mean) / std, -clip_val, clip_val)."""
    roll_mean = series.rolling(win, min_periods=minp).mean()
    roll_std = series.rolling(win, min_periods=minp).std()

    z = pd.Series(np.nan, index=series.index, dtype="float64")
    valid = (roll_std > 0) & roll_std.notna() & series.notna()
    z[valid] = (series[valid] - roll_mean[valid]) / roll_std[valid]
    return z.clip(-clip_val, clip_val)


def compute_features_single(
    sym: str,
    klines: pd.DataFrame,
    funding: pd.DataFrame,
    metrics: pd.DataFrame,
    btc_klines: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Compute 11 features, rv30, and labels for a single asset with strict causal alignment."""
    df = klines.sort_values("open_time").drop_duplicates(subset=["open_time"]).reset_index(drop=True).copy()
    n = len(df)
    if n == 0:
        return pd.DataFrame(columns=["sym", "open_time", "close", "open"] + FEATURE_NAMES + ["rv30", "y"])

    t_open = df["open_time"]
    t_close = t_open + BAR_MS

    # 1. Metrics alignment: snapshot ts <= t_open + 3h55m
    m_asof_ts = t_open + (3 * 3600 + 55 * 60) * 1000
    if len(metrics) > 0:
        m_sorted = metrics.drop_duplicates(subset=["ts"], keep="last").sort_values("ts").reset_index(drop=True)
        m_query = pd.DataFrame({"asof_ts": m_asof_ts})
        m_merged = pd.merge_asof(m_query, m_sorted, left_on="asof_ts", right_on="ts", direction="backward")
    else:
        m_merged = pd.DataFrame(
            index=range(n),
            columns=["oi", "oi_value", "top_ls_count", "top_ls_sum", "ls_count", "taker_ls_vol"],
        )

    # 2. Funding alignment: settle_hour < t_close (i.e. settle_hour <= t_close - 1)
    f_asof_ts = t_close - 1
    if len(funding) > 0:
        f_sorted = funding.drop_duplicates(subset=["settle_hour"], keep="last").sort_values("settle_hour").reset_index(drop=True)
        f_query = pd.DataFrame({"asof_ts": f_asof_ts})
        f_merged = pd.merge_asof(f_query, f_sorted, left_on="asof_ts", right_on="settle_hour", direction="backward")
    else:
        f_merged = pd.DataFrame(index=range(n), columns=["rate", "settle_hour"])

    # 3. Daily trend alignment: UTC 20:00 bar close is daily close.
    # Day ends at next day 00:00 (day_end = open_time + 4h).
    # Only daily closes where day_end <= t_close are available.
    d_mask = df["open_time"] % 86400000 == 72000000
    d_bars = df[d_mask].copy()
    if len(d_bars) > 0:
        d_bars["day_end"] = d_bars["open_time"] + BAR_MS
        close_d = d_bars["close"]
        ema20_d = close_d.ewm(span=20, adjust=False).mean()
        d_bars["d1_trend_raw"] = np.log(close_d) - np.log(ema20_d)

        d_sorted = d_bars[["day_end", "d1_trend_raw"]].drop_duplicates(subset=["day_end"], keep="last").sort_values("day_end").reset_index(drop=True)
        d_query = pd.DataFrame({"t_close": t_close})
        d_merged = pd.merge_asof(d_query, d_sorted, left_on="t_close", right_on="day_end", direction="backward")
        raw_d1_trend = d_merged["d1_trend_raw"]
    else:
        raw_d1_trend = pd.Series(np.nan, index=df.index, dtype="float64")

    # 4. Raw indicators
    raw_funding_level = f_merged["rate"].astype("float64")
    raw_funding_chg_3d = raw_funding_level - raw_funding_level.shift(18)

    raw_oi = m_merged["oi"].astype("float64")
    # Avoid log of zero or negative
    raw_oi_safe = raw_oi.where(raw_oi > 0)
    raw_oi_chg_24h = np.log(raw_oi_safe / raw_oi_safe.shift(6))

    ret6 = np.log(df["close"] / df["close"].shift(6))
    raw_oi_price_div = calc_zscore(raw_oi_chg_24h) - calc_zscore(ret6)

    net_taker = 2.0 * df["taker_buy_volume"] - df["volume"]
    vol_sum6 = df["volume"].rolling(6).sum()
    raw_taker_imb = net_taker.rolling(6).sum() / vol_sum6.where(vol_sum6 > 0)

    top_ls_safe = m_merged["top_ls_sum"].astype("float64").where(m_merged["top_ls_sum"] > 0)
    raw_top_ls = np.log(top_ls_safe)

    ls_count_safe = m_merged["ls_count"].astype("float64").where(m_merged["ls_count"] > 0)
    raw_global_ls = np.log(ls_count_safe)

    logret_close = np.log(df["close"] / df["close"].shift(1))
    rolling_std42 = logret_close.rolling(42).std()
    raw_rv_7d = np.log(rolling_std42.where(rolling_std42 > 0))

    s6 = df["quote_volume"].rolling(6).sum()
    s6_mean180 = s6.rolling(180).mean()
    raw_vol_anom = np.log(s6 / s6_mean180.where(s6_mean180 > 0))

    if sym == "BTCUSDT":
        raw_rel_btc = pd.Series(0.0, index=df.index, dtype="float64")
    else:
        if btc_klines is not None and len(btc_klines) > 0:
            btc_df = btc_klines.sort_values("open_time").drop_duplicates(subset=["open_time"]).reset_index(drop=True)
            btc_ret6 = np.log(btc_df["close"] / btc_df["close"].shift(6))
            btc_map = pd.DataFrame({"open_time": btc_df["open_time"], "btc_ret6": btc_ret6})
            merged_btc = pd.merge(df[["open_time"]], btc_map, on="open_time", how="left")
            raw_rel_btc = ret6 - merged_btc["btc_ret6"]
        else:
            raw_rel_btc = pd.Series(np.nan, index=df.index, dtype="float64")

    # 5. Final z-scores
    df_out = pd.DataFrame()
    df_out["sym"] = [sym] * n
    df_out["open_time"] = df["open_time"]
    df_out["close"] = df["close"]
    df_out["open"] = df["open"]

    df_out["funding_level"] = calc_zscore(raw_funding_level)
    df_out["funding_chg_3d"] = calc_zscore(raw_funding_chg_3d)
    df_out["oi_chg_24h"] = calc_zscore(raw_oi_chg_24h)
    df_out["oi_price_div"] = calc_zscore(raw_oi_price_div)
    df_out["taker_imb"] = calc_zscore(raw_taker_imb)
    df_out["top_ls"] = calc_zscore(raw_top_ls)
    df_out["global_ls"] = calc_zscore(raw_global_ls)
    df_out["rv_7d"] = calc_zscore(raw_rv_7d)
    df_out["vol_anom"] = calc_zscore(raw_vol_anom)
    df_out["d1_trend"] = calc_zscore(raw_d1_trend)

    if sym == "BTCUSDT":
        df_out["rel_btc"] = 0.0
    else:
        df_out["rel_btc"] = calc_zscore(raw_rel_btc)

    # 6. rv30 (annualized volatility over 180 bars = 30 days)
    df_out["rv30"] = logret_close.rolling(180).std() * np.sqrt(2190)

    # 7. Labels
    df_out["y"] = compute_labels(df, sym)

    return df_out


def build_features(split: str = "dev") -> pd.DataFrame:
    """Build and save features dataframe for the given split."""
    if split == "dev":
        assets = DEV_ASSETS
    else:
        assets = DEV_ASSETS + ["SOLUSDT"]

    # Load BTC first for rel_btc
    btc_data = load_asset("BTCUSDT", split=split)
    btc_klines = btc_data["klines"]

    dfs = []
    for sym in assets:
        data = load_asset(sym, split=split)
        feat_df = compute_features_single(
            sym=sym,
            klines=data["klines"],
            funding=data["funding"],
            metrics=data["metrics"],
            btc_klines=btc_klines,
        )
        dfs.append(feat_df)

    all_features = pd.concat(dfs, ignore_index=True)
    out_dir = Path(OUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"features_{split}.parquet"
    all_features.to_parquet(out_file, index=False)
    print(f"Saved {len(all_features)} feature rows to {out_file}")
    return all_features

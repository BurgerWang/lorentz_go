"""Causal indicators for Lorentzian v3: EMA, Donchian, ATR, Funding 3d, and rv30."""

import numpy as np
import pandas as pd


def calc_ema(src: np.ndarray | pd.Series, length: int) -> np.ndarray:
    """Calculate EMA matching TradingView ta.ema semantics.
    
    - First `length` finite values use SMA seed; prior outputs are NaN.
    - Subsequent finite values update recursively with alpha = 2 / (length + 1).
    - Non-finite inputs skip state update and output current state.
    """
    arr = np.asarray(src, dtype=np.float64)
    n = len(arr)
    out = np.full(n, np.nan, dtype=np.float64)
    if length <= 0 or n == 0:
        return out

    alpha = 2.0 / (float(length) + 1.0)
    state = np.nan
    s = 0.0
    count = 0

    for i in range(n):
        x = arr[i]
        if np.isfinite(x):
            if count < length:
                s += x
                count += 1
                if count == length:
                    state = s / float(length)
            else:
                state = alpha * x + (1.0 - alpha) * state
        out[i] = state
    return out


def calc_rma(src: np.ndarray | pd.Series, length: int) -> np.ndarray:
    """Calculate RMA (Wilder's moving average) matching TradingView ta.rma semantics.
    
    - First `length` finite values use SMA seed; prior outputs are NaN.
    - Subsequent finite values update recursively with alpha = 1 / length.
    - Non-finite inputs skip state update and output current state.
    """
    arr = np.asarray(src, dtype=np.float64)
    n = len(arr)
    out = np.full(n, np.nan, dtype=np.float64)
    if length <= 0 or n == 0:
        return out

    alpha = 1.0 / float(length)
    state = np.nan
    s = 0.0
    count = 0

    for i in range(n):
        x = arr[i]
        if np.isfinite(x):
            if count < length:
                s += x
                count += 1
                if count == length:
                    state = s / float(length)
            else:
                state = alpha * x + (1.0 - alpha) * state
        out[i] = state
    return out


def calc_donchian(
    high: np.ndarray | pd.Series,
    low: np.ndarray | pd.Series,
    n_breakout: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Calculate Donchian Channel breakout levels with strict causal guarantee.
    
    Breakout_high(t) = max(high_{t-N ... t-1})
    Breakout_low(t) = min(low_{t-N ... t-1})
    Current bar t is strictly excluded.
    """
    h_s = pd.Series(high, dtype=np.float64)
    l_s = pd.Series(low, dtype=np.float64)
    b_high = h_s.shift(1).rolling(n_breakout).max().to_numpy(dtype=np.float64)
    b_low = l_s.shift(1).rolling(n_breakout).min().to_numpy(dtype=np.float64)
    return b_high, b_low


def calc_tr(
    high: np.ndarray | pd.Series,
    low: np.ndarray | pd.Series,
    close: np.ndarray | pd.Series,
) -> np.ndarray:
    """Calculate True Range matching TradingView ta.tr."""
    h = np.asarray(high, dtype=np.float64)
    l = np.asarray(low, dtype=np.float64)
    c = np.asarray(close, dtype=np.float64)
    n = len(h)
    tr = np.zeros(n, dtype=np.float64)
    if n == 0:
        return tr
    tr[0] = h[0] - l[0]
    for i in range(1, n):
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
    return tr


def calc_atr(
    high: np.ndarray | pd.Series,
    low: np.ndarray | pd.Series,
    close: np.ndarray | pd.Series,
    length: int = 14,
) -> np.ndarray:
    """Calculate Average True Range using RMA(TR, length)."""
    tr = calc_tr(high, low, close)
    return calc_rma(tr, length)


def calc_funding_3d(
    klines: pd.DataFrame,
    funding: pd.DataFrame | None,
    min_periods: int = 1,
) -> np.ndarray:
    """Calculate 3-day (18 4h-bars) rolling mean of settled funding rates causally.
    
    For each bar t, only settlements strictly before open_time_t (settle_hour < open_time_t)
    are observed.
    """
    n = len(klines)
    if n == 0:
        return np.zeros(0, dtype=np.float64)
    if funding is None or funding.empty or "settle_hour" not in funding.columns or "rate" not in funding.columns:
        return np.zeros(n, dtype=np.float64)

    f_df = funding
    if "sym" in funding.columns and "sym" in klines.columns and len(klines) > 0:
        target_sym = klines["sym"].iloc[0]
        f_df = funding[funding["sym"] == target_sym]
        if f_df.empty:
            return np.zeros(n, dtype=np.float64)

    f_sorted = f_df.drop_duplicates(subset=["settle_hour"], keep="last").sort_values("settle_hour").reset_index(drop=True)
    f_query = pd.DataFrame({"asof_ts": klines["open_time"].astype(np.int64) - 1})
    merged = pd.merge_asof(f_query, f_sorted, left_on="asof_ts", right_on="settle_hour", direction="backward")
    rate_series = merged["rate"].fillna(0.0).astype(np.float64)
    return rate_series.rolling(18, min_periods=min_periods).mean().to_numpy(dtype=np.float64)


def calc_rv30(close: np.ndarray | pd.Series, min_periods: int = 30) -> np.ndarray:
    """Calculate annualized 30-day realized volatility: std(logret, 180) * sqrt(2190)."""
    c_s = pd.Series(close, dtype=np.float64)
    logret = np.log(c_s / c_s.shift(1))
    rv = logret.rolling(180, min_periods=min_periods).std() * np.sqrt(2190.0)
    return rv.to_numpy(dtype=np.float64)


def build_indicators_df(
    klines: pd.DataFrame,
    funding: pd.DataFrame | None,
    l_fast: int,
    l_slow: int,
    n_breakout: int,
    atr_length: int = 14,
    sym: str | None = None,
) -> pd.DataFrame:
    """Compute and attach all v3 indicators to a klines DataFrame."""
    df = klines.copy().reset_index(drop=True)
    if sym is not None and "sym" not in df.columns:
        df["sym"] = sym

    c = df["close"].to_numpy(dtype=np.float64)
    h = df["high"].to_numpy(dtype=np.float64)
    l = df["low"].to_numpy(dtype=np.float64)

    df["ema_fast"] = calc_ema(c, l_fast)
    df["ema_slow"] = calc_ema(c, l_slow)
    b_high, b_low = calc_donchian(h, l, n_breakout)
    df["breakout_high"] = b_high
    df["breakout_low"] = b_low
    df["atr"] = calc_atr(h, l, c, atr_length)
    df["funding_3d"] = calc_funding_3d(df, funding)
    df["rv30"] = calc_rv30(c)

    return df


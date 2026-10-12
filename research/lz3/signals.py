"""Dual-speed trend breakout signals and funding rate Carry modifiers for Lorentzian v3."""

import numpy as np
import pandas as pd


def step_signal(
    close_t: float,
    breakout_high: float,
    breakout_low: float,
    ema_fast: float,
    ema_slow: float,
    pos_current: int,
    f_3d: float = 0.0,
    carry_mode: str = "Off",
) -> tuple[int, float]:
    """Calculate decision signal S_t in {+1, -1, 0} and carry multiplier at bar t close."""
    if np.isfinite(breakout_high) and np.isfinite(ema_fast) and np.isfinite(ema_slow) and close_t > breakout_high and ema_fast > ema_slow:
        trend_raw = 1
    elif np.isfinite(breakout_low) and np.isfinite(ema_fast) and np.isfinite(ema_slow) and close_t < breakout_low and ema_fast < ema_slow:
        trend_raw = -1
    elif pos_current == 1 and np.isfinite(ema_slow) and close_t < ema_slow:
        trend_raw = 0
    elif pos_current == -1 and np.isfinite(ema_slow) and close_t > ema_slow:
        trend_raw = 0
    else:
        trend_raw = pos_current

    if carry_mode == "On":
        if f_3d > 0.0003:
            signal, mult = (0, 0.0) if trend_raw == 1 else ((-1, 1.3) if trend_raw == -1 else (0, 0.0))
        elif f_3d < -0.0002:
            signal, mult = (0, 0.0) if trend_raw == -1 else ((1, 1.2) if trend_raw == 1 else (0, 0.0))
        else:
            signal, mult = trend_raw, 1.0
    else:
        signal, mult = trend_raw, 1.0

    return signal, mult


def generate_channel_signals_series(
    df: pd.DataFrame,
    carry_mode: str = "Off",
) -> tuple[np.ndarray, np.ndarray]:
    """Generate sequential signals and multipliers without trailing stop overrides."""
    n = len(df)
    signals = np.zeros(n, dtype=np.int32)
    mults = np.zeros(n, dtype=np.float64)

    c = df["close"].to_numpy(dtype=np.float64)
    bh = df["breakout_high"].to_numpy(dtype=np.float64)
    bl = df["breakout_low"].to_numpy(dtype=np.float64)
    ef = df["ema_fast"].to_numpy(dtype=np.float64)
    es = df["ema_slow"].to_numpy(dtype=np.float64)
    f3d = df["funding_3d"].to_numpy(dtype=np.float64) if "funding_3d" in df.columns else np.zeros(n, dtype=np.float64)

    curr_pos = 0
    for t in range(n):
        sig, mult = step_signal(
            close_t=c[t], breakout_high=bh[t], breakout_low=bl[t],
            ema_fast=ef[t], ema_slow=es[t], pos_current=curr_pos,
            f_3d=f3d[t], carry_mode=carry_mode,
        )
        signals[t] = sig
        mults[t] = mult
        curr_pos = sig

    return signals, mults

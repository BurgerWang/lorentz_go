"""Dual-speed trend breakout signals, ADX chop filter, and Carry modifiers for Lorentzian v4."""

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
    carry_mode: str = "On",
    chop_filter: str = "None",
    adx_t: float = np.nan,
    plus_di_t: float = np.nan,
    minus_di_t: float = np.nan,
) -> tuple[int, float]:
    """Calculate decision signal S_t in {+1, -1, 0} and carry multiplier at bar t close.

    1. Base Donchian channel breakout confirmed by EMA fast/slow band.
    2. ADX macro chop filter: if opening new position (pos_current == 0), require
       ADX >= 22.0 and directional dominance (+DI > -DI for long, -DI > +DI for short).
    3. Channel exit condition: if currently holding, exit when close crosses EMA_slow.
    4. Carry overlay: adjust or inhibit based on extreme funding crowding.
    """
    # 1. Base channel breakout & EMA trend confirmation
    if np.isfinite(breakout_high) and np.isfinite(ema_fast) and np.isfinite(ema_slow) and close_t > breakout_high and ema_fast > ema_slow:
        raw_dir = 1
    elif np.isfinite(breakout_low) and np.isfinite(ema_fast) and np.isfinite(ema_slow) and close_t < breakout_low and ema_fast < ema_slow:
        raw_dir = -1
    else:
        raw_dir = pos_current

    # 2. Macro Chop Filter (ADX_22) for new entries
    if chop_filter == "ADX_22" and pos_current == 0 and raw_dir != 0:
        if not (np.isfinite(adx_t) and adx_t >= 22.0):
            raw_dir = 0
        elif raw_dir == 1 and not (np.isfinite(plus_di_t) and np.isfinite(minus_di_t) and plus_di_t > minus_di_t):
            raw_dir = 0
        elif raw_dir == -1 and not (np.isfinite(plus_di_t) and np.isfinite(minus_di_t) and minus_di_t > plus_di_t):
            raw_dir = 0

    # 3. Channel exit condition (held position strictly exits when breaking EMA_slow)
    if pos_current == 1 and np.isfinite(ema_slow) and close_t < ema_slow:
        raw_dir = 0
    elif pos_current == -1 and np.isfinite(ema_slow) and close_t > ema_slow:
        raw_dir = 0

    # 4. Carry overlay adjustment
    if carry_mode == "On":
        if f_3d > 0.0003:  # Longs extremely crowded
            signal, mult = (0, 0.0) if raw_dir == 1 else ((-1, 1.3) if raw_dir == -1 else (0, 0.0))
        elif f_3d < -0.0002:  # Shorts extremely crowded
            signal, mult = (0, 0.0) if raw_dir == -1 else ((1, 1.2) if raw_dir == 1 else (0, 0.0))
        else:
            signal, mult = raw_dir, 1.0
    else:
        signal, mult = raw_dir, 1.0

    return signal, mult


def generate_channel_signals_series(
    df: pd.DataFrame,
    carry_mode: str = "On",
    chop_filter: str = "None",
) -> tuple[np.ndarray, np.ndarray]:
    """Generate sequential signals and multipliers for a precomputed features DataFrame."""
    n = len(df)
    signals = np.zeros(n, dtype=np.int32)
    mults = np.zeros(n, dtype=np.float64)

    c = df["close"].to_numpy(dtype=np.float64)
    bh = df["breakout_high"].to_numpy(dtype=np.float64)
    bl = df["breakout_low"].to_numpy(dtype=np.float64)
    ef = df["ema_fast"].to_numpy(dtype=np.float64)
    es = df["ema_slow"].to_numpy(dtype=np.float64)
    f3d = df["funding_3d"].to_numpy(dtype=np.float64) if "funding_3d" in df.columns else np.zeros(n, dtype=np.float64)
    adx_arr = df["adx"].to_numpy(dtype=np.float64) if "adx" in df.columns else np.full(n, np.nan, dtype=np.float64)
    pdi_arr = df["plus_di"].to_numpy(dtype=np.float64) if "plus_di" in df.columns else np.full(n, np.nan, dtype=np.float64)
    mdi_arr = df["minus_di"].to_numpy(dtype=np.float64) if "minus_di" in df.columns else np.full(n, np.nan, dtype=np.float64)

    curr_pos = 0
    for t in range(n):
        sig, mult = step_signal(
            close_t=c[t],
            breakout_high=bh[t],
            breakout_low=bl[t],
            ema_fast=ef[t],
            ema_slow=es[t],
            pos_current=curr_pos,
            f_3d=f3d[t],
            carry_mode=carry_mode,
            chop_filter=chop_filter,
            adx_t=adx_arr[t],
            plus_di_t=pdi_arr[t],
            minus_di_t=mdi_arr[t],
        )
        signals[t] = sig
        mults[t] = mult
        curr_pos = sig

    return signals, mults

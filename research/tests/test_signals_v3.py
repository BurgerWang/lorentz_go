"""Unit tests for v3 indicators, causality, signals, and carry modifiers."""

import numpy as np
import pandas as pd
import pytest

from lz3.config import (
    DEV_ASSETS,
    HOLDOUT_ASSET,
    load_asset_v3,
    GRID_CONFIGS,
    get_dev_cutoff_ms,
)
from lz3.indicators import (
    calc_ema,
    calc_rma,
    calc_donchian,
    calc_tr,
    calc_atr,
    calc_funding_3d,
    calc_rv30,
    build_indicators_df,
)
from lz3.signals import step_signal, generate_channel_signals_series


# ---------------------------------------------------------------------------
# 1. EMA Tests: TradingView ta.ema & SMA Seed Equivalence
# ---------------------------------------------------------------------------

def test_ema_sma_seed_and_nan_skipping():
    """Verify ta.ema exact parity with Go reference test:
    Input: [NaN, 2, 4, NaN, 6, 8], length=3.
    Seed over first 3 finite values: (2 + 4 + 6) / 3 = 4.0 at index 4.
    Next value 8: alpha = 2 / (3 + 1) = 0.5.
    state = 0.5 * 8 + 0.5 * 4 = 6.0 at index 5.
    Prior values are NaN.
    """
    src = np.array([np.nan, 2.0, 4.0, np.nan, 6.0, 8.0])
    res = calc_ema(src, length=3)
    expected = np.array([np.nan, np.nan, np.nan, np.nan, 4.0, 6.0])
    assert np.allclose(res, expected, equal_nan=True)


def test_ema_length_one_is_identity():
    """ta.ema with length=1 should equal the original finite values."""
    src = np.array([1.0, 2.5, np.nan, 4.0, 5.5])
    res = calc_ema(src, length=1)
    expected = np.array([1.0, 2.5, 2.5, 4.0, 5.5])  # NaN inherits previous state
    assert np.allclose(res, expected, equal_nan=True)


def test_ema_hand_calculation():
    """Hand-calculated EMA for length=2 on [10.0, 20.0, 30.0].
    Index 0: NaN.
    Index 1 (seed): (10 + 20) / 2 = 15.0.
    Index 2: alpha = 2 / (2 + 1) = 2/3.
             state = (2/3) * 30 + (1/3) * 15 = 20 + 5 = 25.0.
    """
    src = np.array([10.0, 20.0, 30.0])
    res = calc_ema(src, length=2)
    expected = np.array([np.nan, 15.0, 25.0])
    assert np.allclose(res, expected, equal_nan=True)


# ---------------------------------------------------------------------------
# 2. Donchian Channel Tests: Hand Calculation & Strict Causality
# ---------------------------------------------------------------------------

def test_donchian_hand_calculation():
    """Verify Donchian channel strictly uses previous N bars (excluding bar t).
    high = [10, 15, 12, 18, 14, 20], low = [8, 11, 9, 13, 10, 16], N=3.
    t=0,1,2: NaN.
    t=3: max(high[0:3]) = 15, min(low[0:3]) = 8. (Note high[3]=18 is strictly excluded!)
    t=4: max(high[1:4]) = max(15, 12, 18) = 18, min(low[1:4]) = min(11, 9, 13) = 9.
    t=5: max(high[2:5]) = max(12, 18, 14) = 18, min(low[2:5]) = min(9, 13, 10) = 9.
    """
    high = np.array([10.0, 15.0, 12.0, 18.0, 14.0, 20.0])
    low = np.array([8.0, 11.0, 9.0, 13.0, 10.0, 16.0])
    b_high, b_low = calc_donchian(high, low, n_breakout=3)

    expected_high = np.array([np.nan, np.nan, np.nan, 15.0, 18.0, 18.0])
    expected_low = np.array([np.nan, np.nan, np.nan, 8.0, 9.0, 9.0])

    assert np.allclose(b_high, expected_high, equal_nan=True)
    assert np.allclose(b_low, expected_low, equal_nan=True)


def test_donchian_causality_future_perturbation():
    """Perturbing prices strictly after timestamp T must leave breakout levels at t <= T invariant."""
    rng = np.random.default_rng(42)
    n = 100
    high = 100.0 + np.cumsum(rng.standard_normal(n)) + np.abs(rng.standard_normal(n))
    low = high - np.abs(rng.standard_normal(n)) - 1.0

    b_high_orig, b_low_orig = calc_donchian(high, low, n_breakout=18)

    # Perturb strictly after index 50
    t_cutoff = 50
    high_pert = high.copy()
    low_pert = low.copy()
    high_pert[t_cutoff + 1 :] *= 5.0
    low_pert[t_cutoff + 1 :] *= 0.2

    b_high_pert, b_low_pert = calc_donchian(high_pert, low_pert, n_breakout=18)

    assert np.allclose(b_high_orig[: t_cutoff + 1], b_high_pert[: t_cutoff + 1], equal_nan=True)
    assert np.allclose(b_low_orig[: t_cutoff + 1], b_low_pert[: t_cutoff + 1], equal_nan=True)


# ---------------------------------------------------------------------------
# 3. TR & ATR Tests: Hand Calculation & RMA Parity
# ---------------------------------------------------------------------------

def test_tr_hand_calculation():
    """TR_0 = high_0 - low_0.
    For t >= 1: max(high - low, |high - close_prev|, |low - close_prev|).
    """
    high = np.array([10.0, 12.0, 15.0])
    low = np.array([8.0, 11.0, 9.0])
    close = np.array([9.0, 11.5, 14.0])

    # t=0: 10 - 8 = 2.0
    # t=1: max(12 - 11 = 1, |12 - 9| = 3, |11 - 9| = 2) = 3.0 (gap up)
    # t=2: max(15 - 9 = 6, |15 - 11.5| = 3.5, |9 - 11.5| = 2.5) = 6.0
    tr = calc_tr(high, low, close)
    assert np.allclose(tr, [2.0, 3.0, 6.0])


def test_rma_and_atr_hand_calculation():
    """RMA for length=3 on [10.0, 10.0, 10.0, 20.0].
    Index 0, 1: NaN.
    Index 2 (seed): (10 + 10 + 10) / 3 = 10.0.
    Index 3: alpha = 1 / 3. state = (1/3) * 20 + (2/3) * 10 = 20/3 + 20/3 = 40/3 = 13.3333333333.
    """
    src = np.array([10.0, 10.0, 10.0, 20.0])
    res = calc_rma(src, length=3)
    expected = np.array([np.nan, np.nan, 10.0, 40.0 / 3.0])
    assert np.allclose(res, expected, equal_nan=True)


def test_atr_causality_future_perturbation():
    """Perturbing prices strictly after timestamp T must leave ATR at t <= T invariant."""
    rng = np.random.default_rng(123)
    n = 100
    close = 100.0 + np.cumsum(rng.standard_normal(n))
    high = close + np.abs(rng.standard_normal(n))
    low = close - np.abs(rng.standard_normal(n))

    atr_orig = calc_atr(high, low, close, length=14)

    t_cutoff = 60
    high_pert, low_pert, close_pert = high.copy(), low.copy(), close.copy()
    high_pert[t_cutoff + 1 :] *= 2.5
    low_pert[t_cutoff + 1 :] *= 0.5
    close_pert[t_cutoff + 1 :] *= 1.8

    atr_pert = calc_atr(high_pert, low_pert, close_pert, length=14)

    assert np.allclose(atr_orig[: t_cutoff + 1], atr_pert[: t_cutoff + 1], equal_nan=True)


# ---------------------------------------------------------------------------
# 4. Funding 3d Rolling Mean & Causal Barrier
# ---------------------------------------------------------------------------

def test_funding_3d_causal_barrier():
    """Funding settlement occurring at settle_hour T must NOT be visible at bar with open_time = T.
    It becomes visible at subsequent bar with open_time > T.
    """
    bar_ms = 4 * 3600 * 1000
    t0 = 1672531200000  # 00:00 UTC
    open_times = [t0 + i * bar_ms for i in range(25)]
    klines = pd.DataFrame({"open_time": open_times})

    # Settlements at 00:00 (t0), 08:00 (t0 + 2*bar_ms), 16:00 (t0 + 4*bar_ms)
    funding_data = [
        {"calc_time": t0, "settle_hour": t0, "rate": 0.0001},
        {"calc_time": t0 + 2 * bar_ms, "settle_hour": t0 + 2 * bar_ms, "rate": 0.0005},
    ]
    funding_df = pd.DataFrame(funding_data)

    f3d = calc_funding_3d(klines, funding_df, min_periods=1)

    # Bar 0 (open_time = t0): settle_hour < t0 has no settlement -> 0.0
    assert np.isclose(f3d[0], 0.0)
    # Bar 1 (open_time = t0 + 1*bar_ms, 04:00): settle_hour < 04:00 finds t0 (0.0001)
    # Bar 2 (open_time = t0 + 2*bar_ms, 08:00): settle_hour < 08:00 strictly finds t0 (0.0001), NOT 08:00 settlement!
    # Bar 3 (open_time = t0 + 3*bar_ms, 12:00): settle_hour < 12:00 finds 08:00 settlement (0.0005)
    # Let's verify rate series behind rolling:
    # bar 0: 0.0
    # bar 1: 0.0001
    # bar 2: 0.0001
    # bar 3: 0.0005
    assert np.isclose(f3d[1], (0.0 + 0.0001) / 2.0)
    assert np.isclose(f3d[2], (0.0 + 0.0001 + 0.0001) / 3.0)
    assert np.isclose(f3d[3], (0.0 + 0.0001 + 0.0001 + 0.0005) / 4.0)


# ---------------------------------------------------------------------------
# 5. Signal State Machine & Carry Modifier Logic
# ---------------------------------------------------------------------------

def test_step_signal_trend_breakout_and_channel_exit():
    """Test trend breakout entry, holding, and slow EMA channel exit."""
    # 1. Bullish breakout: close > Breakout_high and EMA_fast > EMA_slow
    sig, mult = step_signal(
        close_t=105.0,
        breakout_high=100.0,
        breakout_low=80.0,
        ema_fast=95.0,
        ema_slow=90.0,
        pos_current=0,
        f_3d=0.0,
        carry_mode="Off",
    )
    assert sig == 1 and mult == 1.0

    # 2. Holding in trend: close between slow EMA and breakout high
    sig, mult = step_signal(
        close_t=98.0,
        breakout_high=102.0,
        breakout_low=80.0,
        ema_fast=95.0,
        ema_slow=90.0,
        pos_current=1,
        f_3d=0.0,
        carry_mode="Off",
    )
    assert sig == 1 and mult == 1.0  # Maintained

    # 3. Channel exit: pos == +1 and close < EMA_slow
    sig, mult = step_signal(
        close_t=88.0,
        breakout_high=102.0,
        breakout_low=80.0,
        ema_fast=92.0,
        ema_slow=90.0,
        pos_current=1,
        f_3d=0.0,
        carry_mode="Off",
    )
    assert sig == 0 and mult == 1.0  # Exited channel


def test_carry_modifier_extreme_crowding_rules():
    """Test Carry rules when carry_mode == 'On':
    - F_3d > 0.0003: Long forbidden (signal=0, mult=0.0); Short gets 1.3x boost (signal=-1, mult=1.3).
    - F_3d < -0.0002: Short forbidden (signal=0, mult=0.0); Long gets 1.2x boost (signal=1, mult=1.2).
    - Neutral: mult=1.0.
    """
    # 1. High funding: Long breakout attempted -> forbidden!
    sig, mult = step_signal(
        close_t=105.0,
        breakout_high=100.0,
        breakout_low=80.0,
        ema_fast=95.0,
        ema_slow=90.0,
        pos_current=0,
        f_3d=0.0004,
        carry_mode="On",
    )
    assert sig == 0 and mult == 0.0

    # 2. High funding: Short breakout attempted -> boosted 1.3x!
    sig, mult = step_signal(
        close_t=75.0,
        breakout_high=100.0,
        breakout_low=80.0,
        ema_fast=85.0,
        ema_slow=90.0,
        pos_current=0,
        f_3d=0.0004,
        carry_mode="On",
    )
    assert sig == -1 and mult == 1.3

    # 3. Negative funding: Short breakout attempted -> forbidden!
    sig, mult = step_signal(
        close_t=75.0,
        breakout_high=100.0,
        breakout_low=80.0,
        ema_fast=85.0,
        ema_slow=90.0,
        pos_current=0,
        f_3d=-0.0003,
        carry_mode="On",
    )
    assert sig == 0 and mult == 0.0

    # 4. Negative funding: Long breakout attempted -> boosted 1.2x!
    sig, mult = step_signal(
        close_t=105.0,
        breakout_high=100.0,
        breakout_low=80.0,
        ema_fast=95.0,
        ema_slow=90.0,
        pos_current=0,
        f_3d=-0.0003,
        carry_mode="On",
    )
    assert sig == 1 and mult == 1.2


# ---------------------------------------------------------------------------
# 6. Data Isolation & Cutoff Guards (R4 & R5)
# ---------------------------------------------------------------------------

def test_sol_isolation_guard():
    """Verify SOLUSDT cannot be loaded under split='dev'."""
    with pytest.raises(PermissionError, match="strictly forbidden in dev split"):
        load_asset_v3(HOLDOUT_ASSET, split="dev")


@pytest.mark.needs_market_data
def test_dev_data_physical_cutoff():
    """Verify dev split klines and funding are strictly truncated before 2025-01-01."""
    cutoff_ms = get_dev_cutoff_ms()
    data = load_asset_v3("BTCUSDT", split="dev")
    klines = data["klines"]
    funding = data["funding"]

    assert len(klines) > 0
    assert (klines["open_time"] < cutoff_ms).all(), "Klines leak into holdout period!"
    assert (funding["calc_time"] < cutoff_ms).all(), "Funding leaks into holdout period!"


def test_grid_configs_count():
    """Verify exactly 16 configurations are registered in config.py."""
    assert len(GRID_CONFIGS) == 16


def test_funding_3d_multi_symbol_isolation():
    """Verify multi-symbol funding table does not cross-contaminate symbol funding rates."""
    bar_ms = 4 * 3600 * 1000
    t0 = 1672531200000
    klines_btc = pd.DataFrame({"open_time": [t0 + i * bar_ms for i in range(5)], "sym": ["BTCUSDT"] * 5})
    
    # Funding table containing both BTC and ETH
    funding_data = [
        {"sym": "BTCUSDT", "calc_time": t0, "settle_hour": t0, "rate": 0.0001},
        {"sym": "ETHUSDT", "calc_time": t0, "settle_hour": t0, "rate": 0.0008},
    ]
    funding_df = pd.DataFrame(funding_data)

    f3d = calc_funding_3d(klines_btc, funding_df, min_periods=1)
    # At bar 1, settle_hour < open_time_1 finds BTC settlement (0.0001), not ETH (0.0008)!
    assert np.isclose(f3d[1], (0.0 + 0.0001) / 2.0)


def test_build_indicators_df_with_sym():
    """Verify build_indicators_df attaches sym and computes all indicators."""
    bar_ms = 4 * 3600 * 1000
    t0 = 1672531200000
    n = 30
    klines = pd.DataFrame({
        "open_time": [t0 + i * bar_ms for i in range(n)],
        "open": [100.0 + i for i in range(n)],
        "high": [105.0 + i for i in range(n)],
        "low": [95.0 + i for i in range(n)],
        "close": [102.0 + i for i in range(n)],
    })
    df = build_indicators_df(klines, funding=None, l_fast=12, l_slow=24, n_breakout=18, sym="BTCUSDT")
    assert df["sym"].iloc[0] == "BTCUSDT"
    assert "ema_fast" in df.columns
    assert "breakout_high" in df.columns
    assert "funding_3d" in df.columns
    assert "rv30" in df.columns


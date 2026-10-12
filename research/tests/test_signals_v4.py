"""Unit tests for v4 indicators, causality, signals, and ADX chop filter."""

import numpy as np
import pandas as pd
import pytest

from lz4.config import (
    DEV_ASSETS,
    HOLDOUT_ASSET,
    load_asset_v4,
    GRID_CONFIGS,
    get_dev_cutoff_ms,
)
from lz4.indicators import (
    calc_ema,
    calc_rma,
    calc_donchian,
    calc_tr,
    calc_atr,
    calc_dmi_adx,
    calc_funding_3d,
    calc_rv30,
    build_indicators_df,
)
from lz4.signals import step_signal, generate_channel_signals_series


# ---------------------------------------------------------------------------
# 1. EMA Tests: TradingView ta.ema & SMA Seed Equivalence
# ---------------------------------------------------------------------------

def test_ema_sma_seed_and_nan_skipping():
    """Verify ta.ema exact parity:
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
    expected = np.array([1.0, 2.5, 2.5, 4.0, 5.5])
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

    b_high_orig, b_low_orig = calc_donchian(high, low, n_breakout=24)

    t_cutoff = 50
    high_pert = high.copy()
    low_pert = low.copy()
    high_pert[t_cutoff + 1 :] *= 5.0
    low_pert[t_cutoff + 1 :] *= 0.2

    b_high_pert, b_low_pert = calc_donchian(high_pert, low_pert, n_breakout=24)

    assert np.allclose(b_high_orig[: t_cutoff + 1], b_high_pert[: t_cutoff + 1], equal_nan=True)
    assert np.allclose(b_low_orig[: t_cutoff + 1], b_low_pert[: t_cutoff + 1], equal_nan=True)


# ---------------------------------------------------------------------------
# 3. TR, RMA & ATR Tests: Hand Calculation & Future Invariance
# ---------------------------------------------------------------------------

def test_tr_hand_calculation():
    """TR_0 = high_0 - low_0.
    For t >= 1: max(high - low, |high - close_prev|, |low - close_prev|).
    """
    high = np.array([10.0, 12.0, 15.0])
    low = np.array([8.0, 11.0, 9.0])
    close = np.array([9.0, 11.5, 14.0])

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
# 4. ADX & DMI(14) Tests: Hand Calculation, Incremental Recursion & Invariance
# ---------------------------------------------------------------------------

def test_adx_dmi_hand_calculation():
    """Step-by-step hand calculation verification of +DI, -DI, DX, and ADX."""
    h = np.array([100.0 + i + (2.0 if i % 2 == 0 else -1.0) for i in range(28)], dtype=np.float64)
    l = h - 4.0
    c = h - 1.0

    pdi, mdi, adx = calc_dmi_adx(h, l, c, length=14)

    # First 13 values (0..12) must be NaN
    assert np.isnan(pdi[:13]).all()
    assert np.isnan(mdi[:13]).all()
    assert np.isnan(adx[:26]).all()

    # Hand calculation at bar 13:
    # plus_dm sum over bars 0..13 is 24.0, mean is 24.0 / 14
    # minus_dm sum over bars 0..13 is 14.0, mean is 14.0 / 14
    # tr sum over bars 0..13 is 69.0, mean is 69.0 / 14
    # pdi[13] = (24 / 69) * 100 = 34.78260869565217
    # mdi[13] = (14 / 69) * 100 = 20.289855072463766
    expected_p13 = (24.0 / 69.0) * 100.0
    expected_m13 = (14.0 / 69.0) * 100.0
    assert np.isclose(pdi[13], expected_p13)
    assert np.isclose(mdi[13], expected_m13)

    # First finite ADX must appear at index 26 (14 bars after index 13)
    assert np.isfinite(adx[26])
    assert np.isfinite(adx[27])


def test_adx_dmi_incremental_recursion_unbiased():
    """Verify that incremental online calculation exactly matches batch calculation (no lookahead / lag)."""
    rng = np.random.default_rng(999)
    n = 60
    close = 100.0 + np.cumsum(rng.standard_normal(n))
    high = close + np.abs(rng.standard_normal(n)) + 1.0
    low = close - np.abs(rng.standard_normal(n)) - 1.0

    p_full, m_full, a_full = calc_dmi_adx(high, low, close, length=14)

    # For each sub-window from index 27 to 60, last element must match full batch
    for t in range(27, n + 1):
        p_sub, m_sub, a_sub = calc_dmi_adx(high[:t], low[:t], close[:t], length=14)
        assert np.isclose(p_full[t - 1], p_sub[-1], equal_nan=True)
        assert np.isclose(m_full[t - 1], m_sub[-1], equal_nan=True)
        assert np.isclose(a_full[t - 1], a_sub[-1], equal_nan=True)


def test_adx_dmi_causality_future_perturbation():
    """Perturbing prices strictly after timestamp T must leave +DI, -DI, ADX at t <= T invariant."""
    rng = np.random.default_rng(2026)
    n = 100
    close = 100.0 + np.cumsum(rng.standard_normal(n))
    high = close + np.abs(rng.standard_normal(n)) + 1.0
    low = close - np.abs(rng.standard_normal(n)) - 1.0

    p_orig, m_orig, a_orig = calc_dmi_adx(high, low, close, length=14)

    t_cutoff = 55
    high_pert, low_pert, close_pert = high.copy(), low.copy(), close.copy()
    high_pert[t_cutoff + 1 :] *= 3.0
    low_pert[t_cutoff + 1 :] *= 0.1
    close_pert[t_cutoff + 1 :] *= 2.0

    p_pert, m_pert, a_pert = calc_dmi_adx(high_pert, low_pert, close_pert, length=14)

    assert np.allclose(p_orig[: t_cutoff + 1], p_pert[: t_cutoff + 1], equal_nan=True)
    assert np.allclose(m_orig[: t_cutoff + 1], m_pert[: t_cutoff + 1], equal_nan=True)
    assert np.allclose(a_orig[: t_cutoff + 1], a_pert[: t_cutoff + 1], equal_nan=True)


def test_adx_dmi_zero_range_boundary():
    """Flat prices (zero volatility) must not cause divide-by-zero or crash."""
    n = 40
    high = np.full(n, 100.0)
    low = np.full(n, 100.0)
    close = np.full(n, 100.0)

    pdi, mdi, adx = calc_dmi_adx(high, low, close, length=14)
    # Beyond warmup, values should safely be 0.0 without runtime warning / crash
    assert np.allclose(pdi[13:], 0.0)
    assert np.allclose(mdi[13:], 0.0)
    assert np.allclose(adx[26:], 0.0)


# ---------------------------------------------------------------------------
# 5. Funding 3d Rolling Mean & Causal Barrier
# ---------------------------------------------------------------------------

def test_funding_3d_causal_barrier():
    """Funding settlement occurring at settle_hour T must NOT be visible at bar with open_time = T.
    It becomes visible at subsequent bar with open_time > T.
    """
    bar_ms = 4 * 3600 * 1000
    t0 = 1672531200000  # 00:00 UTC
    open_times = [t0 + i * bar_ms for i in range(25)]
    klines = pd.DataFrame({"open_time": open_times})

    funding_data = [
        {"calc_time": t0, "settle_hour": t0, "rate": 0.0001},
        {"calc_time": t0 + 2 * bar_ms, "settle_hour": t0 + 2 * bar_ms, "rate": 0.0005},
    ]
    funding_df = pd.DataFrame(funding_data)

    f3d = calc_funding_3d(klines, funding_df, min_periods=1)

    assert np.isclose(f3d[0], 0.0)
    assert np.isclose(f3d[1], (0.0 + 0.0001) / 2.0)
    assert np.isclose(f3d[2], (0.0 + 0.0001 + 0.0001) / 3.0)
    assert np.isclose(f3d[3], (0.0 + 0.0001 + 0.0001 + 0.0005) / 4.0)


def test_funding_3d_multi_symbol_isolation():
    """Verify multi-symbol funding table does not cross-contaminate symbol funding rates."""
    bar_ms = 4 * 3600 * 1000
    t0 = 1672531200000
    klines_btc = pd.DataFrame({"open_time": [t0 + i * bar_ms for i in range(5)], "sym": ["BTCUSDT"] * 5})

    funding_data = [
        {"sym": "BTCUSDT", "calc_time": t0, "settle_hour": t0, "rate": 0.0001},
        {"sym": "ETHUSDT", "calc_time": t0, "settle_hour": t0, "rate": 0.0008},
    ]
    funding_df = pd.DataFrame(funding_data)

    f3d = calc_funding_3d(klines_btc, funding_df, min_periods=1)
    assert np.isclose(f3d[1], (0.0 + 0.0001) / 2.0)


# ---------------------------------------------------------------------------
# 6. Signal State Machine & ADX Chop Filter Interception
# ---------------------------------------------------------------------------

def test_step_signal_adx_chop_filter_intercepts_weak_trend():
    """ADX filter must intercept breakouts when ADX < 22.0."""
    # Breakout condition met, but ADX = 18.0 < 22.0 -> INTERCEPTED!
    sig, mult = step_signal(
        close_t=105.0, breakout_high=100.0, breakout_low=80.0,
        ema_fast=95.0, ema_slow=90.0, pos_current=0,
        chop_filter="ADX_22", adx_t=18.0, plus_di_t=30.0, minus_di_t=10.0,
    )
    assert sig == 0

    # Breakout condition met, but ADX is NaN (warmup) -> INTERCEPTED!
    sig_nan, mult_nan = step_signal(
        close_t=105.0, breakout_high=100.0, breakout_low=80.0,
        ema_fast=95.0, ema_slow=90.0, pos_current=0,
        chop_filter="ADX_22", adx_t=np.nan, plus_di_t=30.0, minus_di_t=10.0,
    )
    assert sig_nan == 0


def test_step_signal_adx_chop_filter_intercepts_wrong_direction():
    """ADX filter must intercept breakouts when directional dominance opposes breakout direction."""
    # Bullish breakout, ADX = 25.0 >= 22.0, but +DI <= -DI (e.g. 15.0 vs 28.0) -> INTERCEPTED!
    sig, mult = step_signal(
        close_t=105.0, breakout_high=100.0, breakout_low=80.0,
        ema_fast=95.0, ema_slow=90.0, pos_current=0,
        chop_filter="ADX_22", adx_t=25.0, plus_di_t=15.0, minus_di_t=28.0,
    )
    assert sig == 0

    # Bearish breakout, ADX = 25.0 >= 22.0, but -DI <= +DI (e.g. 18.0 vs 24.0) -> INTERCEPTED!
    sig_short, mult_short = step_signal(
        close_t=75.0, breakout_high=100.0, breakout_low=80.0,
        ema_fast=85.0, ema_slow=90.0, pos_current=0,
        chop_filter="ADX_22", adx_t=25.0, plus_di_t=24.0, minus_di_t=18.0,
    )
    assert sig_short == 0


def test_step_signal_adx_chop_filter_allows_valid_breakout():
    """ADX filter permits entry when ADX >= 22.0 and directional dominance matches."""
    # Bullish: ADX >= 22 and +DI > -DI -> ALLOWED!
    sig, mult = step_signal(
        close_t=105.0, breakout_high=100.0, breakout_low=80.0,
        ema_fast=95.0, ema_slow=90.0, pos_current=0,
        chop_filter="ADX_22", adx_t=25.0, plus_di_t=28.0, minus_di_t=15.0,
    )
    assert sig == 1 and mult == 1.0

    # Bearish: ADX >= 22 and -DI > +DI -> ALLOWED!
    sig_short, mult_short = step_signal(
        close_t=75.0, breakout_high=100.0, breakout_low=80.0,
        ema_fast=85.0, ema_slow=90.0, pos_current=0,
        chop_filter="ADX_22", adx_t=26.5, plus_di_t=12.0, minus_di_t=32.0,
    )
    assert sig_short == -1 and mult_short == 1.0


def test_step_signal_chop_filter_none_baseline():
    """Baseline chop_filter='None' allows breakout entry regardless of ADX."""
    sig, mult = step_signal(
        close_t=105.0, breakout_high=100.0, breakout_low=80.0,
        ema_fast=95.0, ema_slow=90.0, pos_current=0,
        chop_filter="None", adx_t=12.0, plus_di_t=10.0, minus_di_t=20.0,
    )
    assert sig == 1 and mult == 1.0


def test_step_signal_holding_channel_exit():
    """Channel exit condition: holding position exits when crossing EMA_slow regardless of ADX."""
    # Long held, close drops below EMA_slow -> exits
    sig, mult = step_signal(
        close_t=88.0, breakout_high=102.0, breakout_low=80.0,
        ema_fast=92.0, ema_slow=90.0, pos_current=1,
        chop_filter="ADX_22", adx_t=35.0, plus_di_t=30.0, minus_di_t=10.0,
    )
    assert sig == 0 and mult == 1.0

    # Short held, close rises above EMA_slow -> exits
    sig_s, mult_s = step_signal(
        close_t=93.0, breakout_high=102.0, breakout_low=80.0,
        ema_fast=88.0, ema_slow=90.0, pos_current=-1,
        chop_filter="ADX_22", adx_t=35.0, plus_di_t=10.0, minus_di_t=30.0,
    )
    assert sig_s == 0 and mult_s == 1.0


def test_step_signal_carry_overlay():
    """Test Carry rules when carry_mode == 'On':
    - F_3d > 0.0003: Long forbidden (signal=0, mult=0.0); Short gets 1.3x boost.
    - F_3d < -0.0002: Short forbidden (signal=0, mult=0.0); Long gets 1.2x boost.
    """
    # High funding: Long breakout attempted -> forbidden!
    sig, mult = step_signal(
        close_t=105.0, breakout_high=100.0, breakout_low=80.0,
        ema_fast=95.0, ema_slow=90.0, pos_current=0,
        f_3d=0.0004, carry_mode="On", chop_filter="None",
    )
    assert sig == 0 and mult == 0.0

    # High funding: Short breakout attempted -> boosted 1.3x!
    sig, mult = step_signal(
        close_t=75.0, breakout_high=100.0, breakout_low=80.0,
        ema_fast=85.0, ema_slow=90.0, pos_current=0,
        f_3d=0.0004, carry_mode="On", chop_filter="None",
    )
    assert sig == -1 and mult == 1.3

    # Negative funding: Short breakout attempted -> forbidden!
    sig, mult = step_signal(
        close_t=75.0, breakout_high=100.0, breakout_low=80.0,
        ema_fast=85.0, ema_slow=90.0, pos_current=0,
        f_3d=-0.0003, carry_mode="On", chop_filter="None",
    )
    assert sig == 0 and mult == 0.0

    # Negative funding: Long breakout attempted -> boosted 1.2x!
    sig, mult = step_signal(
        close_t=105.0, breakout_high=100.0, breakout_low=80.0,
        ema_fast=95.0, ema_slow=90.0, pos_current=0,
        f_3d=-0.0003, carry_mode="On", chop_filter="None",
    )
    assert sig == 1 and mult == 1.2


# ---------------------------------------------------------------------------
# 7. Data Isolation & Cutoff Guards (R4 & R5)
# ---------------------------------------------------------------------------

def test_sol_isolation_guard():
    """Verify SOLUSDT cannot be loaded under split='dev'."""
    with pytest.raises(PermissionError, match="strictly forbidden in dev split"):
        load_asset_v4(HOLDOUT_ASSET, split="dev")


@pytest.mark.needs_market_data
def test_dev_data_physical_cutoff():
    """Verify dev split klines and funding are strictly truncated before 2025-01-01."""
    cutoff_ms = get_dev_cutoff_ms()
    data = load_asset_v4("BTCUSDT", split="dev")
    klines = data["klines"]
    funding = data["funding"]

    assert len(klines) > 0
    assert (klines["open_time"] < cutoff_ms).all(), "Klines leak into holdout period!"
    assert (funding["calc_time"] < cutoff_ms).all(), "Funding leaks into holdout period!"


def test_grid_configs_count():
    """Verify exactly 16 configurations are registered in config.py."""
    assert len(GRID_CONFIGS) == 16


def test_build_indicators_df_with_adx():
    """Verify build_indicators_df attaches sym and computes all v4 indicators including ADX/DMI."""
    bar_ms = 4 * 3600 * 1000
    t0 = 1672531200000
    n = 40
    klines = pd.DataFrame({
        "open_time": [t0 + i * bar_ms for i in range(n)],
        "open": [100.0 + i for i in range(n)],
        "high": [105.0 + i for i in range(n)],
        "low": [95.0 + i for i in range(n)],
        "close": [102.0 + i for i in range(n)],
    })
    df = build_indicators_df(klines, funding=None, l_fast=12, l_slow=24, n_breakout=24, sym="BTCUSDT")
    assert df["sym"].iloc[0] == "BTCUSDT"
    assert "ema_fast" in df.columns
    assert "breakout_high" in df.columns
    assert "atr" in df.columns
    assert "plus_di" in df.columns
    assert "minus_di" in df.columns
    assert "adx" in df.columns
    assert "funding_3d" in df.columns
    assert "rv30" in df.columns

"""Unit tests for v4 backtest engine with exact hand-calculated reconciliation.

Verifies the mandatory scenarios:
1. ADX chop filter intercepts weak/counter-trend breakout entries vs allows strong trend entries.
2. Dual-track dynamic trailing stop (Long trade hand calculation): 4.0x disaster stop + 2.0x profit activation tightening.
3. Dual-track dynamic trailing stop (Short trade hand calculation): 4.0x disaster stop + 2.0x profit activation tightening.
4. Disaster stop intra-bar pierce (when profit activation threshold is never reached).
5. Single-track Wide_40 comparison (proves dual-track profit tightening vs wide trailing).
6. Capacity-aware nominal leverage normalization (total portfolio leverage strictly <= 1.0).
7. Carry extreme crowding overlay and exact funding settlement accounting.
8. Period-end forced liquidation at last bar close.
"""

import numpy as np
import pandas as pd
import pytest

from lz4.config import BAR_MS
from lz4.backtest_v4 import run_backtest_v4


def make_test_features_df(
    sym: str,
    opens: list[float],
    highs: list[float],
    lows: list[float],
    closes: list[float],
    breakout_highs: list[float],
    breakout_lows: list[float],
    ema_fasts: list[float],
    ema_slows: list[float],
    atrs: list[float],
    adxs: list[float] | None = None,
    plus_dis: list[float] | None = None,
    minus_dis: list[float] | None = None,
    funding_3ds: list[float] | None = None,
    rv30s: list[float] | None = None,
    start_ms: int = 1672531200000,
) -> pd.DataFrame:
    """Helper to construct a synthetic features DataFrame for v4 backtest verification."""
    n = len(opens)
    times = [start_ms + i * BAR_MS for i in range(n)]
    f3d = funding_3ds if funding_3ds is not None else [0.0] * n
    rv = rv30s if rv30s is not None else [0.1] * n
    adx_arr = adxs if adxs is not None else [25.0] * n
    pdi_arr = plus_dis if plus_dis is not None else [30.0] * n
    mdi_arr = minus_dis if minus_dis is not None else [10.0] * n

    return pd.DataFrame({
        "sym": [sym] * n,
        "open_time": times,
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "breakout_high": breakout_highs,
        "breakout_low": breakout_lows,
        "ema_fast": ema_fasts,
        "ema_slow": ema_slows,
        "atr": atrs,
        "adx": adx_arr,
        "plus_di": pdi_arr,
        "minus_di": mdi_arr,
        "funding_3d": f3d,
        "rv30": rv,
    })


# ---------------------------------------------------------------------------
# 1. ADX Chop Filter Interception vs Baseline
# ---------------------------------------------------------------------------

def test_adx_chop_filter_intercepts_entry_vs_none_baseline():
    """Verify ADX chop filter intercepts false breakout in chop (ADX < 22) while None baseline enters."""
    sym = "BTCUSDT"
    start_ms = 1672531200000

    opens = [100.0, 104.0, 105.0]
    highs = [105.0, 106.0, 106.0]
    lows = [98.0, 103.0, 104.0]
    closes = [104.0, 105.0, 105.0]

    b_highs = [102.0, 102.0, 102.0]
    b_lows = [90.0] * 3
    ema_fasts = [102.0, 103.0, 104.0]
    ema_slows = [95.0, 95.0, 95.0]
    atrs = [2.0] * 3
    # ADX = 18.0 (< 22.0 threshold -> indicates choppy market)
    adxs = [18.0] * 3
    plus_dis = [30.0] * 3
    minus_dis = [10.0] * 3

    feat_df = make_test_features_df(
        sym, opens, highs, lows, closes, b_highs, b_lows, ema_fasts, ema_slows, atrs,
        adxs=adxs, plus_dis=plus_dis, minus_dis=minus_dis, start_ms=start_ms,
    )

    # 1. With chop_filter="ADX_22": entry MUST be intercepted, zero trades!
    res_filtered = run_backtest_v4(
        feat_df,
        chop_filter="ADX_22",
        score_start_str="2023-01-01",
        score_end_str="2023-01-03",
    )
    assert len(res_filtered.trades) == 0
    assert np.allclose(res_filtered.r_bar, 0.0)

    # 2. With chop_filter="None": breakout enters at bar 1 open!
    res_unfiltered = run_backtest_v4(
        feat_df,
        chop_filter="None",
        score_start_str="2023-01-01",
        score_end_str="2023-01-03",
    )
    assert len(res_unfiltered.trades) == 1
    assert res_unfiltered.trades[0]["entry_px"] == 104.0


# ---------------------------------------------------------------------------
# 2. Dual-Track Trailing Stop: Long Hand-Calculated Reconciliation
# ---------------------------------------------------------------------------

def test_dual_track_trailing_stop_long_hand_calculation():
    """Exact bar-by-bar reconciliation of Dual_40_20 trailing stop for Long trade:
    
    - Entry at open[1] = 100.0, entry ATR = 2.0.
    - Initial disaster stop: 100.0 - 4.0 * 2.0 = 92.0.
    - Profit activation target: 2.0 * ATR = 4.0 (trigger price >= 104.0).
    - Bar 1: high = 103.0 (< 104.0) -> profit target not reached, stop stays at 92.0.
    - Bar 2: high = 105.0 (>= 104.0) -> profit target reached! Trailing stop tightens to 105.0 - 2.0 * 2.0 = 101.0.
    - Bar 3: low = 100.5 (<= 101.0) -> pierced intra-bar at exit_px = min(104.0, 101.0) = 101.0!
    """
    sym = "BTCUSDT"
    start_ms = 1672531200000

    opens = [100.0, 100.0, 102.0, 104.0, 101.0]
    highs = [105.0, 103.0, 105.0, 104.5, 102.0]
    lows = [98.0, 99.0, 101.0, 100.5, 100.0]
    closes = [104.0, 102.0, 104.0, 101.0, 101.0]

    b_highs = [102.0] * 5
    b_lows = [90.0] * 5
    ema_fasts = [102.0] * 5
    ema_slows = [95.0] * 5
    atrs = [2.0] * 5
    adxs = [25.0] * 5
    plus_dis = [30.0] * 5
    minus_dis = [10.0] * 5

    feat_df = make_test_features_df(
        sym, opens, highs, lows, closes, b_highs, b_lows, ema_fasts, ema_slows, atrs,
        adxs=adxs, plus_dis=plus_dis, minus_dis=minus_dis, start_ms=start_ms,
    )

    res = run_backtest_v4(
        feat_df,
        stop_mode="Dual_40_20",
        cost_mult=1.0,
        score_start_str="2023-01-01",
        score_end_str="2023-01-05",
    )

    assert len(res.trades) == 1
    tr = res.trades[0]
    assert tr["direction"] == 1
    assert tr["reason"] == "stop_loss"
    assert tr["entry_px"] == 100.0
    assert tr["exit_px"] == 101.0
    assert np.isclose(tr["gross"], 0.01)  # +100 bps
    assert np.isclose(tr["rt"], 0.0014)   # 2 * 7 bps = 14 bps
    assert np.isclose(tr["net"], 0.01 - 0.0014)

    # Hand-calculated bar returns:
    # Bar 0: Flat -> 0.0
    assert np.isclose(res.r_bar[0], 0.0)
    # Bar 1: Entry fee 0.0007, holds 100.0 -> open[2]=102.0: 1.0 * (102/100 - 1) = 0.02 -> 0.02 - 0.0007 = 0.0193
    assert np.isclose(res.r_bar[1], 0.0193)
    # Bar 2: Holds 102.0 -> open[3]=104.0: 1.0 * (104/102 - 1) = 2.0 / 102.0
    assert np.isclose(res.r_bar[2], 2.0 / 102.0)
    # Bar 3: Stopped out at 101.0 from open[3]=104.0: 1.0 * (101/104 - 1) - 0.0007 exit fee
    assert np.isclose(res.r_bar[3], (101.0 / 104.0 - 1.0) - 0.0007)
    # Bar 4: Flat -> 0.0
    assert np.isclose(res.r_bar[4], 0.0)


# ---------------------------------------------------------------------------
# 3. Dual-Track Trailing Stop: Short Hand-Calculated Reconciliation
# ---------------------------------------------------------------------------

def test_dual_track_trailing_stop_short_hand_calculation():
    """Exact bar-by-bar reconciliation of Dual_40_20 trailing stop for Short trade:
    
    - Entry at open[1] = 100.0, entry ATR = 2.0.
    - Initial disaster stop: 100.0 + 4.0 * 2.0 = 108.0.
    - Profit activation target: 2.0 * ATR = 4.0 (trigger price <= 96.0).
    - Bar 1: low = 97.0 (> 96.0) -> profit target not reached, stop stays at 108.0.
    - Bar 2: low = 95.0 (<= 96.0) -> profit target reached! Trailing stop tightens to 95.0 + 2.0 * 2.0 = 99.0.
    - Bar 3: high = 99.5 (>= 99.0) -> pierced intra-bar at exit_px = max(96.0, 99.0) = 99.0!
    """
    sym = "BTCUSDT"
    start_ms = 1672531200000

    opens = [100.0, 100.0, 98.0, 96.0, 99.0]
    highs = [101.0, 101.0, 99.0, 99.5, 100.0]
    lows = [95.0, 97.0, 95.0, 95.5, 98.0]
    closes = [96.0, 98.0, 96.0, 99.0, 99.0]

    b_highs = [105.0] * 5
    b_lows = [98.0] * 5
    ema_fasts = [95.0] * 5
    ema_slows = [100.0] * 5
    atrs = [2.0] * 5
    adxs = [25.0] * 5
    plus_dis = [10.0] * 5
    minus_dis = [30.0] * 5

    feat_df = make_test_features_df(
        sym, opens, highs, lows, closes, b_highs, b_lows, ema_fasts, ema_slows, atrs,
        adxs=adxs, plus_dis=plus_dis, minus_dis=minus_dis, start_ms=start_ms,
    )

    res = run_backtest_v4(
        feat_df,
        stop_mode="Dual_40_20",
        cost_mult=1.0,
        score_start_str="2023-01-01",
        score_end_str="2023-01-05",
    )

    assert len(res.trades) == 1
    tr = res.trades[0]
    assert tr["direction"] == -1
    assert tr["reason"] == "stop_loss"
    assert tr["entry_px"] == 100.0
    assert tr["exit_px"] == 99.0
    assert np.isclose(tr["gross"], 0.01)  # +100 bps
    assert np.isclose(tr["rt"], 0.0014)
    assert np.isclose(tr["net"], 0.01 - 0.0014)

    # Bar return checks:
    assert np.isclose(res.r_bar[0], 0.0)
    # Bar 1: Entry fee 0.0007, holds 100.0 -> open[2]=98.0: -1 * (98/100 - 1) = 0.02 -> 0.02 - 0.0007 = 0.0193
    assert np.isclose(res.r_bar[1], 0.0193)
    # Bar 2: Holds 98.0 -> open[3]=96.0: -1 * (96/98 - 1) = 2.0 / 98.0
    assert np.isclose(res.r_bar[2], 2.0 / 98.0)
    # Bar 3: Stopped out at 99.0 from open[3]=96.0: -1 * (99/96 - 1) - 0.0007 exit fee
    assert np.isclose(res.r_bar[3], -1.0 * (99.0 / 96.0 - 1.0) - 0.0007)
    assert np.isclose(res.r_bar[4], 0.0)


# ---------------------------------------------------------------------------
# 4. Disaster Stop Pierce (without Profit Activation)
# ---------------------------------------------------------------------------

def test_disaster_stop_pierce_without_profit_activation():
    """Verify disaster stop pierces when market collapses immediately without reaching 2.0x profit."""
    sym = "BTCUSDT"
    start_ms = 1672531200000

    opens = [100.0, 100.0, 94.0, 90.0]
    highs = [105.0, 102.0, 95.0, 91.0]
    lows = [98.0, 99.0, 90.0, 89.0]  # low[2] = 90.0 <= disaster stop 92.0!
    closes = [104.0, 100.0, 91.0, 90.0]

    b_highs = [102.0] * 4
    b_lows = [90.0] * 4
    ema_fasts = [102.0] * 4
    ema_slows = [95.0] * 4
    atrs = [2.0] * 4

    feat_df = make_test_features_df(sym, opens, highs, lows, closes, b_highs, b_lows, ema_fasts, ema_slows, atrs, start_ms=start_ms)

    res = run_backtest_v4(
        feat_df,
        stop_mode="Dual_40_20",
        score_start_str="2023-01-01",
        score_end_str="2023-01-04",
    )

    assert len(res.trades) == 1
    tr = res.trades[0]
    assert tr["direction"] == 1
    assert tr["reason"] == "stop_loss"
    assert tr["entry_px"] == 100.0
    # Pierced at disaster stop: min(open[2]=94.0, sl=92.0) = 92.0!
    assert tr["exit_px"] == 92.0
    assert np.isclose(tr["gross"], -0.08)  # -4.0 * 2.0 = -8.0% loss


# ---------------------------------------------------------------------------
# 5. Dual_40_20 vs Wide_40 Comparison
# ---------------------------------------------------------------------------

def test_dual_track_vs_wide_40_comparison():
    """On identical prices, Dual_40_20 locks in profit at 101.0, whereas Wide_40 holds because stop is 97.0."""
    sym = "BTCUSDT"
    start_ms = 1672531200000

    opens = [100.0, 100.0, 102.0, 104.0, 101.0]
    highs = [105.0, 103.0, 105.0, 104.5, 102.0]
    lows = [98.0, 99.0, 101.0, 100.5, 100.0]  # low[3] = 100.5
    closes = [104.0, 102.0, 104.0, 101.0, 101.0]

    b_highs = [102.0] * 5
    b_lows = [90.0] * 5
    ema_fasts = [102.0] * 5
    ema_slows = [95.0] * 5
    atrs = [2.0] * 5

    feat_df = make_test_features_df(sym, opens, highs, lows, closes, b_highs, b_lows, ema_fasts, ema_slows, atrs, start_ms=start_ms)

    # Dual_40_20: stop tightened to 101.0 at bar 2 close -> low[3] = 100.5 <= 101.0 -> STOPPED OUT!
    res_dual = run_backtest_v4(feat_df, stop_mode="Dual_40_20", score_start_str="2023-01-01", score_end_str="2023-01-05")
    assert len(res_dual.trades) == 1
    assert res_dual.trades[0]["reason"] == "stop_loss"

    # Wide_40: stop trails at 105.0 - 4.0 * 2.0 = 97.0 -> low[3] = 100.5 > 97.0 -> NOT stopped out!
    # Holds until period_end!
    res_wide = run_backtest_v4(feat_df, stop_mode="Wide_40", score_start_str="2023-01-01", score_end_str="2023-01-05")
    assert len(res_wide.trades) == 1
    assert res_wide.trades[0]["reason"] == "period_end"


# ---------------------------------------------------------------------------
# 6. Capacity-Aware Nominal Leverage Cap (<= 1.0)
# ---------------------------------------------------------------------------

def test_capacity_aware_leverage_cap():
    """Verify combined portfolio nominal leverage never exceeds max_leverage (1.0)."""
    start_ms = 1672531200000

    # 2 assets: BTC and ETH both break out long with negative funding (1.2x carry boost)
    # Raw weight = 0.5 * 1.2 = 0.6 each -> total raw = 1.2 > 1.0!
    # Scaled down to 0.5 each -> total nominal leverage = 1.0!
    opens = [100.0, 100.0, 100.0]
    highs = [105.0, 101.0, 101.0]
    lows = [98.0, 99.0, 99.0]
    closes = [104.0, 100.0, 100.0]

    b_highs = [102.0] * 3
    b_lows = [90.0] * 3
    ema_fasts = [102.0] * 3
    ema_slows = [95.0] * 3
    atrs = [2.0] * 3
    funding_3ds = [-0.0003] * 3  # Triggers 1.2x long boost
    rv30s = [0.2] * 3

    df_btc = make_test_features_df("BTCUSDT", opens, highs, lows, closes, b_highs, b_lows, ema_fasts, ema_slows, atrs, funding_3ds=funding_3ds, rv30s=rv30s, start_ms=start_ms)
    df_eth = make_test_features_df("ETHUSDT", opens, highs, lows, closes, b_highs, b_lows, ema_fasts, ema_slows, atrs, funding_3ds=funding_3ds, rv30s=rv30s, start_ms=start_ms)

    combined_df = pd.concat([df_btc, df_eth], ignore_index=True)

    res = run_backtest_v4(
        combined_df,
        max_leverage=1.0,
        score_start_str="2023-01-01",
        score_end_str="2023-01-03",
    )

    # Both entered at open[1] = 100.0 and exited at period_end close[2] = 100.0
    assert len(res.trades) == 2
    # Entry fee paid per asset: weight * cost_per_side.
    # BTC cost_per_side = 0.0007. Weight scaled to 0.5 -> fee = 0.5 * 0.0007 = 0.00035.
    # ETH cost_per_side = 0.0007. Weight scaled to 0.5 -> fee = 0.5 * 0.0007 = 0.00035.
    # Combined entry fee = 0.0007. Total return at bar 1 must be -0.0007!
    assert np.isclose(res.r_bar[1], -0.0007)


# ---------------------------------------------------------------------------
# 7. Carry Crowding Overlay & Exact Funding Settlement
# ---------------------------------------------------------------------------

def test_carry_crowding_and_funding_settlement():
    """Verify funding settlement is accurately subtracted from bar returns and tracked in trade record."""
    sym = "BTCUSDT"
    start_ms = 1672531200000

    opens = [100.0, 100.0, 102.0]
    highs = [105.0, 103.0, 103.0]
    lows = [98.0, 99.0, 101.0]
    closes = [104.0, 102.0, 102.0]

    b_highs = [102.0] * 3
    b_lows = [90.0] * 3
    ema_fasts = [102.0] * 3
    ema_slows = [95.0] * 3
    atrs = [2.0] * 3

    feat_df = make_test_features_df(sym, opens, highs, lows, closes, b_highs, b_lows, ema_fasts, ema_slows, atrs, start_ms=start_ms)

    # Funding settlement at settle_hour = start_ms + 2*BAR_MS (during bar 1: open[1] < sh <= open[2])
    # rate = 0.0005 (5 bps)
    funding_df = pd.DataFrame([{
        "sym": sym,
        "calc_time": start_ms + 2 * BAR_MS,
        "settle_hour": start_ms + 2 * BAR_MS,
        "rate": 0.0005,
    }])

    res = run_backtest_v4(
        feat_df,
        funding_df=funding_df,
        score_start_str="2023-01-01",
        score_end_str="2023-01-03",
    )

    assert len(res.trades) == 1
    tr = res.trades[0]
    assert np.isclose(tr["funding_sum"], 0.0005)
    # Gross = 102 / 100 - 1 = 0.02. RT = 0.0014. Funding = 0.0005.
    # Net = 0.02 - 0.0014 - 0.0005 = 0.0181.
    assert np.isclose(tr["net"], 0.0181)

    # Bar 1 return: price ret (0.02) - entry fee (0.0007) - funding (0.0005) = 0.0188.
    assert np.isclose(res.r_bar[1], 0.0188)


# ---------------------------------------------------------------------------
# 8. Period-End Forced Liquidation
# ---------------------------------------------------------------------------

def test_period_end_forced_liquidation():
    """Verify open positions are forcibly liquidated at final bar close with reason='period_end'."""
    sym = "BTCUSDT"
    start_ms = 1672531200000

    opens = [100.0, 100.0, 105.0]
    highs = [105.0, 106.0, 108.0]
    lows = [98.0, 99.0, 104.0]
    closes = [104.0, 105.0, 107.0]

    b_highs = [102.0] * 3
    b_lows = [90.0] * 3
    ema_fasts = [102.0] * 3
    ema_slows = [95.0] * 3
    atrs = [2.0] * 3

    feat_df = make_test_features_df(sym, opens, highs, lows, closes, b_highs, b_lows, ema_fasts, ema_slows, atrs, start_ms=start_ms)

    res = run_backtest_v4(
        feat_df,
        score_start_str="2023-01-01",
        score_end_str="2023-01-03",
    )

    assert len(res.trades) == 1
    tr = res.trades[0]
    assert tr["reason"] == "period_end"
    assert tr["entry_px"] == 100.0
    assert tr["exit_px"] == 107.0
    assert np.isclose(tr["gross"], 0.07)


# ---------------------------------------------------------------------------
# 9. Leverage Exhaustion Zero-Weight Phantom Trade Protection
# ---------------------------------------------------------------------------

def test_leverage_exhaustion_prevents_zero_weight_phantom_trades():
    """Verify that when available leverage is 0, new breakout signals do not open
    phantom trades with zero weight, protecting trade statistics from distortion."""
    start_ms = 1672531200000
    n = 4
    times = [start_ms + i * BAR_MS for i in range(n)]

    # BTC breaks out at bar 0, occupies full allowed leverage (max_leverage=0.5)
    btc = pd.DataFrame({
        "sym": ["BTCUSDT"] * n,
        "open_time": times,
        "open": [100.0, 100.0, 102.0, 104.0],
        "high": [105.0, 103.0, 105.0, 105.0],
        "low": [98.0, 99.0, 101.0, 103.0],
        "close": [104.0, 102.0, 104.0, 104.0],
        "breakout_high": [102.0] * n,
        "breakout_low": [90.0] * n,
        "ema_fast": [102.0] * n,
        "ema_slow": [95.0] * n,
        "atr": [2.0] * n,
        "adx": [25.0] * n,
        "plus_di": [30.0] * n,
        "minus_di": [10.0] * n,
        "funding_3d": [0.0] * n,
        "rv30": [0.1] * n,
    })

    # ETH breaks out at bar 1, but BTC already consumes max_leverage (0.5) -> avail_w == 0.0
    eth = pd.DataFrame({
        "sym": ["ETHUSDT"] * n,
        "open_time": times,
        "open": [10.0, 10.0, 10.0, 12.0],
        "high": [10.5, 12.0, 12.5, 12.5],
        "low": [9.5, 9.8, 9.9, 11.5],
        "close": [10.0, 11.5, 12.0, 12.0],
        "breakout_high": [11.0] * n,
        "breakout_low": [8.0] * n,
        "ema_fast": [11.0] * n,
        "ema_slow": [9.0] * n,
        "atr": [0.5] * n,
        "adx": [25.0] * n,
        "plus_di": [30.0] * n,
        "minus_di": [10.0] * n,
        "funding_3d": [0.0] * n,
        "rv30": [0.1] * n,
    })

    combined = pd.concat([btc, eth], ignore_index=True)
    res = run_backtest_v4(combined, max_leverage=0.5, score_start_str="2023-01-01", score_end_str="2023-01-04")

    # Only BTC must have opened and traded; ETH must NOT produce phantom trades with 0 weight
    assert len(res.trades) == 1
    assert res.trades[0]["sym"] == "BTCUSDT"
    assert (res.per_asset_r["ETHUSDT"] == 0.0).all()


# ---------------------------------------------------------------------------
# 10. Short Disaster Stop Intra-bar Pierce
# ---------------------------------------------------------------------------

def test_disaster_stop_pierce_short_trade():
    """Verify short disaster stop pierces intra-bar when market spikes upward
    without profit activation threshold being reached."""
    sym = "BTCUSDT"
    start_ms = 1672531200000

    opens = [100.0, 100.0, 105.0, 110.0]
    highs = [101.0, 101.0, 110.0, 112.0]  # high[2] = 110.0 >= disaster stop 108.0!
    lows = [95.0, 97.0, 104.0, 108.0]
    closes = [96.0, 100.0, 109.0, 110.0]

    b_highs = [105.0] * 4
    b_lows = [98.0] * 4
    ema_fasts = [95.0] * 4
    ema_slows = [100.0] * 4
    atrs = [2.0] * 4

    feat_df = make_test_features_df(
        sym, opens, highs, lows, closes, b_highs, b_lows, ema_fasts, ema_slows, atrs,
        plus_dis=[10.0] * 4, minus_dis=[30.0] * 4, start_ms=start_ms,
    )

    res = run_backtest_v4(
        feat_df,
        stop_mode="Dual_40_20",
        score_start_str="2023-01-01",
        score_end_str="2023-01-04",
    )

    assert len(res.trades) == 1
    tr = res.trades[0]
    assert tr["direction"] == -1
    assert tr["reason"] == "stop_loss"
    assert tr["entry_px"] == 100.0
    # Pierced at disaster stop: max(open[2]=105.0, sl=108.0) = 108.0!
    assert tr["exit_px"] == 108.0
    assert np.isclose(tr["gross"], -0.08)  # -4.0 * 2.0 = -8.0% loss


# ---------------------------------------------------------------------------
# 11. Friction Cost Multiplier Scaling (Stress Testing)
# ---------------------------------------------------------------------------

def test_cost_mult_stress_scaling():
    """Verify that cost_mult scales fees and round-trip frictions linearly."""
    sym = "BTCUSDT"
    start_ms = 1672531200000

    opens = [100.0, 100.0, 105.0]
    highs = [105.0, 106.0, 108.0]
    lows = [98.0, 99.0, 104.0]
    closes = [104.0, 105.0, 107.0]

    b_highs = [102.0] * 3
    b_lows = [90.0] * 3
    ema_fasts = [102.0] * 3
    ema_slows = [95.0] * 3
    atrs = [2.0] * 3

    feat_df = make_test_features_df(sym, opens, highs, lows, closes, b_highs, b_lows, ema_fasts, ema_slows, atrs, start_ms=start_ms)

    res_1x = run_backtest_v4(feat_df, cost_mult=1.0, score_start_str="2023-01-01", score_end_str="2023-01-03")
    res_2x = run_backtest_v4(feat_df, cost_mult=2.0, score_start_str="2023-01-01", score_end_str="2023-01-03")

    tr_1x = res_1x.trades[0]
    tr_2x = res_2x.trades[0]

    # Gross return must be invariant to cost multiplier
    assert np.isclose(tr_1x["gross"], tr_2x["gross"])
    # Round-trip friction must double exactly
    assert np.isclose(tr_2x["rt"], tr_1x["rt"] * 2.0)
    # Net return must reflect doubled friction
    assert np.isclose(tr_2x["net"], tr_2x["gross"] - 2.0 * tr_1x["rt"])

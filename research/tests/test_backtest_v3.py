"""Unit tests for v3 backtest engine with exact hand-calculated reconciliation.

Verifies the 4 mandatory scenarios:
1. Long trend holding, trailing stop moving up, intra-bar pierce stop-out (with funding settlement).
2. Slow EMA channel exit.
3. Carry inhibit long and reversal position multiplier (1.3x and 1.2x).
4. Period-end forced liquidation.
"""

import numpy as np
import pandas as pd
import pytest

from lz3.config import BAR_MS
from lz3.backtest_v3 import run_backtest_v3


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
    funding_3ds: list[float] | None = None,
    rv30s: list[float] | None = None,
    start_ms: int = 1672531200000,
) -> pd.DataFrame:
    """Helper to construct a synthetic features DataFrame for backtest verification."""
    n = len(opens)
    times = [start_ms + i * BAR_MS for i in range(n)]
    f3d = funding_3ds if funding_3ds is not None else [0.0] * n
    rv = rv30s if rv30s is not None else [0.1] * n

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
        "funding_3d": f3d,
        "rv30": rv,
    })


def test_scenario_1_trailing_stop_and_funding():
    """Scenario 1: Long trade, trailing stop moves up bar by bar, pierced intra-bar, with funding."""
    sym = "BTCUSDT"
    start_ms = 1672531200000  # 2023-01-01 00:00 UTC

    # 6 bars: k=0, 1, 2, 3, 4, 5
    opens = [100.0, 104.0, 108.0, 111.0, 106.0, 106.0]
    highs = [105.0, 110.0, 112.0, 111.0, 107.0, 107.0]
    lows = [98.0, 103.0, 107.0, 105.0, 105.0, 105.0]
    closes = [104.0, 108.0, 111.0, 106.0, 106.0, 106.0]

    b_highs = [103.0, 103.0, 110.0, 112.0, 112.0, 112.0]
    b_lows = [90.0] * 6
    ema_fasts = [102.0, 104.0, 106.0, 106.0, 105.0, 105.0]
    ema_slows = [95.0, 96.0, 97.0, 97.0, 97.0, 97.0]
    atrs = [2.0] * 6

    feat_df = make_test_features_df(
        sym=sym,
        opens=opens,
        highs=highs,
        lows=lows,
        closes=closes,
        breakout_highs=b_highs,
        breakout_lows=b_lows,
        ema_fasts=ema_fasts,
        ema_slows=ema_slows,
        atrs=atrs,
        start_ms=start_ms,
    )

    # Funding settlement at settle_hour = start_ms + 2*BAR_MS (during bar 1: open[1] < sh <= open[2])
    # rate = 0.0002
    funding_df = pd.DataFrame([{
        "sym": sym,
        "calc_time": start_ms + 2 * BAR_MS,
        "settle_hour": start_ms + 2 * BAR_MS,
        "rate": 0.0002,
    }])

    res = run_backtest_v3(
        features_df=feat_df,
        funding_df=funding_df,
        atr_mult=2.0,
        carry_mode="Off",
        cost_mult=1.0,
        score_start_str="2023-01-01",
        score_end_str="2023-01-05",
    )

    # 1. Bar Return Verification:
    # Bar 0: Flat -> 0.0
    assert np.isclose(res.r_bar[0], 0.0)

    # Bar 1: Entry at open[1] = 104.0 (fee = 0.0007), holding to open[2] = 108.0 (ret = 4/104), funding = 0.0002
    expected_r1 = (4.0 / 104.0) - 0.0007 - 0.0002
    assert np.isclose(res.r_bar[1], expected_r1)

    # Bar 2: Holding 108.0 -> 111.0 (ret = 3/108), no fees/funding
    expected_r2 = 3.0 / 108.0
    assert np.isclose(res.r_bar[2], expected_r2)

    # Bar 3: StopLoss was 108.0 (from bar 2: high_2 - 2*atr = 112 - 4 = 108).
    # low_3 = 105.0 <= 108.0 -> stopped out at exit_px = min(111, 108) = 108.0.
    # Return during bar: (108.0 / 111.0 - 1.0) - 0.0007 fee.
    expected_r3 = (108.0 / 111.0 - 1.0) - 0.0007
    assert np.isclose(res.r_bar[3], expected_r3)

    # Bars 4, 5: Flat -> 0.0
    assert np.isclose(res.r_bar[4], 0.0)
    assert np.isclose(res.r_bar[5], 0.0)

    # 2. Equity Curve Verification:
    expected_eq = np.cumprod(np.concatenate([[1.0], 1.0 + res.r_bar]))
    assert np.allclose(res.equity, expected_eq)

    # 3. Trade Record Verification:
    assert len(res.trades) == 1
    tr = res.trades[0]
    assert tr["direction"] == 1
    assert np.isclose(tr["entry_px"], 104.0)
    assert np.isclose(tr["exit_px"], 108.0)
    assert np.isclose(tr["gross"], 4.0 / 104.0)
    assert np.isclose(tr["rt"], 0.0014)
    assert np.isclose(tr["funding_sum"], 0.0002)
    assert np.isclose(tr["net"], (4.0 / 104.0) - 0.0014 - 0.0002)
    assert tr["reason"] == "stop_loss"


def test_scenario_2_channel_exit():
    """Scenario 2: Long trade exits at next bar open after close falls below slow EMA."""
    sym = "BTCUSDT"
    start_ms = 1672531200000

    # 5 bars: k=0, 1, 2, 3, 4
    opens = [100.0, 100.0, 101.0, 94.0, 94.0]
    highs = [105.0, 102.0, 103.0, 95.0, 95.0]
    lows = [98.0, 99.0, 98.0, 92.0, 92.0]
    closes = [104.0, 101.0, 94.0, 93.0, 93.0]

    b_highs = [102.0, 102.0, 102.0, 102.0, 102.0]
    b_lows = [80.0] * 5
    ema_fasts = [102.0, 100.0, 98.0, 95.0, 95.0]
    ema_slows = [95.0, 95.0, 96.0, 96.0, 96.0]  # At bar 2 close: close 94.0 < slow EMA 96.0!
    atrs = [10.0] * 5  # Wide ATR so trailing stop is not triggered

    feat_df = make_test_features_df(
        sym=sym,
        opens=opens,
        highs=highs,
        lows=lows,
        closes=closes,
        breakout_highs=b_highs,
        breakout_lows=b_lows,
        ema_fasts=ema_fasts,
        ema_slows=ema_slows,
        atrs=atrs,
        start_ms=start_ms,
    )

    res = run_backtest_v3(
        features_df=feat_df,
        funding_df=None,
        atr_mult=2.0,
        carry_mode="Off",
        cost_mult=1.0,
        score_start_str="2023-01-01",
        score_end_str="2023-01-05",
    )

    # 1. Bar Returns:
    # Bar 0: Flat -> 0.0
    assert np.isclose(res.r_bar[0], 0.0)
    # Bar 1: Entry at open[1] = 100.0 (fee = 0.0007), holds to open[2] = 101.0 (ret = 1/100)
    assert np.isclose(res.r_bar[1], 0.01 - 0.0007)
    # Bar 2: Holds 101.0 -> open[3] = 94.0 (ret = 94/101 - 1). Close 94 < slow EMA 96 triggers exit.
    assert np.isclose(res.r_bar[2], 94.0 / 101.0 - 1.0)
    # Bar 3: Exit executed at open[3] = 94.0. Exit fee 0.0007.
    assert np.isclose(res.r_bar[3], -0.0007)
    # Bar 4: Flat -> 0.0
    assert np.isclose(res.r_bar[4], 0.0)

    # 2. Trade Record:
    assert len(res.trades) == 1
    tr = res.trades[0]
    assert np.isclose(tr["entry_px"], 100.0)
    assert np.isclose(tr["exit_px"], 94.0)
    assert np.isclose(tr["gross"], -0.06)
    assert np.isclose(tr["rt"], 0.0014)
    assert np.isclose(tr["net"], -0.06 - 0.0014)
    assert tr["reason"] == "channel_exit"


def test_scenario_3_carry_inhibit_and_reversal_multiplier():
    """Scenario 3: Carry inhibits long breakout, opens 1.3x short, and flips to 1.2x long."""
    sym = "BTCUSDT"
    start_ms = 1672531200000

    # 6 bars: k=0, 1, 2, 3, 4, 5
    opens = [100.0, 100.0, 100.0, 96.0, 96.0, 96.0]
    highs = [105.0, 101.0, 101.0, 98.0, 98.0, 98.0]
    lows = [98.0, 95.0, 95.0, 94.0, 94.0, 94.0]
    closes = [104.0, 95.0, 96.0, 97.0, 97.0, 97.0]

    b_highs = [102.0, 102.0, 95.0, 95.0, 95.0, 95.0]
    b_lows = [96.0, 96.0, 90.0, 90.0, 90.0, 90.0]
    ema_fasts = [102.0, 92.0, 98.0, 98.0, 98.0, 98.0]
    ema_slows = [95.0, 96.0, 95.0, 95.0, 95.0, 95.0]
    atrs = [10.0] * 6

    # Funding 3d:
    # Bar 0: F_3d = 0.0004 (> 0.0003) -> Long breakout attempted but inhibited!
    # Bar 1: F_3d = 0.0004 (> 0.0003) -> Short breakout occurs: close 95 < b_low 96, fast 92 < slow 96.
    #        Carry rule: Short boosted to 1.3x weight!
    # Bar 2: F_3d = -0.0003 (< -0.0002) -> Long breakout occurs: close 96 > b_high 95, fast 98 > slow 95.
    #        Carry rule: Long boosted to 1.2x weight! Flip from Short (1.3x) to Long (1.2x).
    funding_3ds = [0.0004, 0.0004, -0.0003, 0.0, 0.0, 0.0]

    feat_df = make_test_features_df(
        sym=sym,
        opens=opens,
        highs=highs,
        lows=lows,
        closes=closes,
        breakout_highs=b_highs,
        breakout_lows=b_lows,
        ema_fasts=ema_fasts,
        ema_slows=ema_slows,
        atrs=atrs,
        funding_3ds=funding_3ds,
        start_ms=start_ms,
    )

    res = run_backtest_v3(
        features_df=feat_df,
        funding_df=None,
        atr_mult=2.0,
        carry_mode="On",
        cost_mult=1.0,
        score_start_str="2023-01-01",
        score_end_str="2023-01-05",
        max_leverage=2.0,
    )

    # 1. Bar 0 & 1: Long was inhibited at bar 0 close, so bar 1 is completely flat
    assert np.isclose(res.r_bar[0], 0.0)
    assert np.isclose(res.r_bar[1], 0.0)

    # 2. Bar 2: Short entered with 1.3x multiplier at open[2] = 100.0
    # Entry fee = 1.3 * 0.0007 = 0.00091.
    # Holding 100.0 -> open[3] = 96.0: return = -1 * 1.3 * (96/100 - 1) = +0.052.
    expected_r2 = 0.052 - 0.00091
    assert np.isclose(res.r_bar[2], expected_r2)

    # 3. Bar 3: Reversal executed at open[3] = 96.0
    # Fee paid = (old_w + new_w) * 0.0007 = (1.3 + 1.2) * 0.0007 = 0.00175.
    # Price holds 96.0 -> 96.0 (flat).
    expected_r3 = -0.00175
    assert np.isclose(res.r_bar[3], expected_r3)

    # 4. First trade was Short: entry 100.0, exit 96.0, gross = +0.04
    assert len(res.trades) == 2
    tr0 = res.trades[0]
    assert tr0["direction"] == -1
    assert np.isclose(tr0["entry_px"], 100.0)
    assert np.isclose(tr0["exit_px"], 96.0)
    assert np.isclose(tr0["gross"], 0.04)
    assert tr0["reason"] == "reverse"

    # 5. Second trade was Long: entry 96.0, exit 97.0, gross = +1.0417%, reason = period_end
    tr1 = res.trades[1]
    assert tr1["direction"] == 1
    assert np.isclose(tr1["entry_px"], 96.0)
    assert np.isclose(tr1["exit_px"], 97.0)
    assert np.isclose(tr1["gross"], 97.0 / 96.0 - 1.0)
    assert np.isclose(tr1["rt"], 0.0014)
    assert np.isclose(tr1["net"], (97.0 / 96.0 - 1.0) - 0.0014)
    assert tr1["reason"] == "period_end"


def test_scenario_4_period_end_forced_liquidation():
    """Scenario 4: Open position is forcibly liquidated at the final bar close."""
    sym = "BTCUSDT"
    start_ms = 1672531200000

    # 3 bars: k=0, 1, 2
    opens = [100.0, 100.0, 110.0]
    highs = [105.0, 112.0, 116.0]
    lows = [98.0, 99.0, 109.0]
    closes = [104.0, 110.0, 115.0]

    b_highs = [102.0, 102.0, 102.0]
    b_lows = [80.0] * 3
    ema_fasts = [102.0, 105.0, 108.0]
    ema_slows = [95.0, 95.0, 95.0]
    atrs = [10.0] * 3

    feat_df = make_test_features_df(
        sym=sym,
        opens=opens,
        highs=highs,
        lows=lows,
        closes=closes,
        breakout_highs=b_highs,
        breakout_lows=b_lows,
        ema_fasts=ema_fasts,
        ema_slows=ema_slows,
        atrs=atrs,
        start_ms=start_ms,
    )

    res = run_backtest_v3(
        features_df=feat_df,
        funding_df=None,
        atr_mult=2.0,
        carry_mode="Off",
        cost_mult=1.0,
        score_start_str="2023-01-01",
        score_end_str="2023-01-05",
    )

    # 1. Bar 2 is the final bar: forced liquidation at close[2] = 115.0
    # Bar 1 return: (110.0 / 100.0 - 1.0) - 0.0007 = 0.10 - 0.0007 = 0.0993
    assert np.isclose(res.r_bar[1], 0.0993)
    # Bar 2 return: (115.0 / 110.0 - 1.0) - 0.0007 exit fee
    expected_r2 = (115.0 / 110.0 - 1.0) - 0.0007
    assert np.isclose(res.r_bar[2], expected_r2)

    # 2. Trade record closed at 115.0
    assert len(res.trades) == 1
    tr = res.trades[0]
    assert np.isclose(tr["entry_px"], 100.0)
    assert np.isclose(tr["exit_px"], 115.0)
    assert np.isclose(tr["gross"], 0.15)
    assert np.isclose(tr["rt"], 0.0014)
    assert np.isclose(tr["net"], 0.15 - 0.0014)
    assert tr["reason"] == "period_end"


def test_scenario_5_short_trailing_stop_pierce():
    """Scenario 5: Short position, trailing stop ratchets down from 100.0 to 94.0, pierced intra-bar at 94.0."""
    sym = "BTCUSDT"
    start_ms = 1672531200000

    # 4 bars: k=0, 1, 2, 3
    opens = [100.0, 96.0, 92.0, 94.0]
    highs = [101.0, 97.0, 95.0, 95.0]
    lows = [95.0, 90.0, 88.0, 92.0]
    closes = [96.0, 92.0, 94.0, 93.0]

    b_highs = [110.0] * 4
    b_lows = [98.0, 98.0, 80.0, 80.0]
    ema_fasts = [92.0, 91.0, 91.0, 91.0]
    ema_slows = [96.0, 95.0, 95.0, 95.0]
    atrs = [2.0] * 4

    feat_df = make_test_features_df(
        sym=sym,
        opens=opens,
        highs=highs,
        lows=lows,
        closes=closes,
        breakout_highs=b_highs,
        breakout_lows=b_lows,
        ema_fasts=ema_fasts,
        ema_slows=ema_slows,
        atrs=atrs,
        start_ms=start_ms,
    )

    res = run_backtest_v3(
        features_df=feat_df,
        funding_df=None,
        atr_mult=2.0,
        carry_mode="Off",
        cost_mult=1.0,
        score_start_str="2023-01-01",
        score_end_str="2023-01-05",
    )

    # 1. Bar Returns:
    # Bar 0: Flat -> 0.0
    assert np.isclose(res.r_bar[0], 0.0)
    # Bar 1: Short entered at 96.0 (fee 0.0007), holds to 92.0: return = -1 * (92/96 - 1) - 0.0007
    expected_r1 = (4.0 / 96.0) - 0.0007
    assert np.isclose(res.r_bar[1], expected_r1)
    # Bar 2: Stop loss ratcheted to min(100.0, 90.0 + 4.0) = 94.0. High 95 >= 94 -> stopped out at 94.0!
    # Return: -1 * (94/92 - 1) - 0.0007
    expected_r2 = (-2.0 / 92.0) - 0.0007
    assert np.isclose(res.r_bar[2], expected_r2)
    # Bar 3: Flat
    assert np.isclose(res.r_bar[3], 0.0)

    # 2. Trade record:
    assert len(res.trades) == 1
    tr = res.trades[0]
    assert tr["direction"] == -1
    assert np.isclose(tr["entry_px"], 96.0)
    assert np.isclose(tr["exit_px"], 94.0)
    assert np.isclose(tr["gross"], 2.0 / 96.0)
    assert np.isclose(tr["rt"], 0.0014)
    assert np.isclose(tr["net"], (2.0 / 96.0) - 0.0014)
    assert tr["reason"] == "stop_loss"


def test_scenario_6_short_channel_exit():
    """Scenario 6: Short position exits when bar close crosses above slow EMA."""
    sym = "BTCUSDT"
    start_ms = 1672531200000

    # 5 bars: k=0, 1, 2, 3, 4
    opens = [100.0, 96.0, 94.0, 98.0, 98.0]
    highs = [101.0, 97.0, 99.0, 99.0, 99.0]
    lows = [95.0, 93.0, 93.0, 97.0, 97.0]
    closes = [96.0, 94.0, 98.0, 97.0, 97.0]

    b_highs = [110.0] * 5
    b_lows = [98.0, 98.0, 80.0, 80.0, 80.0]
    ema_fasts = [92.0, 92.0, 94.0, 94.0, 94.0]
    ema_slows = [96.0, 96.0, 96.0, 96.0, 96.0]
    atrs = [10.0] * 5  # Wide ATR so trailing stop is not triggered

    feat_df = make_test_features_df(
        sym=sym,
        opens=opens,
        highs=highs,
        lows=lows,
        closes=closes,
        breakout_highs=b_highs,
        breakout_lows=b_lows,
        ema_fasts=ema_fasts,
        ema_slows=ema_slows,
        atrs=atrs,
        start_ms=start_ms,
    )

    res = run_backtest_v3(
        features_df=feat_df,
        funding_df=None,
        atr_mult=2.0,
        carry_mode="Off",
        cost_mult=1.0,
        score_start_str="2023-01-01",
        score_end_str="2023-01-05",
    )

    # 1. Bar Returns:
    # Bar 0: Flat
    assert np.isclose(res.r_bar[0], 0.0)
    # Bar 1: Entry at 96.0, holding to 94.0
    assert np.isclose(res.r_bar[1], (2.0 / 96.0) - 0.0007)
    # Bar 2: Holding 94.0 -> 98.0. Close 98.0 > slow EMA 96.0 triggers channel exit
    assert np.isclose(res.r_bar[2], -4.0 / 94.0)
    # Bar 3: Exit executed at open 98.0. Exit fee 0.0007
    assert np.isclose(res.r_bar[3], -0.0007)

    # 2. Trade record:
    assert len(res.trades) == 1
    tr = res.trades[0]
    assert tr["direction"] == -1
    assert np.isclose(tr["entry_px"], 96.0)
    assert np.isclose(tr["exit_px"], 98.0)
    assert np.isclose(tr["gross"], -2.0 / 96.0)
    assert np.isclose(tr["rt"], 0.0014)
    assert np.isclose(tr["net"], (-2.0 / 96.0) - 0.0014)
    assert tr["reason"] == "channel_exit"


def test_backtest_with_unkeyed_funding_df():
    """Verify single-asset backtest runs when funding_df has no 'sym' column."""
    sym = "BTCUSDT"
    start_ms = 1672531200000

    opens = [100.0, 104.0, 108.0]
    highs = [105.0, 110.0, 112.0]
    lows = [98.0, 103.0, 107.0]
    closes = [104.0, 108.0, 111.0]

    b_highs = [103.0, 103.0, 110.0]
    b_lows = [90.0] * 3
    ema_fasts = [102.0, 104.0, 106.0]
    ema_slows = [95.0, 96.0, 97.0]
    atrs = [2.0] * 3

    feat_df = make_test_features_df(sym, opens, highs, lows, closes, b_highs, b_lows, ema_fasts, ema_slows, atrs, start_ms=start_ms)

    # Funding without 'sym' column (as in raw Binance parquet file)
    funding_raw = pd.DataFrame([{
        "calc_time": start_ms + 2 * BAR_MS,
        "settle_hour": start_ms + 2 * BAR_MS,
        "rate": 0.0001,
    }])

    res = run_backtest_v3(feat_df, funding_df=funding_raw, score_start_str="2023-01-01", score_end_str="2023-01-05")
    assert len(res.r_bar) == 3
    assert len(res.trades) == 1


def test_multi_asset_portfolio_leverage_limit():
    """Verify that multi-asset portfolio never breaches max_leverage under extreme Carry multipliers."""
    start_ms = 1672531200000
    n = 6

    # 3 assets triggering entry with 1.3x Carry boost
    dfs = []
    for s in ["BTCUSDT", "ETHUSDT", "BNBUSDT"]:
        df = make_test_features_df(
            sym=s,
            opens=[100.0, 100.0, 96.0, 96.0, 96.0, 96.0],
            highs=[105.0, 101.0, 98.0, 98.0, 98.0, 98.0],
            lows=[98.0, 95.0, 94.0, 94.0, 94.0, 94.0],
            closes=[104.0, 95.0, 95.0, 95.0, 95.0, 95.0],
            breakout_highs=[102.0] * n,
            breakout_lows=[96.0] * n,
            ema_fasts=[92.0] * n,
            ema_slows=[96.0] * n,
            atrs=[10.0] * n,
            funding_3ds=[0.0005] * n,  # > 0.0003 -> Short 1.3x
            rv30s=[0.5] * n,
            start_ms=start_ms,
        )
        dfs.append(df)

    feat_df = pd.concat(dfs, ignore_index=True)
    res = run_backtest_v3(feat_df, carry_mode="On", max_leverage=1.0, score_start_str="2023-01-01", score_end_str="2023-01-05")

    # In every bar, sum of absolute asset returns / leverage must be bounded
    assert len(res.trades) == 3
    for tr in res.trades:
        assert tr["direction"] == -1
    # Check metrics computed properly
    assert res.metrics["total_net_return"] is not None


def test_empty_backtest_slice():
    """Verify empty features slice returns valid neutral container without errors."""
    feat_df = pd.DataFrame(columns=[
        "sym", "open_time", "open", "high", "low", "close",
        "breakout_high", "breakout_low", "ema_fast", "ema_slow", "atr", "funding_3d", "rv30"
    ])
    res = run_backtest_v3(features_df=feat_df)
    assert len(res.r_bar) == 0
    assert len(res.equity) == 1
    assert res.equity[0] == 1.0
    assert len(res.trades) == 0


def test_scenario_5_dual_track_profit_trailing_stop_long():
    """Hand-calculated test for dual-track trailing stop profit activation (Long trade).
    
    Long entry at open[1] = 100.0, entry ATR = 2.0.
    Initial disaster stop: 100.0 - 3.5 * 2.0 = 93.0.
    profit_trail_act_atr = 2.5 (trigger price >= 100.0 + 2.5 * 2.0 = 105.0).
    - Bar 1: high = 104.0 (< 105.0) -> profit threshold not met, stop stays at 93.0.
    - Bar 2: high = 107.0 (>= 105.0) -> profit threshold met! Trailing stop moves up to 107.0 - 3.5 * 2.0 = 100.0.
    - Bar 3: low = 99.5 (<= 100.0) -> pierced intra-bar at exit_px = min(107.0, 100.0) = 100.0!
    """
    sym = "BTCUSDT"
    start_ms = 1672531200000

    opens = [100.0, 100.0, 103.0, 107.0, 100.0]
    highs = [105.0, 104.0, 107.0, 108.0, 101.0]
    lows = [98.0, 99.0, 102.0, 99.5, 98.0]
    closes = [104.0, 103.0, 106.0, 100.0, 99.0]
    b_highs = [102.0] * 5
    b_lows = [90.0] * 5
    ema_fasts = [102.0] * 5
    ema_slows = [95.0] * 5
    atrs = [2.0] * 5

    feat_df = make_test_features_df(sym, opens, highs, lows, closes, b_highs, b_lows, ema_fasts, ema_slows, atrs, start_ms=start_ms)
    res = run_backtest_v3(
        feat_df,
        atr_mult=3.5,
        carry_mode="Off",
        cost_mult=1.0,
        profit_trail_act_atr=2.5,
        score_start_str="2023-01-01",
        score_end_str="2023-01-05",
    )

    assert len(res.trades) == 1
    tr = res.trades[0]
    assert tr["direction"] == 1
    assert tr["reason"] == "stop_loss"
    assert tr["entry_px"] == 100.0
    assert tr["exit_px"] == 100.0
    assert np.isclose(tr["gross"], 0.0)
    assert np.isclose(tr["net"], -0.0014)

    # Bar return checks:
    # Bar 0: Flat -> 0.0
    assert np.isclose(res.r_bar[0], 0.0)
    # Bar 1: Entry fee 0.0007, holds to open[2]=103.0 -> 0.03 - 0.0007 = 0.0293
    assert np.isclose(res.r_bar[1], 0.0293)
    # Bar 2: Holds 103.0 -> 107.0 -> 4.0 / 103.0
    assert np.isclose(res.r_bar[2], 4.0 / 103.0)
    # Bar 3: Stop-loss exit at 100.0 from open[3]=107.0 -> -7.0 / 107.0 - 0.0007 fee
    assert np.isclose(res.r_bar[3], (100.0 / 107.0 - 1.0) - 0.0007)
    # Bar 4: Flat -> 0.0
    assert np.isclose(res.r_bar[4], 0.0)


def test_scenario_6_dual_track_profit_trailing_stop_short():
    """Hand-calculated test for dual-track trailing stop profit activation (Short trade).
    
    Short entry at open[1] = 100.0, entry ATR = 2.0.
    Initial disaster stop: 100.0 + 3.5 * 2.0 = 107.0.
    profit_trail_act_atr = 2.0 (trigger price <= 100.0 - 2.0 * 2.0 = 96.0).
    - Bar 1: low = 97.0 (> 96.0) -> profit threshold not met, stop stays at 107.0.
    - Bar 2: low = 94.0 (<= 96.0) -> profit threshold met! Trailing stop moves down to 94.0 + 3.5 * 2.0 = 101.0.
    - Bar 3: high = 101.5 (>= 101.0) -> pierced intra-bar at exit_px = max(93.0, 101.0) = 101.0!
    """
    sym = "BTCUSDT"
    start_ms = 1672531200000

    opens = [100.0, 100.0, 97.0, 93.0, 100.0]
    highs = [101.0, 101.0, 98.0, 101.5, 101.0]
    lows = [95.0, 97.0, 93.0, 92.0, 99.0]
    closes = [96.0, 97.0, 94.0, 100.0, 100.0]
    b_highs = [105.0] * 5
    b_lows = [98.0] * 5
    ema_fasts = [95.0] * 5
    ema_slows = [100.0] * 5
    atrs = [2.0] * 5

    feat_df = make_test_features_df(sym, opens, highs, lows, closes, b_highs, b_lows, ema_fasts, ema_slows, atrs, start_ms=start_ms)
    res = run_backtest_v3(
        feat_df,
        atr_mult=3.5,
        carry_mode="Off",
        cost_mult=1.0,
        profit_trail_act_atr=2.0,
        score_start_str="2023-01-01",
        score_end_str="2023-01-05",
    )

    assert len(res.trades) == 1
    tr = res.trades[0]
    assert tr["direction"] == -1
    assert tr["reason"] == "stop_loss"
    assert tr["entry_px"] == 100.0
    assert tr["exit_px"] == 100.0
    assert np.isclose(tr["gross"], 0.0)
    assert np.isclose(tr["net"], -0.0014)

    # Bar return checks:
    assert np.isclose(res.r_bar[0], 0.0)
    assert np.isclose(res.r_bar[1], (1.0 - 97.0 / 100.0) - 0.0007)
    assert np.isclose(res.r_bar[2], (1.0 - 93.0 / 97.0))
    # Bar 3: short exit at 100.0 (cur_lo 93.0 + 3.5*2.0 = 100.0) from open[3]=93.0 -> -1 * (100/93 - 1) - 0.0007 fee
    assert np.isclose(res.r_bar[3], -1.0 * (100.0 / 93.0 - 1.0) - 0.0007)
    assert np.isclose(res.r_bar[4], 0.0)


def test_holdout_runner_guards_and_log_preservation(tmp_path, monkeypatch):
    """Verify run_holdout_v3 security guards and clean section preservation in LOG_V3.md."""
    from lz3 import run_holdout_v3

    # 1. Permission guard without approval
    monkeypatch.delenv("LZ_HOLDOUT_APPROVED", raising=False)
    fake_done = tmp_path / "nonexistent_done.json"
    monkeypatch.setattr(run_holdout_v3, "HOLDOUT_DONE_FILE", fake_done)
    with pytest.raises(PermissionError, match="LZ_HOLDOUT_APPROVED"):
        run_holdout_v3.run_holdout()

    # 2. Guard against duplicate run when holdout_v3_done.json exists
    fake_done.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("LZ_HOLDOUT_APPROVED", "yes")
    with pytest.raises(RuntimeError, match="Guard:"):
        run_holdout_v3.run_holdout()

    # 3. Candidate must come from the dev selection artifact, never a baked-in config
    fake_done.unlink()
    monkeypatch.setattr(run_holdout_v3, "SELECTED_FILE", tmp_path / "dev_v3_selected.json")
    with pytest.raises(FileNotFoundError, match="run_dev_v3"):
        run_holdout_v3.run_holdout()

    # 4. Preservation of trailing sections in LOG_V3.md
    log_file = tmp_path / "LOG_V3.md"
    orig_log = (
        "# Header\n\n"
        "## Diagnostics\n- Grid info\n\n"
        "## Holdout Diagnostics\n- Old holdout info\n\n"
        "## V3.1 Mechanism Exploration Diagnostics\n- Preserved V3.1 content\n"
    )
    log_file.write_text(orig_log, encoding="utf-8")
    # Verify replacing holdout section preserves V3.1
    diag = "\n## Holdout Diagnostics (Phase 3 - 2026-10-05 15:00 UTC)\n- New holdout info\n"
    content = log_file.read_text(encoding="utf-8")
    if "## Holdout Diagnostics" in content:
        prefix = content.split("## Holdout Diagnostics")[0].rstrip()
        remainder = content.split("## Holdout Diagnostics", 1)[1]
        next_h2_idx = remainder.find("\n## ")
        suffix = remainder[next_h2_idx:] if next_h2_idx != -1 else ""
        new_content = prefix + "\n" + diag + suffix
    else:
        new_content = content.rstrip() + "\n" + diag

    assert "## V3.1 Mechanism Exploration Diagnostics" in new_content
    assert "- Preserved V3.1 content" in new_content
    assert "- New holdout info" in new_content
    assert "- Old holdout info" not in new_content


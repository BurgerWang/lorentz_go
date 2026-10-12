"""Unit tests for backtest simulation hand-calculating 4 required scenarios."""

import numpy as np
import pandas as pd
import pytest

from lz.config import BAR_MS
from lz.backtest import run_portfolio_backtest


def make_features_df(sym: str, opens: list[float], closes: list[float], start_ms: int = 1672531200000):
    n = len(opens)
    times = [start_ms + i * BAR_MS for i in range(n)]
    return pd.DataFrame({
        "sym": [sym] * n,
        "open_time": times,
        "open": opens,
        "close": closes,
        "rv30": [0.1] * n,
    })


def test_scenario_1_six_bar_trade_with_funding():
    """Scenario 1: A 6-bar long trade with 1 funding settlement.
    
    Verifies Section 7.2 formula:
      r_bar(k) = pos(k)*w*(open[k+1]/open[k]-1) - |delta|*cost - funding
    """
    sym = "BTCUSDT"
    start_ms = 1672531200000  # 2023-01-01 00:00 UTC
    opens = [100.0] * 10
    closes = [100.0] * 10
    opens[1] = 100.0  # entry at open[1]
    opens[7] = 106.0  # exit at open[7] (6 bars of holding: bars 1,2,3,4,5,6)
    closes[6] = 106.0

    feat_df = make_features_df(sym, opens, closes, start_ms=start_ms)

    # Signal +1 at bar 0, 0 elsewhere
    sig_rows = [{"sym": sym, "open_time": feat_df.iloc[i]["open_time"], "signal": 1.0 if i == 0 else 0.0} for i in range(10)]
    sig_df = pd.DataFrame(sig_rows)

    # Funding settlement at settle_hour = start_ms + 2*BAR_MS (during bar 1, i.e. open[1] < sh <= open[2])
    # rate = 0.0002
    funding_df = pd.DataFrame([{
        "sym": sym,
        "calc_time": start_ms + 2 * BAR_MS,
        "settle_hour": start_ms + 2 * BAR_MS,
        "rate": 0.0002,
    }])

    equity, r_bar, trades, per_asset = run_portfolio_backtest(
        features_df=feat_df,
        signals_df=sig_df,
        funding_df=funding_df,
        cost_mult=1.0,
        score_start_str="2023-01-01",
        score_end_str="2023-01-05",
    )

    # 1. Bar return verification:
    # Bar 0: flat -> r_bar = 0.0
    assert np.isclose(r_bar[0], 0.0)
    # Bar 1: entry at open[1] (fee = 0.0007), price return 100->100, funding 0.0002 -> -0.0009
    assert np.isclose(r_bar[1], -0.0009)
    # Bars 2-5: holding flat price -> 0.0
    for i in range(2, 6):
        assert np.isclose(r_bar[i], 0.0)
    # Bar 6: holding 100->106 (+6%), no fee at open[6] -> +0.06
    assert np.isclose(r_bar[6], 0.06)
    # Bar 7: exit at open[7] (fee = 0.0007), flat price -> -0.0007
    assert np.isclose(r_bar[7], -0.0007)
    # Bars 8-9: flat -> 0.0
    assert np.isclose(r_bar[8], 0.0)
    assert np.isclose(r_bar[9], 0.0)

    # 2. Equity curve verification
    expected_equity = np.cumprod(np.concatenate([[1.0], 1.0 + r_bar]))
    assert np.allclose(equity, expected_equity)

    # 3. Trade record verification
    assert len(trades) == 1
    tr = trades[0]
    assert np.isclose(tr["entry_px"], 100.0)
    assert np.isclose(tr["exit_px"], 106.0)
    assert np.isclose(tr["gross"], 0.06)
    assert np.isclose(tr["funding_sum"], 0.0002)
    assert np.isclose(tr["rt"], 0.0014)
    # net = gross - rt - funding_sum = 0.06 - 0.0014 - 0.0002 = 0.0584
    assert np.isclose(tr["net"], 0.0584)


def test_scenario_2_reversal():
    """Scenario 2: Reversal (flip) from long to short at open[4]."""
    sym = "BTCUSDT"
    start_ms = 1672531200000
    opens = [100.0] * 12
    closes = [100.0] * 12
    opens[1] = 100.0  # entry long
    opens[4] = 103.0  # flip to short
    closes[3] = 103.0

    feat_df = make_features_df(sym, opens, closes, start_ms=start_ms)

    # Long at bar 0, flip short at bar 3
    sig_rows = []
    for i in range(12):
        s = 0.0
        if i == 0:
            s = 1.0
        elif i == 3:
            s = -1.0
        sig_rows.append({"sym": sym, "open_time": feat_df.iloc[i]["open_time"], "signal": s})
    sig_df = pd.DataFrame(sig_rows)

    equity, r_bar, trades, per_asset = run_portfolio_backtest(
        features_df=feat_df,
        signals_df=sig_df,
        cost_mult=1.0,
        score_start_str="2023-01-01",
        score_end_str="2023-01-05",
    )

    # Bar 0: 0
    assert np.isclose(r_bar[0], 0.0)
    # Bar 1: enters long at open[1], fee 0.0007 -> -0.0007
    assert np.isclose(r_bar[1], -0.0007)
    # Bar 3: price 100->103 -> +0.03
    assert np.isclose(r_bar[3], 0.03)
    # Bar 4: reversal at open[4] -> delta = 2w, cost = 2*0.0007 = 0.0014
    # short from 103 to 100 -> -1*(100/103 - 1) = 3/103
    expected_bar4 = (3.0 / 103.0) - 0.0014
    assert np.isclose(r_bar[4], expected_bar4)

    # First trade was closed at bar 4
    assert len(trades) >= 1
    tr1 = trades[0]
    assert tr1["direction"] == 1.0
    assert np.isclose(tr1["entry_px"], 100.0)
    assert np.isclose(tr1["exit_px"], 103.0)
    assert np.isclose(tr1["gross"], 0.03)
    assert np.isclose(tr1["net"], 0.03 - 0.0014)


def test_scenario_3_continuation_no_cost():
    """Scenario 3: Continuation at scheduled exit (no trade closed, no cost paid)."""
    sym = "BTCUSDT"
    start_ms = 1672531200000
    opens = [100.0] * 16
    closes = [100.0] * 16
    opens[1] = 100.0
    for idx in range(7, 13):
        opens[idx] = 105.0
        closes[idx - 1] = 105.0
    opens[13] = 110.0  # final exit
    closes[12] = 110.0

    feat_df = make_features_df(sym, opens, closes, start_ms=start_ms)

    # Long at bar 0, continue at bar 6 (governs open[7]), exit at bar 12
    sig_rows = []
    for i in range(16):
        s = 0.0
        if i == 0 or i == 6:
            s = 1.0
        sig_rows.append({"sym": sym, "open_time": feat_df.iloc[i]["open_time"], "signal": s})
    sig_df = pd.DataFrame(sig_rows)

    equity, r_bar, trades, per_asset = run_portfolio_backtest(
        features_df=feat_df,
        signals_df=sig_df,
        cost_mult=1.0,
        score_start_str="2023-01-01",
        score_end_str="2023-01-05",
    )

    # Bar 7 is continuation: no fee paid at open[7], price 105->105 -> r_bar[7] = 0.0
    assert np.isclose(r_bar[7], 0.0)
    # Bar 13 is exit: fee 0.0007 paid at open[13] -> r_bar[13] = -0.0007
    assert np.isclose(r_bar[13], -0.0007)

    # Should only be 1 closed trade at bar 13
    assert len(trades) == 1
    tr = trades[0]
    assert np.isclose(tr["entry_px"], 100.0)
    assert np.isclose(tr["exit_px"], 110.0)
    assert np.isclose(tr["gross"], 0.10)
    assert np.isclose(tr["net"], 0.10 - 0.0014)


def test_scenario_4_end_of_period_force_close():
    """Scenario 4: Force close at end of scoring period at close[T-1]."""
    sym = "BTCUSDT"
    start_ms = 1672531200000
    # 5 bars only
    opens = [100.0, 100.0, 101.0, 102.0, 103.0]
    closes = [100.0, 101.0, 102.0, 103.0, 105.0]

    feat_df = make_features_df(sym, opens, closes, start_ms=start_ms)

    sig_rows = [
        {"sym": sym, "open_time": feat_df.iloc[0]["open_time"], "signal": 1.0},
        {"sym": sym, "open_time": feat_df.iloc[1]["open_time"], "signal": 0.0},
        {"sym": sym, "open_time": feat_df.iloc[2]["open_time"], "signal": 0.0},
        {"sym": sym, "open_time": feat_df.iloc[3]["open_time"], "signal": 0.0},
        {"sym": sym, "open_time": feat_df.iloc[4]["open_time"], "signal": 0.0},
    ]
    sig_df = pd.DataFrame(sig_rows)

    end_str = pd.to_datetime(start_ms + 5 * BAR_MS, unit="ms", utc=True).strftime("%Y-%m-%d %H:%M")

    equity, r_bar, trades, per_asset = run_portfolio_backtest(
        features_df=feat_df,
        signals_df=sig_df,
        cost_mult=1.0,
        score_start_str="2023-01-01",
        score_end_str=end_str,
    )

    # Last bar (k=4): open-to-close return = 105/103 - 1, minus exit cost 0.0007
    expected_bar4 = (105.0 / 103.0 - 1.0) - 0.0007
    assert np.isclose(r_bar[4], expected_bar4)

    assert len(trades) == 1
    tr = trades[0]
    assert np.isclose(tr["entry_px"], 100.0)
    assert np.isclose(tr["exit_px"], 105.0)  # closed at close of last bar
    assert np.isclose(tr["gross"], 0.05)
    assert np.isclose(tr["net"], 0.05 - 0.0014)

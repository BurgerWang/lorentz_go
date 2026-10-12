"""Unit tests for v5 market-neutral backtest engine with exact hand-calculated reconciliation.

Verifies the 4 mandatory scenarios:
1. Multi-asset market 1:1 hedging against market crash (perfect Beta neutrality: gross return == 0.000000).
2. Short leg stable funding fee carry cash flow harvest (exact hand calculation of funding income).
3. Rebalance turnover friction accurate deduction (initial open, zero friction between rebalances, rotation cost).
4. Period-end forced liquidation at final bar close (positions flattened, liquidation friction charged).
5. Full dev cycle smoke test (7 dev assets, 2023-2024, strict dollar neutrality and valid equity curve).
"""

import numpy as np
import pandas as pd
import pytest

from lz5.config import BAR_MS, DEV_ASSETS, GRID_CONFIGS, load_asset_v5
from lz5.backtest_v5 import (
    run_backtest_v5,
    calc_dsr,
    precompute_bar_funding,
)


def make_synthetic_asset_df(
    sym: str,
    opens: list[float],
    closes: list[float],
    moms: list[float],
    carrys: list[float],
    rv30s: list[float],
    bar_fundings: list[float] | None = None,
    start_ms: int = 1672531200000,
) -> pd.DataFrame:
    """Helper to build a synthetic bar DataFrame for unit testing."""
    n = len(opens)
    bf = bar_fundings if bar_fundings is not None else [0.0] * n
    return pd.DataFrame({
        "sym": [sym] * n,
        "open_time": [start_ms + i * BAR_MS for i in range(n)],
        "open": opens,
        "close": closes,
        "mom": moms,
        "carry": carrys,
        "rv30": rv30s,
        "bar_funding": bf,
    })


# ---------------------------------------------------------------------------
# 1. Market Crash 1:1 Hedging Hand Calculation
# ---------------------------------------------------------------------------

def test_market_crash_1_to_1_dollar_neutral_hedge():
    """Verify that under an extreme market collapse (-20% on all coins),
    1:1 long-short Dollar neutral portfolio gross price return is STRICTLY 0.00000000."""
    # 4 assets: BTC, ETH (Long, w=+0.25 each), DOGE, ADA (Short, w=-0.25 each)
    # 3 bars: bar 0 normal, bar 1 crashes -20%, bar 2 ends
    # Prices:
    # Bar 0: open=100, close=100
    # Bar 1: open=100, close=80 (crashes -20%)
    # Bar 2: open=80, close=80
    opens = [100.0, 100.0, 80.0]
    closes = [100.0, 80.0, 80.0]

    # Scores engineered so BTC & ETH are Top 2, DOGE & ADA are Bottom 2
    # Equal rv30 -> equal weights +0.25, +0.25, -0.25, -0.25
    rv30 = [0.20] * 3
    data = {
        "BTCUSDT": make_synthetic_asset_df("BTCUSDT", opens, closes, [0.30]*3, [0.0]*3, rv30),
        "ETHUSDT": make_synthetic_asset_df("ETHUSDT", opens, closes, [0.20]*3, [0.0]*3, rv30),
        "DOGEUSDT": make_synthetic_asset_df("DOGEUSDT", opens, closes, [-0.20]*3, [0.0]*3, rv30),
        "ADAUSDT": make_synthetic_asset_df("ADAUSDT", opens, closes, [-0.30]*3, [0.0]*3, rv30),
    }

    # Run with cost_mult=0.0 to isolate pure gross price return
    res = run_backtest_v5(
        data,
        t_reb=6,
        cost_mult=0.0,
        force_period_end_liquidation=False,
    )

    # In bar 0: holding from open(0)=100 to open(1)=100 -> price return = 0.0
    assert np.isclose(res.gross_r_bar[0], 0.0, atol=1e-12)

    # In bar 1: holding from open(1)=100 to open(2)=80 -> price return = -20%
    # Long leg: (+0.25 * -0.20) + (+0.25 * -0.20) = -0.10
    # Short leg: (-0.25 * -0.20) + (-0.25 * -0.20) = +0.10
    # Net: -0.10 + 0.10 = 0.00000000!
    assert np.isclose(res.gross_r_bar[1], 0.0, atol=1e-12)
    assert np.isclose(res.r_bar[1], 0.0, atol=1e-12)
    assert np.isclose(res.equity[2], 1.0, atol=1e-12)


# ---------------------------------------------------------------------------
# 2. Short Funding Carry Cash Flow Hand Calculation
# ---------------------------------------------------------------------------

def test_short_funding_carry_cash_flow():
    """Verify that short positions stably harvest funding fee cash flow.

    DOGE & ADA funding = +0.0010 (10 bps settled during bar).
    Short weight = -0.25 each.
    Funding deduction: (-0.25 * 0.0010) + (-0.25 * 0.0010) = -0.00050.
    Subtracting funding deduction yields: r_bar = -(-0.00050) = +0.00050 (+5 bps).
    """
    # Flat prices (0 price change)
    opens = [100.0, 100.0, 100.0]
    closes = [100.0, 100.0, 100.0]
    rv30 = [0.20] * 3

    # DOGE and ADA funding = +0.0010 on bar 0
    bf_doge = [0.0010, 0.0010, 0.0010]
    bf_ada = [0.0010, 0.0010, 0.0010]
    bf_zero = [0.0, 0.0, 0.0]

    data = {
        "BTCUSDT": make_synthetic_asset_df("BTCUSDT", opens, closes, [0.30]*3, [0.0]*3, rv30, bf_zero),
        "ETHUSDT": make_synthetic_asset_df("ETHUSDT", opens, closes, [0.20]*3, [0.0]*3, rv30, bf_zero),
        "DOGEUSDT": make_synthetic_asset_df("DOGEUSDT", opens, closes, [-0.20]*3, [0.0]*3, rv30, bf_doge),
        "ADAUSDT": make_synthetic_asset_df("ADAUSDT", opens, closes, [-0.30]*3, [0.0]*3, rv30, bf_ada),
    }

    res = run_backtest_v5(
        data,
        t_reb=6,
        cost_mult=0.0,  # zero friction to isolate funding
        force_period_end_liquidation=False,
    )

    # In bar 0: price return is 0, funding return is +0.00050
    expected_funding_gain = 0.25 * 0.0010 + 0.25 * 0.0010  # +0.00050
    assert np.isclose(res.funding_bar[0], -expected_funding_gain, atol=1e-12)
    assert np.isclose(res.r_bar[0], expected_funding_gain, atol=1e-12)
    assert np.isclose(res.equity[1], 1.0 + expected_funding_gain, atol=1e-12)

    # Total net funding metric must be positive (revenue)
    assert np.isclose(res.metrics["total_funding"], expected_funding_gain * 3, atol=1e-12)


# ---------------------------------------------------------------------------
# 3. Turnover Friction Accurate Deduction
# ---------------------------------------------------------------------------

def test_turnover_friction_hand_calculation():
    """Verify exact hand calculation of transaction friction:
    1. Initial opening cost at bar 0.
    2. Zero turnover cost between rebalances (bars 1..5 when t_reb=6).
    3. Rebalance cost at bar 6 when substituting one asset.
    4. 2.0x cost multiplier scaling.
    """
    n = 10
    opens = [100.0] * n
    closes = [100.0] * n
    rv30 = [0.20] * n

    # At bar 0..4 (indices 0..4): DOGE is in bottom 2.
    # At bar 5 close (index 5): DOGE rises out of bottom 2, LINK enters bottom 2.
    # At bar 6 open: rebalance executes based on bar 5 close state!
    mom_doge = [-0.20] * 5 + [0.00] * 5    # rises out of bottom 2 at bar 5 close
    mom_link = [0.00] * 5 + [-0.25] * 5    # enters bottom 2 at bar 5 close

    data = {
        "BTCUSDT": make_synthetic_asset_df("BTCUSDT", opens, closes, [0.30]*n, [0.0]*n, rv30),
        "ETHUSDT": make_synthetic_asset_df("ETHUSDT", opens, closes, [0.20]*n, [0.0]*n, rv30),
        "ADAUSDT": make_synthetic_asset_df("ADAUSDT", opens, closes, [-0.30]*n, [0.0]*n, rv30),
        "DOGEUSDT": make_synthetic_asset_df("DOGEUSDT", opens, closes, mom_doge, [0.0]*n, rv30),
        "LINKUSDT": make_synthetic_asset_df("LINKUSDT", opens, closes, mom_link, [0.0]*n, rv30),
    }

    # Test 1.0x cost
    res = run_backtest_v5(
        data,
        t_reb=6,
        cost_mult=1.0,
        force_period_end_liquidation=False,
    )

    # Initial open friction at bar 0:
    # BTC: 0.25 * (5.0 + 2.0)e-4 = 0.25 * 0.0007 = 0.000175
    # ETH: 0.25 * (5.0 + 2.0)e-4 = 0.25 * 0.0007 = 0.000175
    # ADA: 0.25 * (5.0 + 4.0)e-4 = 0.25 * 0.0009 = 0.000225
    # DOGE: 0.25 * (5.0 + 4.0)e-4 = 0.25 * 0.0009 = 0.000225
    # Total initial cost = 0.000175 + 0.000175 + 0.000225 + 0.000225 = 0.000800
    expected_init_cost = 0.000800
    assert np.isclose(res.cost_bar[0], expected_init_cost, atol=1e-12)

    # Bars 1 through 5: Non-rebalance bars -> cost MUST be strictly 0.0
    for k in range(1, 6):
        assert np.isclose(res.cost_bar[k], 0.0, atol=1e-12)

    # Bar 6: Rebalance occurs. DOGE drops out (Delta w = 0.25), LINK enters (Delta w = 0.25)
    # DOGE friction = 0.25 * 0.0009 = 0.000225
    # LINK friction = 0.25 * 0.0009 = 0.000225
    # Rebalance cost = 0.000450
    expected_reb_cost = 0.000450
    assert np.isclose(res.cost_bar[6], expected_reb_cost, atol=1e-12)

    # Test 2.0x cost multiplier
    res_2x = run_backtest_v5(
        data,
        t_reb=6,
        cost_mult=2.0,
        force_period_end_liquidation=False,
    )
    assert np.isclose(res_2x.cost_bar[0], expected_init_cost * 2.0, atol=1e-12)
    assert np.isclose(res_2x.cost_bar[6], expected_reb_cost * 2.0, atol=1e-12)


# ---------------------------------------------------------------------------
# 4. Period-End Forced Liquidation Hand Calculation
# ---------------------------------------------------------------------------

def test_period_end_forced_liquidation():
    """Verify that on the final bar close, all positions are flattened to 0
    and closing friction is accurately deducted."""
    n = 4
    opens = [100.0] * n
    closes = [100.0] * n
    rv30 = [0.20] * n

    data = {
        "BTCUSDT": make_synthetic_asset_df("BTCUSDT", opens, closes, [0.30]*n, [0.0]*n, rv30),
        "ETHUSDT": make_synthetic_asset_df("ETHUSDT", opens, closes, [0.20]*n, [0.0]*n, rv30),
        "ADAUSDT": make_synthetic_asset_df("ADAUSDT", opens, closes, [-0.30]*n, [0.0]*n, rv30),
        "DOGEUSDT": make_synthetic_asset_df("DOGEUSDT", opens, closes, [-0.20]*n, [0.0]*n, rv30),
    }

    # Run with forced liquidation enabled
    res = run_backtest_v5(
        data,
        t_reb=6,
        cost_mult=1.0,
        force_period_end_liquidation=True,
    )

    # Last bar is k = 3.
    # It must have liquidation cost = 0.000800 deducted
    # Opening cost at k=0 was 0.000800, closing cost at k=3 is 0.000800
    expected_liq_cost = 0.000800
    assert np.isclose(res.cost_bar[3], expected_liq_cost, atol=1e-12)
    assert np.isclose(res.metrics["total_cost"], 0.000800 * 2, atol=1e-12)
    assert np.isclose(res.metrics["turnover_total"], 2.0, atol=1e-12)

    # Without liquidation
    res_no_liq = run_backtest_v5(
        data,
        t_reb=6,
        cost_mult=1.0,
        force_period_end_liquidation=False,
    )
    assert np.isclose(res_no_liq.cost_bar[3], 0.0, atol=1e-12)
    assert np.isclose(res_no_liq.metrics["total_cost"], 0.000800, atol=1e-12)
    assert np.isclose(res_no_liq.metrics["turnover_total"], 1.0, atol=1e-12)


# ---------------------------------------------------------------------------
# 5. Full Cycle Dev Backtest Sanity
# ---------------------------------------------------------------------------

@pytest.mark.needs_market_data
def test_full_cycle_dev_backtest_sanity():
    """Run a realistic 2023-2024 dev backtest across all 7 dev assets and verify:
    - 4386 bars evaluated.
    - Every bar has sum(w) == 0.0000 (dollar neutral).
    - Every bar has sum(|w|) == 1.0000 (gross leverage).
    - Valid equity curve without NaNs.
    """
    raw = {s: load_asset_v5(s, split="dev") for s in DEV_ASSETS}
    funding_combined = pd.concat([d["funding"] for d in raw.values()], ignore_index=True)

    klines_dict = {s: raw[s]["klines"] for s in DEV_ASSETS}

    res = run_backtest_v5(
        data=klines_dict,
        w_mom=18,
        t_reb=6,
        lambda_carry=0.5,
        cost_mult=1.0,
        score_start_str="2023-01-01",
        score_end_str="2025-01-01",
        funding_df=funding_combined,
        force_period_end_liquidation=True,
    )

    assert len(res.r_bar) == 4386
    assert len(res.equity) == 4387

    # Check dollar neutrality on every bar
    net_exposure_per_bar = np.sum(res.weights, axis=1)
    assert np.allclose(net_exposure_per_bar, 0.0, atol=1e-10)

    # Check gross leverage on every bar
    gross_lev_per_bar = np.sum(np.abs(res.weights), axis=1)
    assert np.allclose(gross_lev_per_bar, 1.0, atol=1e-10)

    # Check no NaNs
    assert not np.isnan(res.equity).any()
    assert not np.isnan(res.r_bar).any()

    # Metrics dictionary sanity
    m = res.metrics
    assert "sharpe" in m
    assert "total_net_return" in m
    assert "max_drawdown" in m
    assert "dsr" in m
    assert m["max_drawdown"] >= 0.0


# ---------------------------------------------------------------------------
# 6. Rebalance Period t_reb=12 and Empty Data Robustness
# ---------------------------------------------------------------------------

def test_rebalance_period_12_bars():
    """Verify that t_reb=12 only rebalances at k=0, 12, 24."""
    n = 25
    opens = [100.0] * n
    closes = [100.0] * n
    rv30 = [0.20] * n

    data = {
        "BTCUSDT": make_synthetic_asset_df("BTCUSDT", opens, closes, [0.30]*n, [0.0]*n, rv30),
        "ETHUSDT": make_synthetic_asset_df("ETHUSDT", opens, closes, [0.20]*n, [0.0]*n, rv30),
        "ADAUSDT": make_synthetic_asset_df("ADAUSDT", opens, closes, [-0.30]*n, [0.0]*n, rv30),
        "DOGEUSDT": make_synthetic_asset_df("DOGEUSDT", opens, closes, [-0.20]*n, [0.0]*n, rv30),
    }

    res = run_backtest_v5(
        data,
        t_reb=12,
        cost_mult=1.0,
        force_period_end_liquidation=False,
    )

    rebalance_bars = [r["bar_idx"] for r in res.rebalance_records]
    assert rebalance_bars == [0, 12, 24]
    assert len(rebalance_bars) == 3


def test_empty_and_single_bar_backtest_safety():
    """Verify graceful handling of empty or single-bar inputs without crash."""
    # Empty data
    res_empty = run_backtest_v5({})
    assert len(res_empty.r_bar) == 0
    assert len(res_empty.equity) == 1
    assert res_empty.equity[0] == 1.0

    # Single bar
    data_1 = {
        "BTCUSDT": make_synthetic_asset_df("BTCUSDT", [100.0], [100.0], [0.30], [0.0], [0.20]),
        "ETHUSDT": make_synthetic_asset_df("ETHUSDT", [100.0], [100.0], [0.20], [0.0], [0.20]),
        "ADAUSDT": make_synthetic_asset_df("ADAUSDT", [100.0], [100.0], [-0.30], [0.0], [0.20]),
        "DOGEUSDT": make_synthetic_asset_df("DOGEUSDT", [100.0], [100.0], [-0.20], [0.0], [0.20]),
    }
    res_1 = run_backtest_v5(data_1, force_period_end_liquidation=True)
    assert len(res_1.r_bar) == 1
    assert len(res_1.equity) == 2


def test_subperiod_forced_liquidation_uses_close_not_future_open():
    """Verify that when a backtest period ends before the global dataset end,
    forced liquidation on the final evaluated bar strictly uses close_k (not open_{k+1})."""
    # 6 bars where bar 4 close = 150, but bar 5 open = 200
    opens = [100.0, 100.0, 100.0, 100.0, 100.0, 200.0]
    closes = [100.0, 100.0, 100.0, 100.0, 150.0, 200.0]
    t0 = 1672531200000

    dfs = {}
    for s in ["BTCUSDT", "ETHUSDT", "ADAUSDT", "DOGEUSDT"]:
        c_s = closes if s == "BTCUSDT" else [100.0] * 6
        o_s = opens if s == "BTCUSDT" else [100.0] * 6
        dfs[s] = pd.DataFrame({
            "sym": [s] * 6,
            "open_time": [t0 + i * BAR_MS for i in range(6)],
            "open": o_s,
            "close": c_s,
            "mom": [0.1] * 6 if ("BTC" in s or "ETH" in s) else [-0.1] * 6,
            "carry": [0.0] * 6,
            "rv30": [0.2] * 6,
            "bar_funding": [0.0] * 6,
        })

    # Cut off evaluation at bar 5 (open_time = t0 + 5 * BAR_MS), so bars 0..4 are evaluated
    s_end = pd.Timestamp(t0 + 5 * BAR_MS, unit="ms", tz="UTC").strftime("%Y-%m-%d %H:%M:%S")
    res = run_backtest_v5(
        dfs,
        t_reb=6,
        cost_mult=0.0,
        score_start_str=None,
        score_end_str=s_end,
        force_period_end_liquidation=True,
    )

    assert len(res.r_bar) == 5
    # Bar 4 is final eval bar. BTC weight = +0.25, others flat pnl.
    # Holding return on bar 4 must be BTC close (150) / open (100) - 1 = +0.50.
    # Contribution = 0.25 * 0.50 = +0.12500000.
    # Peeking into bar 5 open (200) would yield +0.25000000, which is incorrect.
    assert np.isclose(res.gross_r_bar[4], 0.125, atol=1e-12)
    assert np.isclose(res.r_bar[4], 0.125, atol=1e-12)


def test_placebo_shuffle_maintains_dollar_neutrality():
    """Verify that placebo cross-section shuffle maintains strict dollar neutrality (net weight == 0.0000)
    and is fully reproducible with a deterministic seed."""
    opens = [100.0] * 10
    closes = [100.0] * 10
    rv30 = [0.20] * 10
    data = {
        s: make_synthetic_asset_df(s, opens, closes, [0.1 * i] * 10, [0.0] * 10, rv30)
        for i, s in enumerate(["BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "LINKUSDT"])
    }

    res1 = run_backtest_v5(data, shuffle_cross_section=True, rng_seed=42)
    res2 = run_backtest_v5(data, shuffle_cross_section=True, rng_seed=42)

    # Determinism
    np.testing.assert_allclose(res1.equity, res2.equity)

    # Dollar neutrality check at all bars
    net_exposure = np.sum(res1.weights, axis=1)
    np.testing.assert_allclose(net_exposure, 0.0, atol=1e-12)

    # Gross leverage check (1.0)
    gross_leverage = np.sum(np.abs(res1.weights), axis=1)
    np.testing.assert_allclose(gross_leverage, 1.0, atol=1e-12)


@pytest.mark.needs_market_data
def test_dev_grid_runner_artifact_integrity():
    """Verify that run_dev_grid executes correctly and produces valid artifacts."""
    from pathlib import Path
    from lz5.run_dev_v5 import run_dev_grid

    res = run_dev_grid()
    assert res["branch"] in ("a", "b")
    assert res["candidate"]["config_id"] in {c["config_id"] for c in GRID_CONFIGS}
    assert res["total_trials_N"] == 8

    # Check files exist
    assert Path("research/out/dev_v5_results.csv").exists()
    assert Path("research/out/dev_v5_selected.json").exists()
    assert Path("research/LOG_V5.md").exists()

    # Verify LOG_V5.md table row count <= 20
    with open("research/LOG_V5.md", "r", encoding="utf-8") as f:
        lines = f.readlines()
    table_rows = [l for l in lines if l.startswith("|") and not l.startswith("| id") and not l.startswith("|---")]
    assert len(table_rows) == 8
    assert len(table_rows) <= 20



"""Regression and causality tests for Lorentzian v5 holdout runner and guards.

Verifies:
1. Environment permission guard (LZ_HOLDOUT_APPROVED='yes' required).
2. Anti-replay guard (holdout_v5_done.json existence blocks execution).
3. H1 universe purity (strictly 7 dev assets, SOL excluded).
4. H2 full 8-asset universe Dollar neutrality & leverage invariance at every bar.
5. Strict causality: future tampering does not leak into holdout decision periods.
6. Period-end forced liquidation correctness.
"""

import json
from pathlib import Path
import numpy as np
import pandas as pd
import pytest

from lz5.config import ALL_ASSETS, DEV_ASSETS, HOLDOUT_ASSET, load_universe_v5
from lz5.backtest_v5 import run_backtest_v5
from lz5.run_holdout_v5 import run_holdout, DONE_FILE


def test_holdout_requires_approval_env(monkeypatch):
    """Calling run_holdout without LZ_HOLDOUT_APPROVED='yes' must raise PermissionError."""
    monkeypatch.delenv("LZ_HOLDOUT_APPROVED", raising=False)
    with pytest.raises(PermissionError, match="LZ_HOLDOUT_APPROVED"):
        run_holdout()


def test_holdout_anti_replay_guard(tmp_path, monkeypatch):
    """If holdout_v5_done.json exists, run_holdout must raise RuntimeError."""
    monkeypatch.setenv("LZ_HOLDOUT_APPROVED", "yes")
    fake_done = tmp_path / "holdout_v5_done.json"
    fake_done.write_text('{"status": "DONE"}', encoding="utf-8")
    monkeypatch.setattr("lz5.run_holdout_v5.DONE_FILE", fake_done)

    with pytest.raises(RuntimeError, match="anti-replay"):
        run_holdout()


@pytest.mark.needs_market_data
def test_h1_universe_strictly_dev_assets(monkeypatch):
    """Verify that H1 setup strictly uses 7 DEV_ASSETS and excludes SOLUSDT."""
    monkeypatch.setenv("LZ_HOLDOUT_APPROVED", "yes")
    universe = load_universe_v5(split="holdout")
    h1_symbols = sorted([s for s in DEV_ASSETS])
    assert HOLDOUT_ASSET not in h1_symbols
    assert len(h1_symbols) == 7

    # Run single-bar smoke test for H1 universe
    klines_h1 = {s: universe[s]["klines"] for s in h1_symbols}
    res = run_backtest_v5(
        klines_h1,
        w_mom=42, t_reb=6, lambda_carry=1.0,
        score_start_str="2025-01-01", score_end_str="2025-01-05",
    )
    assert res.symbols == h1_symbols
    assert HOLDOUT_ASSET not in res.symbols


@pytest.mark.needs_market_data
def test_h2_8_assets_dollar_neutrality_every_bar(monkeypatch):
    """Verify that H2 with 8 assets (including SOL) preserves sum(w) == 0.0000 and sum(|w|) == 1.0000 at all bars."""
    monkeypatch.setenv("LZ_HOLDOUT_APPROVED", "yes")
    universe = load_universe_v5(split="holdout")
    funding_all = pd.concat([universe[s]["funding"] for s in ALL_ASSETS], ignore_index=True)
    klines_h2 = {s: universe[s]["klines"] for s in ALL_ASSETS}

    # Evaluate across a 1-month window to check all rebalances
    res = run_backtest_v5(
        klines_h2,
        w_mom=42, t_reb=6, lambda_carry=1.0,
        score_start_str="2023-01-01", score_end_str="2023-02-01",
        funding_df=funding_all,
        n_long=2, n_short=2,
    )

    assert len(res.r_bar) > 0
    # Net dollar exposure must be zero
    net_exposure = np.sum(res.weights, axis=1)
    np.testing.assert_allclose(net_exposure, 0.0, atol=1e-12)

    # Gross leverage must be 1.0
    gross_leverage = np.sum(np.abs(res.weights), axis=1)
    np.testing.assert_allclose(gross_leverage, 1.0, atol=1e-12)


@pytest.mark.needs_market_data
def test_holdout_future_causality_temporal_tampering(monkeypatch):
    """Tampering with data at future bar T+10 must not alter positions or returns at bar T."""
    monkeypatch.setenv("LZ_HOLDOUT_APPROVED", "yes")
    universe = load_universe_v5(split="holdout")

    # Take copy of klines for dev assets
    klines_orig = {s: universe[s]["klines"].copy() for s in DEV_ASSETS}
    res_orig = run_backtest_v5(
        klines_orig,
        w_mom=42, t_reb=6, lambda_carry=1.0,
        score_start_str="2025-01-01", score_end_str="2025-01-15",
    )

    # Tamper with future prices after 2025-01-10
    cutoff_tamper = int(pd.Timestamp("2025-01-10", tz="UTC").timestamp() * 1000)
    klines_tampered = {}
    for s in DEV_ASSETS:
        df_t = universe[s]["klines"].copy()
        mask = df_t["open_time"] >= cutoff_tamper
        df_t.loc[mask, "close"] = df_t.loc[mask, "close"] * 5.0
        df_t.loc[mask, "open"] = df_t.loc[mask, "open"] * 5.0
        klines_tampered[s] = df_t

    res_tampered = run_backtest_v5(
        klines_tampered,
        w_mom=42, t_reb=6, lambda_carry=1.0,
        score_start_str="2025-01-01", score_end_str="2025-01-15",
    )

    # For weights: decisions at bar t (using close <= t-1) must be strictly identical for all t < cutoff_tamper
    # For r_bar: holding window [t, t + BAR_MS] must be strictly identical for all t + BAR_MS < cutoff_tamper
    from lz5.config import BAR_MS
    for i, t in enumerate(res_orig.open_times):
        if t < cutoff_tamper:
            np.testing.assert_allclose(res_orig.weights[i], res_tampered.weights[i], atol=1e-12)
            if t + BAR_MS < cutoff_tamper:
                assert np.isclose(res_orig.r_bar[i], res_tampered.r_bar[i], atol=1e-12)

    # Causality test must be non-vacuous: future tampering must actually alter future returns
    assert not np.allclose(res_orig.r_bar, res_tampered.r_bar)


def test_lz5_config_load_guards(monkeypatch):
    """Verify load_asset_v5 security guards for dev isolation and holdout permissions."""
    from lz5.config import load_asset_v5

    # 1. Invalid split
    with pytest.raises(ValueError, match="Invalid split"):
        load_asset_v5("BTCUSDT", split="invalid_split")

    # 2. SOL strictly forbidden in dev split
    with pytest.raises(PermissionError, match="strictly forbidden in dev"):
        load_asset_v5(HOLDOUT_ASSET, split="dev")

    # 3. Holdout split forbidden without env var
    monkeypatch.delenv("LZ_HOLDOUT_APPROVED", raising=False)
    with pytest.raises(PermissionError, match="LZ_HOLDOUT_APPROVED"):
        load_asset_v5("BTCUSDT", split="holdout")
    with pytest.raises(PermissionError, match="LZ_HOLDOUT_APPROVED"):
        load_asset_v5(HOLDOUT_ASSET, split="holdout")


@pytest.mark.needs_market_data
def test_holdout_period_end_forced_liquidation(monkeypatch):
    """Verify that period-end forced liquidation deducts liquidation friction and flattens positions."""
    monkeypatch.setenv("LZ_HOLDOUT_APPROVED", "yes")
    universe = load_universe_v5(split="holdout")
    klines_h1 = {s: universe[s]["klines"] for s in DEV_ASSETS}

    res_liq = run_backtest_v5(
        klines_h1,
        w_mom=42, t_reb=6, lambda_carry=1.0,
        score_start_str="2025-01-01", score_end_str="2025-01-05",
        force_period_end_liquidation=True,
    )
    res_noliq = run_backtest_v5(
        klines_h1,
        w_mom=42, t_reb=6, lambda_carry=1.0,
        score_start_str="2025-01-01", score_end_str="2025-01-05",
        force_period_end_liquidation=False,
    )

    # Period-end liquidation must incur additional transaction friction
    assert res_liq.metrics["total_cost"] > res_noliq.metrics["total_cost"]
    # Final equity is lower by liquidation friction
    assert res_liq.metrics["total_net_return"] < res_noliq.metrics["total_net_return"]


@pytest.mark.needs_market_data
def test_run_holdout_execution_flow(tmp_path, monkeypatch):
    """Verify run_holdout creates DONE_FILE, updates LOG_FILE, and blocks second execution."""
    monkeypatch.setenv("LZ_HOLDOUT_APPROVED", "yes")
    test_done = tmp_path / "test_holdout_done.json"
    test_log = tmp_path / "test_LOG.md"
    test_log.write_text("# Test Log\n| id | cfg |\n|---|---|\n| 1 | cfg_01 |\n", encoding="utf-8")

    monkeypatch.setattr("lz5.run_holdout_v5.DONE_FILE", test_done)
    monkeypatch.setattr("lz5.run_holdout_v5.LOG_FILE", test_log)

    out = run_holdout()
    assert test_done.exists()
    assert out["status"] == "DONE"
    assert "H1" in out and "H2" in out

    log_content = test_log.read_text(encoding="utf-8")
    assert "## Holdout Diagnostics (Phase 3)" in log_content
    assert "H1 (7 dev assets 2025-2026 OOS): " in log_content

    # Second execution must immediately raise anti-replay RuntimeError
    with pytest.raises(RuntimeError, match="anti-replay"):
        run_holdout()



def test_holdout_candidate_comes_from_dev_selection(tmp_path, monkeypatch):
    """The blind-test candidate is whatever grid cell the dev run froze, never a baked-in id."""
    from lz5 import run_holdout_v5
    from lz5.config import GRID_CONFIGS

    selected = tmp_path / "dev_v5_selected.json"
    monkeypatch.setattr(run_holdout_v5, "SELECTED_FILE", selected)
    with pytest.raises(FileNotFoundError, match="run_dev_v5"):
        run_holdout_v5.load_candidate()

    for cfg in GRID_CONFIGS:
        selected.write_text(json.dumps({"candidate": {"config_id": cfg["config_id"], "sharpe": 0.0}}), encoding="utf-8")
        assert run_holdout_v5.load_candidate() == cfg

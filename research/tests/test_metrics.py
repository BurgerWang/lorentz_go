"""Unit tests for metrics and DSR."""

import numpy as np
import pytest

from lz.metrics import calc_dsr, calculate_metrics


def test_dsr_n1_v0_and_sr_zero():
    # Symmetric zero-mean returns: mean=0, std>0 -> SR_hat = 0
    r = np.array([0.01, -0.01, 0.02, -0.02, 0.01, -0.01])
    # When N=1 and V=0, SR0 must be 0, and SR_hat=0 must give DSR = 0.5
    dsr = calc_dsr(r, v_sr=0.0, n_trials=1)
    assert np.isclose(dsr, 0.5)


def test_dsr_positive_sr():
    # Realistic 4h returns: mean ~0.0005, std ~0.01, SR_hat ~ 0.05
    rng = np.random.default_rng(42)
    r = rng.normal(loc=0.0005, scale=0.01, size=500)
    # With N=1, SR0=0, DSR should be moderately positive (>0.5)
    dsr = calc_dsr(r, v_sr=0.0, n_trials=1)
    assert dsr > 0.5

    # With high variance of previous trials and large N, DSR should be discounted
    dsr_discounted = calc_dsr(r, v_sr=0.001, n_trials=100)
    assert dsr_discounted < dsr


def test_calculate_metrics_structure():
    equity = np.array([1.0, 1.01, 1.02, 1.015, 1.03])
    r_bar = np.array([0.01, 0.0099, -0.0049, 0.0148])
    trade_records = [
        {"gross": 0.02, "rt": 0.0014},
        {"gross": -0.01, "rt": 0.0014},
    ]
    per_asset = {"BTCUSDT": np.array([0.01, 0.01]), "ETHUSDT": np.array([-0.005, 0.01])}
    res = calculate_metrics(equity, r_bar, trade_records, per_asset, v_sr=0.0, n_trials=1)

    assert "net_return" in res
    assert "sharpe_ann" in res
    assert "max_dd" in res
    assert res["n_trades"] == 2
    assert res["assets_pos"] == 2
    assert np.isclose(res["gross_mean_bps"], 50.0)  # (0.02 - 0.01)/2 * 10000 = 50 bps


def test_dsr_edge_cases():
    # Empty or single-bar returns return 0.5
    assert calc_dsr(np.array([]), v_sr=0.01, n_trials=10) == 0.5
    assert calc_dsr(np.array([0.05]), v_sr=0.01, n_trials=10) == 0.5

    # Constant returns (zero variance) return 0.5
    assert calc_dsr(np.array([0.01, 0.01, 0.01, 0.01]), v_sr=0.01, n_trials=10) == 0.5

    # Monotonicity with respect to N: more trials -> lower DSR
    rng = np.random.default_rng(123)
    r = rng.normal(loc=0.001, scale=0.01, size=1000)
    dsr_10 = calc_dsr(r, v_sr=0.0004, n_trials=10)
    dsr_50 = calc_dsr(r, v_sr=0.0004, n_trials=50)
    dsr_161 = calc_dsr(r, v_sr=0.0004, n_trials=161)
    assert dsr_10 > dsr_50 > dsr_161


def test_count_log_trials_parser(tmp_path):
    from lz.run_dev import count_log_trials
    log_file = tmp_path / "test_log.md"

    # Empty file
    assert count_log_trials(log_file) == 0

    # Header only
    log_file.write_text("# Trial log\n| id | utc |\n|---|---|\n")
    assert count_log_trials(log_file) == 0

    # 3 rows
    log_file.write_text("# Trial log\n| id | utc |\n|---|---|\n| 1 | ok |\n| 2 | ok |\n| 3 | ok |\n")
    assert count_log_trials(log_file) == 3

    # Diagnostics section should not be counted
    log_file.write_text("# Trial log\n| id | utc |\n|---|---|\n| 1 | ok |\n| 2 | ok |\n## Diagnostics\n- info\n| 3 | not counted |\n")
    assert count_log_trials(log_file) == 2


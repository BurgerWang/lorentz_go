"""Unit tests for Lorentzian v5 cross-sectional ranking engine.

Verifies:
1. Cross-sectional standardization z(X) mathematical properties.
2. Composite cross-sectional score: Score_i = z(Mom_i) + lambda * z(Carry_i).
3. 7-asset ranking and strict Dollar neutrality:
   - Top 2 Long sum == +0.50
   - Bottom 2 Short sum == -0.50
   - Ranks 3..5 Middle == 0.00
   - Net exposure == 0.0000, Gross leverage == 1.0000
4. 1/rv30 Risk Parity weighting exact hand calculation.
5. Strict temporal causality & future tampering independence.
6. SOL isolation guard (R4) and dev cutoff guard (R5).
7. Boundary and edge case handling (ties, zero/nan rv30, uniform values).
"""

import numpy as np
import pandas as pd
import pytest

from lz5.config import (
    DEV_ASSETS,
    HOLDOUT_ASSET,
    DEV_END,
    get_dev_cutoff_ms,
    load_asset_v5,
)
from lz5.ranker import (
    calc_rv30,
    calc_funding_3d,
    cross_sectional_zscore,
    compute_cross_sectional_scores,
    allocate_long_short_weights,
    rank_and_allocate,
)


# ---------------------------------------------------------------------------
# 1. Cross-sectional z-score properties
# ---------------------------------------------------------------------------

def test_cross_sectional_zscore_properties():
    """Verify mean is zero, variance is 1, and uniform inputs don't crash."""
    vals = {"BTC": 10.0, "ETH": 20.0, "BNB": 30.0, "XRP": 40.0}
    z = cross_sectional_zscore(vals)

    z_arr = np.array(list(z.values()))
    assert np.isclose(np.mean(z_arr), 0.0, atol=1e-10)
    assert np.isclose(np.std(z_arr, ddof=0), 1.0, atol=1e-10)

    # Monotonicity
    assert z["BTC"] < z["ETH"] < z["BNB"] < z["XRP"]

    # Uniform input test (std == 0)
    uniform_vals = {"BTC": 5.0, "ETH": 5.0, "BNB": 5.0}
    z_uni = cross_sectional_zscore(uniform_vals)
    for k in uniform_vals:
        assert np.isclose(z_uni[k], 0.0, atol=1e-10)


# ---------------------------------------------------------------------------
# 2. Composite Score Hand Calculation
# ---------------------------------------------------------------------------

def test_composite_scores_hand_calculation():
    """Exact hand-calculated reconciliation for Score_i = z(Mom_i) + lambda * z(Carry_i)."""
    # 7 assets with known values
    mom = {
        "BTCUSDT": 0.10,
        "ETHUSDT": 0.05,
        "BNBUSDT": 0.00,
        "XRPUSDT": -0.05,
        "DOGEUSDT": -0.10,
        "ADAUSDT": 0.15,
        "LINKUSDT": 0.20,
    }
    # Carry = - Funding_3d
    carry = {
        "BTCUSDT": -0.0001,
        "ETHUSDT": -0.0002,
        "BNBUSDT": 0.0000,
        "XRPUSDT": 0.0005,
        "DOGEUSDT": -0.0008,
        "ADAUSDT": 0.0002,
        "LINKUSDT": -0.0003,
    }

    # Manual calculation
    mom_arr = np.array([mom[s] for s in sorted(mom.keys())])
    carry_arr = np.array([carry[s] for s in sorted(carry.keys())])

    z_mom_manual = (mom_arr - np.mean(mom_arr)) / np.std(mom_arr, ddof=0)
    z_carry_manual = (carry_arr - np.mean(carry_arr)) / np.std(carry_arr, ddof=0)

    for lc in [0.5, 1.0]:
        scores = compute_cross_sectional_scores(mom, carry, lambda_carry=lc)
        expected = z_mom_manual + lc * z_carry_manual
        actual = np.array([scores[s] for s in sorted(scores.keys())])
        assert np.allclose(actual, expected, atol=1e-10)


# ---------------------------------------------------------------------------
# 3. Strict Dollar Neutrality & Leg Weight Sums
# ---------------------------------------------------------------------------

def test_long_short_allocation_strict_dollar_neutrality():
    """Verify Top 2 Long = +0.50, Bottom 2 Short = -0.50, Ranks 3..5 = 0.00,
    Net Exposure = 0.0000, Gross Leverage = 1.0000."""
    scores = {
        "BTC": 2.5,   # Rank 1 -> Long
        "ETH": 1.8,   # Rank 2 -> Long
        "BNB": 0.5,   # Rank 3 -> Flat
        "XRP": 0.0,   # Rank 4 -> Flat
        "DOGE": -0.3, # Rank 5 -> Flat
        "ADA": -1.2,  # Rank 6 -> Short
        "LINK": -2.0, # Rank 7 -> Short
    }
    # Equal rv30 -> should be exactly +0.25, +0.25, -0.25, -0.25
    rv30 = {s: 0.20 for s in scores}

    weights = allocate_long_short_weights(scores, rv30, n_long=2, n_short=2)

    assert np.isclose(weights["BTC"], 0.25)
    assert np.isclose(weights["ETH"], 0.25)
    assert np.isclose(weights["BNB"], 0.0)
    assert np.isclose(weights["XRP"], 0.0)
    assert np.isclose(weights["DOGE"], 0.0)
    assert np.isclose(weights["ADA"], -0.25)
    assert np.isclose(weights["LINK"], -0.25)

    long_sum = sum(w for w in weights.values() if w > 0)
    short_sum = sum(w for w in weights.values() if w < 0)
    net_exposure = sum(weights.values())
    gross_leverage = sum(abs(w) for w in weights.values())

    assert np.isclose(long_sum, 0.50, atol=1e-12)
    assert np.isclose(short_sum, -0.50, atol=1e-12)
    assert np.isclose(net_exposure, 0.0000, atol=1e-12)
    assert np.isclose(gross_leverage, 1.0000, atol=1e-12)


# ---------------------------------------------------------------------------
# 4. Risk Parity Weighting Exact Hand Calculation
# ---------------------------------------------------------------------------

def test_risk_parity_weighting_hand_calculation():
    """Verify 1/rv30 weighting matches exact analytical values."""
    scores = {
        "A": 3.0,  # Long 1
        "B": 2.0,  # Long 2
        "C": 1.0,  # Flat
        "D": 0.0,  # Flat
        "E": -1.0, # Flat
        "F": -2.0, # Short 1
        "G": -3.0, # Short 2
    }
    # A has vol 0.20, B has vol 0.40
    # inv_rv(A) = 5.0, inv_rv(B) = 2.5, sum = 7.5
    # w(A) = +0.50 * (5.0 / 7.5) = +0.50 * (2/3) = +1/3
    # w(B) = +0.50 * (2.5 / 7.5) = +0.50 * (1/3) = +1/6
    # F has vol 0.10, G has vol 0.30
    # inv_rv(F) = 10.0, inv_rv(G) = 10/3, sum = 40/3
    # w(F) = -0.50 * (10 / (40/3)) = -0.50 * (30/40) = -0.375
    # w(G) = -0.50 * ((10/3) / (40/3)) = -0.50 * (10/40) = -0.125
    rv30 = {
        "A": 0.20,
        "B": 0.40,
        "C": 0.25,
        "D": 0.25,
        "E": 0.25,
        "F": 0.10,
        "G": 0.30,
    }

    weights = allocate_long_short_weights(scores, rv30, n_long=2, n_short=2)

    assert np.isclose(weights["A"], 1.0 / 3.0, atol=1e-10)
    assert np.isclose(weights["B"], 1.0 / 6.0, atol=1e-10)
    assert np.isclose(weights["C"], 0.0, atol=1e-10)
    assert np.isclose(weights["D"], 0.0, atol=1e-10)
    assert np.isclose(weights["E"], 0.0, atol=1e-10)
    assert np.isclose(weights["F"], -0.375, atol=1e-10)
    assert np.isclose(weights["G"], -0.125, atol=1e-10)

    # Leg sums and net neutrality
    assert np.isclose(weights["A"] + weights["B"], 0.50, atol=1e-12)
    assert np.isclose(weights["F"] + weights["G"], -0.50, atol=1e-12)
    assert np.isclose(sum(weights.values()), 0.0000, atol=1e-12)
    assert np.isclose(sum(abs(w) for w in weights.values()), 1.0000, atol=1e-12)


# ---------------------------------------------------------------------------
# 5. Strict Causality & Future Tampering Independence
# ---------------------------------------------------------------------------

def test_causal_funding_3d_no_future_leakage():
    """calc_funding_3d must only observe settlements strictly prior to bar open_time."""
    bar_ms = 4 * 3600 * 1000
    t0 = 1672531200000
    klines = pd.DataFrame({
        "open_time": [t0, t0 + bar_ms, t0 + 2 * bar_ms, t0 + 3 * bar_ms],
        "sym": ["BTCUSDT"] * 4,
    })
    # Settle exactly at t0 and t0 + 2 * bar_ms
    funding = pd.DataFrame({
        "sym": ["BTCUSDT"] * 2,
        "calc_time": [t0, t0 + 2 * bar_ms],
        "settle_hour": [t0, t0 + 2 * bar_ms],
        "rate": [0.0002, 0.0008],
    })

    f3d = calc_funding_3d(klines, funding, min_periods=1)

    # Bar 0 (open_time = t0): settlements < t0 -> None observed -> 0.0
    assert np.isclose(f3d[0], 0.0)

    # Bar 1 (open_time = t0 + 1 bar): settlement at t0 observed (rate = 0.0002)
    assert np.isclose(f3d[1], (0.0 + 0.0002) / 2.0)

    # Bar 2 (open_time = t0 + 2 bars): settlement at t0 + 2 bars is NOT yet observed (settle_hour < t0 + 2 bars fails)
    assert np.isclose(f3d[2], (0.0 + 0.0002 + 0.0002) / 3.0)

    # Bar 3 (open_time = t0 + 3 bars): settlement at t0 + 2 bars is now observed
    assert np.isclose(f3d[3], (0.0 + 0.0002 + 0.0002 + 0.0008) / 4.0)

    # Tamper with future settlement in funding table (modify t0 + 2 bar rate)
    funding_tampered = pd.DataFrame({
        "sym": ["BTCUSDT"] * 2,
        "calc_time": [t0, t0 + 2 * bar_ms],
        "settle_hour": [t0, t0 + 2 * bar_ms],
        "rate": [0.0002, 0.9999],  # wildly changed future rate
    })
    f3d_tampered = calc_funding_3d(klines, funding_tampered, min_periods=1)

    # Bar 0 and Bar 1 MUST be strictly identical despite future alteration!
    assert np.isclose(f3d_tampered[0], f3d[0])
    assert np.isclose(f3d_tampered[1], f3d[1])
    assert np.isclose(f3d_tampered[2], f3d[2])
    assert not np.isclose(f3d_tampered[3], f3d[3])


def test_rv30_causal_rolling():
    """calc_rv30 must only compute on past prices."""
    closes = np.array([100.0, 102.0, 101.0, 103.0, 105.0] * 40)  # 200 bars
    rv = calc_rv30(closes, min_periods=10)

    # Changing future bars cannot alter past rv30 values
    closes_tampered = closes.copy()
    closes_tampered[-1] = 99999.0
    rv_tampered = calc_rv30(closes_tampered, min_periods=10)

    assert np.allclose(rv[:-1], rv_tampered[:-1], equal_nan=True)


# ---------------------------------------------------------------------------
# 6. SOL Isolation Guard (R4) & Dev Cutoff (R5)
# ---------------------------------------------------------------------------

def test_sol_in_dev_raises_permission_error():
    """R4 Guard: Loading SOLUSDT in dev split must raise PermissionError."""
    with pytest.raises(PermissionError, match="strictly forbidden in dev"):
        load_asset_v5(HOLDOUT_ASSET, split="dev")


def test_holdout_without_approval_raises_permission_error(monkeypatch):
    """R12 Guard: Loading holdout without LZ_HOLDOUT_APPROVED='yes' must raise PermissionError."""
    monkeypatch.delenv("LZ_HOLDOUT_APPROVED", raising=False)
    with pytest.raises(PermissionError, match="LZ_HOLDOUT_APPROVED"):
        load_asset_v5("BTCUSDT", split="holdout")
    with pytest.raises(PermissionError, match="LZ_HOLDOUT_APPROVED"):
        load_asset_v5(HOLDOUT_ASSET, split="holdout")


@pytest.mark.needs_market_data
def test_dev_data_physical_cutoff():
    """R5 Guard: All kline open_time and funding calc_time must be < 2025-01-01 in dev split."""
    cutoff_ms = get_dev_cutoff_ms()
    expected_cutoff = int(pd.Timestamp(DEV_END, tz="UTC").timestamp() * 1000)
    assert cutoff_ms == expected_cutoff

    for sym in DEV_ASSETS:
        data = load_asset_v5(sym, split="dev")
        klines = data["klines"]
        funding = data["funding"]
        assert len(klines) > 0
        assert klines["open_time"].max() < cutoff_ms
        assert funding["calc_time"].max() < cutoff_ms


# ---------------------------------------------------------------------------
# 7. Edge Cases & Boundary Conditions
# ---------------------------------------------------------------------------

def test_missing_or_invalid_rv30_fallback():
    """Invalid or zero rv30 must fall back safely without division by zero."""
    scores = {"BTC": 2.0, "ETH": 1.0, "BNB": 0.0, "ADA": -1.0, "DOGE": -2.0}
    # BTC rv30 is NaN, ETH rv30 is 0.0
    rv30 = {"BTC": np.nan, "ETH": 0.0, "BNB": 0.2, "ADA": np.nan, "DOGE": -0.5}

    weights = allocate_long_short_weights(scores, rv30, n_long=2, n_short=2)

    # Fallback to equal weighting: +0.25 each for long, -0.25 each for short
    assert np.isclose(weights["BTC"], 0.25)
    assert np.isclose(weights["ETH"], 0.25)
    assert np.isclose(weights["ADA"], -0.25)
    assert np.isclose(weights["DOGE"], -0.25)
    assert np.isclose(weights["BNB"], 0.0)
    assert np.isclose(sum(weights.values()), 0.0000, atol=1e-12)


def test_insufficient_universe_raises_error():
    """Less than n_long + n_short assets must raise ValueError."""
    scores = {"BTC": 1.0, "ETH": 0.0}
    rv30 = {"BTC": 0.2, "ETH": 0.2}
    with pytest.raises(ValueError, match="Universe size"):
        allocate_long_short_weights(scores, rv30, n_long=2, n_short=2)


def test_partial_invalid_rv30_basket_fallback():
    """If one asset in a basket has valid rv30 and another is NaN, the basket must fall back to equal weighting."""
    scores = {"BTC": 2.0, "ETH": 1.0, "BNB": 0.0, "ADA": -1.0, "DOGE": -2.0}
    # Long basket: BTC is valid 0.02, ETH is NaN
    # Short basket: ADA is valid 0.10, DOGE is valid 0.30 (3:1 ratio)
    rv30 = {"BTC": 0.02, "ETH": np.nan, "BNB": 0.2, "ADA": 0.10, "DOGE": 0.30}

    weights = allocate_long_short_weights(scores, rv30, n_long=2, n_short=2)

    # Long basket falls back to equal weight (+0.25 each), avoiding arbitrary skewing
    assert np.isclose(weights["BTC"], 0.25, atol=1e-10)
    assert np.isclose(weights["ETH"], 0.25, atol=1e-10)

    # Short basket remains valid risk parity: 1/0.10 = 10, 1/0.30 = 3.333 -> 75% ADA, 25% DOGE
    assert np.isclose(weights["ADA"], -0.375, atol=1e-10)
    assert np.isclose(weights["DOGE"], -0.125, atol=1e-10)
    assert np.isclose(sum(weights.values()), 0.0000, atol=1e-12)


def test_zero_variance_zscore_zero():
    """Near-zero variance inputs must cleanly map to 0.0 without numerical noise."""
    vals = {"BTC": 1.0, "ETH": 1.0 + 1e-15, "BNB": 1.0 - 1e-15}
    z = cross_sectional_zscore(vals)
    for k in vals:
        assert np.isclose(z[k], 0.0, atol=1e-12)


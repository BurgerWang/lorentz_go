"""Causality tests verifying absence of lookahead bias."""

import numpy as np
import pytest

from lz.config import BAR_MS, DEV_ASSETS, FEATURE_NAMES
from lz.features import compute_features_single
from lz.load import load_asset


@pytest.fixture(scope="module")
def dev_data():
    """Load dev data for all assets once for the module tests."""
    data = {}
    for sym in DEV_ASSETS:
        try:
            data[sym] = load_asset(sym, split="dev")
        except FileNotFoundError:
            pytest.skip(f"Data for {sym} not downloaded yet.")
    return data


def test_truncation_equivalence(dev_data):
    """Test 1: Truncation equivalence test.

    For each dev asset, sample 50 random timestamps t.
    Compare features computed on strictly truncated data vs full dev data at t.
    """
    btc_klines = dev_data["BTCUSDT"]["klines"]
    rng = np.random.default_rng(42)

    for sym in DEV_ASSETS:
        asset_dict = dev_data[sym]
        klines = asset_dict["klines"]
        funding = asset_dict["funding"]
        metrics = asset_dict["metrics"]

        n = len(klines)
        if n < 300:
            continue

        # Compute full features once
        full_feat = compute_features_single(
            sym=sym,
            klines=klines,
            funding=funding,
            metrics=metrics,
            btc_klines=btc_klines,
        )

        # Sample 50 indices between 200 and n - 1
        sample_indices = rng.choice(np.arange(200, n), size=min(50, n - 200), replace=False)

        for idx in sample_indices:
            t_open = int(klines.iloc[idx]["open_time"])
            t_close = t_open + BAR_MS
            m_cutoff = t_open + (3 * 3600 + 55 * 60) * 1000

            # (a) Truncated raw data
            trunc_klines = klines[klines["open_time"] <= t_open].copy()
            trunc_metrics = metrics[metrics["ts"] <= m_cutoff].copy()
            trunc_funding = funding[funding["settle_hour"] < t_close].copy()
            trunc_btc = btc_klines[btc_klines["open_time"] <= t_open].copy()

            trunc_feat = compute_features_single(
                sym=sym,
                klines=trunc_klines,
                funding=trunc_funding,
                metrics=trunc_metrics,
                btc_klines=trunc_btc,
            )

            # Compare the last row of trunc_feat with full_feat at idx
            val_a = trunc_feat.iloc[-1][FEATURE_NAMES].to_numpy(dtype="float64")
            val_b = full_feat.iloc[idx][FEATURE_NAMES].to_numpy(dtype="float64")

            assert np.allclose(val_a, val_b, equal_nan=True), (
                f"Mismatch in {sym} at index {idx} (t_open={t_open}):\n"
                f"Truncated: {val_a}\nFull: {val_b}"
            )


def test_future_perturbation(dev_data):
    """Test 3: Future perturbation test.

    Perturb data strictly after timestamp t, verify all features at or before t are unchanged.
    """
    btc_klines = dev_data["BTCUSDT"]["klines"]
    rng = np.random.default_rng(123)

    for sym in DEV_ASSETS:
        asset_dict = dev_data[sym]
        klines = asset_dict["klines"]
        funding = asset_dict["funding"]
        metrics = asset_dict["metrics"]

        n = len(klines)
        if n < 500:
            continue

        base_feat = compute_features_single(
            sym=sym,
            klines=klines,
            funding=funding,
            metrics=metrics,
            btc_klines=btc_klines,
        )

        # Pick t at index 400
        t_idx = 400
        t_open = int(klines.iloc[t_idx]["open_time"])
        t_close = t_open + BAR_MS
        m_cutoff = t_open + (3 * 3600 + 55 * 60) * 1000

        # Perturb futures
        pert_klines = klines.copy()
        post_mask = pert_klines["open_time"] > t_open
        for col in ["open", "high", "low", "close", "volume", "quote_volume", "taker_buy_volume"]:
            pert_klines.loc[post_mask, col] *= 1.5

        pert_metrics = metrics.copy()
        m_post_mask = pert_metrics["ts"] > m_cutoff
        for col in ["oi", "oi_value", "top_ls_count", "top_ls_sum", "ls_count", "taker_ls_vol"]:
            pert_metrics.loc[m_post_mask, col] *= 2.0

        pert_funding = funding.copy()
        f_post_mask = pert_funding["settle_hour"] >= t_close
        pert_funding.loc[f_post_mask, "rate"] *= -3.0

        pert_btc = btc_klines.copy()
        btc_post = pert_btc["open_time"] > t_open
        for col in ["open", "high", "low", "close"]:
            pert_btc.loc[btc_post, col] *= 1.8

        pert_feat = compute_features_single(
            sym=sym,
            klines=pert_klines,
            funding=pert_funding,
            metrics=pert_metrics,
            btc_klines=pert_btc,
        )

        base_past = base_feat.iloc[: t_idx + 1][FEATURE_NAMES].to_numpy(dtype="float64")
        pert_past = pert_feat.iloc[: t_idx + 1][FEATURE_NAMES].to_numpy(dtype="float64")

        assert np.allclose(base_past, pert_past, equal_nan=True), (
            f"Future perturbation affected past features in {sym}!"
        )

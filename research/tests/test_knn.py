"""Unit tests for Lorentzian KNN and bank maturity."""

import numpy as np
import pytest

from lz.config import BAR_MS, MATURITY_MS
from lz.knn import compute_lorentzian_distance_batch, build_knn_cache_for_group, predict_knn_from_cache
import pandas as pd


def test_lorentzian_distance_brute_force_equivalence():
    rng = np.random.default_rng(42)
    b_len, m_len, d_dim = 20, 200, 3

    q_feats = rng.normal(size=(b_len, d_dim)).astype(np.float32)
    b_feats = rng.normal(size=(m_len, d_dim)).astype(np.float32)

    # Vectorized batch computation
    dist_batch = compute_lorentzian_distance_batch(q_feats, b_feats)

    # Brute-force double loop
    dist_brute = np.zeros((b_len, m_len), dtype=np.float32)
    for i in range(b_len):
        for j in range(m_len):
            total = 0.0
            for d in range(d_dim):
                total += np.log1p(abs(float(q_feats[i, d]) - float(b_feats[j, d])))
            dist_brute[i, j] = np.float32(total)

    assert np.allclose(dist_batch, dist_brute, atol=1e-5)


def test_bank_maturity_in_cache(tmp_path):
    """Test 5.4 rule 2: Bank samples j used at t must satisfy open_time[j] <= t_open - 28h."""
    # Synthetic dataframe
    n_days = 500
    times = [i * BAR_MS for i in range(n_days * 6)]
    rng = np.random.default_rng(99)

    df = pd.DataFrame({
        "sym": ["BTCUSDT"] * len(times),
        "open_time": times,
        "close": 100.0 + np.cumsum(rng.normal(size=len(times))),
        "open": 100.0 + np.cumsum(rng.normal(size=len(times))),
        "taker_imb": rng.normal(size=len(times)),
        "rv_7d": rng.normal(size=len(times)),
        "vol_anom": rng.normal(size=len(times)),
        "y": rng.choice([-1.0, 0.0, 1.0], size=len(times)),
    })

    score_start = pd.to_datetime(times[370 * 6], unit="ms", utc=True).strftime("%Y-%m-%d")
    score_end = pd.to_datetime(times[-1], unit="ms", utc=True).strftime("%Y-%m-%d")

    cache_file = build_knn_cache_for_group(
        features_df=df,
        group="B",
        score_start_str=score_start,
        score_end_str=score_end,
        cache_dir=tmp_path,
    )

    data = np.load(cache_file)
    q_time = data["q_time"]
    nb_time = data["nb_time"]

    # Verify maturity: each valid neighbor time must be <= q_time - 28h
    for i in range(len(q_time)):
        qt = q_time[i]
        for k in range(128):
            nt = nb_time[i, k]
            if nt != -1:
                assert nt <= qt - MATURITY_MS, f"Maturity violation! Neighbor time {nt} > {qt - MATURITY_MS}"


def test_bank_maturity_on_real_dev_features(tmp_path):
    """Test 5.4 rule 2 on actual dev features dataset."""
    from pathlib import Path
    feat_file = Path("research/out/features_dev.parquet")
    if not feat_file.exists():
        pytest.skip("features_dev.parquet not generated yet")

    df = pd.read_parquet(feat_file)
    # Test on first month of scoring: 2023-01-01 to 2023-02-01
    cache_file = build_knn_cache_for_group(
        features_df=df,
        group="B",
        score_start_str="2023-01-01",
        score_end_str="2023-02-01",
        cache_dir=tmp_path,
    )
    data = np.load(cache_file)
    q_time = data["q_time"]
    nb_time = data["nb_time"]
    for i in range(len(q_time)):
        qt = q_time[i]
        for k in range(128):
            nt = nb_time[i, k]
            if nt != -1:
                assert nt <= qt - MATURITY_MS, f"Maturity violation on real dev features: {nt} > {qt - MATURITY_MS}"

    # Verify predict_knn_from_cache runs without pickle error and produces valid outputs
    q_sym, q_time_out, scores, signals = predict_knn_from_cache(data, k=16, weight="equal", theta=0.1)
    assert len(signals) == len(q_time)
    assert np.all(np.isin(signals, [-1.0, 0.0, 1.0]))
    assert np.all(np.abs(scores) <= 1.0 + 1e-6)


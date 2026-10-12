"""Lorentzian KNN model implementation and caching."""

from pathlib import Path
import numpy as np
import pandas as pd
from lz.config import DEV_SCORE_START, DEV_END, GROUPS, MATURITY_MS, OUT_DIR


def compute_lorentzian_distance_batch(q_features: np.ndarray, b_features: np.ndarray) -> np.ndarray:
    diff = np.abs(q_features[:, None, :] - b_features[None, :, :])
    return np.sum(np.log1p(diff), axis=-1).astype(np.float32)


def build_knn_cache_for_group(
    features_df: pd.DataFrame,
    group: str,
    score_start_str: str = DEV_SCORE_START,
    score_end_str: str = DEV_END,
    cache_dir: Path | None = None,
) -> Path:
    if cache_dir is None:
        cache_dir = Path(OUT_DIR) / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    out_file = cache_dir / f"knn_{group}.npz"
    if out_file.exists():
        return out_file

    feature_cols = GROUPS[group]
    s_start = int(pd.Timestamp(score_start_str, tz="UTC").timestamp() * 1000)
    s_end = int(pd.Timestamp(score_end_str, tz="UTC").timestamp() * 1000)

    # 1. Bank candidates
    valid_bank = features_df[feature_cols].notna().all(axis=1) & features_df["y"].notna()
    bank_df = features_df[valid_bank].sort_values("open_time").reset_index(drop=True)
    b_times = bank_df["open_time"].to_numpy(dtype=np.int64)
    b_feats = bank_df[feature_cols].to_numpy(dtype=np.float32)
    b_y = bank_df["y"].to_numpy(dtype=np.float32)

    # 2. Query points
    query_mask = (features_df["open_time"] >= s_start) & (features_df["open_time"] < s_end)
    query_df = features_df[query_mask].sort_values(["open_time", "sym"]).reset_index(drop=True)
    q_syms, q_times = query_df["sym"].to_numpy(dtype=str), query_df["open_time"].to_numpy(dtype=np.int64)
    q_feats = query_df[feature_cols].to_numpy(dtype=np.float32)
    q_valid = query_df[feature_cols].notna().all(axis=1).to_numpy()

    n_q, k_max = len(query_df), 128
    nb_dist = np.full((n_q, k_max), np.inf, dtype=np.float32)
    nb_y = np.full((n_q, k_max), np.nan, dtype=np.float32)
    nb_time = np.full((n_q, k_max), -1, dtype=np.int64)
    min_h, max_h = 365 * 86400 * 1000, 3 * 365 * 86400 * 1000

    for i in range(0, n_q, 256):
        end_idx = min(i + 256, n_q)
        batch_dists = compute_lorentzian_distance_batch(q_feats[i:end_idx], b_feats)

        for b_i in range(end_idx - i):
            g_idx = i + b_i
            if not q_valid[g_idx]:
                continue
            qt = q_times[g_idx]
            mask_el = (b_times <= qt - MATURITY_MS) & (b_times >= qt - max_h)
            if not np.any(mask_el) or b_times[mask_el].min() > qt - min_h:
                continue

            row_d = batch_dists[b_i].copy()
            row_d[~mask_el] = np.inf
            k_cur = min(k_max, int(np.sum(mask_el)))
            if k_cur == 0:
                continue

            parts = np.argpartition(row_d, kth=k_cur - 1)[:k_cur]
            sub_d, sub_t, sub_y = row_d[parts], b_times[parts], b_y[parts]
            order = np.lexsort((sub_t, sub_d))[:k_max]
            act_k = len(order)
            nb_dist[g_idx, :act_k] = sub_d[order]
            nb_y[g_idx, :act_k] = sub_y[order]
            nb_time[g_idx, :act_k] = sub_t[order]

    np.savez_compressed(out_file, q_sym=q_syms, q_time=q_times, nb_dist=nb_dist, nb_y=nb_y, nb_time=nb_time)
    return out_file


def predict_knn_from_cache(cache_data: dict, k: int, weight: str, theta: float):
    nb_dist, nb_y = cache_data["nb_dist"][:, :k], cache_data["nb_y"][:, :k]
    q_sym, q_time = cache_data["q_sym"], cache_data["q_time"]
    w = np.ones_like(nb_dist, dtype=np.float32) if weight == "equal" else (1.0 / np.maximum(nb_dist, 1e-6) if weight == "inverse" else None)
    if w is None:
        raise ValueError(f"Unknown weight method '{weight}'")

    valid = np.isfinite(nb_dist) & np.isfinite(nb_y)
    w = np.where(valid, w, 0.0)
    w_sum = np.sum(w, axis=-1)
    scores = np.zeros(len(q_sym), dtype=np.float32)
    has_w = w_sum > 0
    scores[has_w] = np.sum(w[has_w] * np.nan_to_num(nb_y[has_w]), axis=-1) / w_sum[has_w]

    signals = np.zeros(len(q_sym), dtype=np.float32)
    signals[has_w & (scores > theta)] = 1.0
    signals[has_w & (scores < -theta)] = -1.0
    return q_sym, q_time, scores, signals

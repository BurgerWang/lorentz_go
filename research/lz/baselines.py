"""Baseline models: Logistic Regression, LightGBM, and 24h Momentum."""

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from lightgbm import LGBMClassifier

from lz.config import (
    DEV_SCORE_START,
    DEV_END,
    GROUPS,
    MATURITY_MS,
)


def get_monthly_boundaries(
    features_df: pd.DataFrame,
    score_start_str: str = DEV_SCORE_START,
    score_end_str: str = DEV_END,
) -> list[tuple[int, int]]:
    """Return list of (month_start_open_time, month_end_open_time) in scoring period."""
    score_start_ms = int(pd.Timestamp(score_start_str, tz="UTC").timestamp() * 1000)
    score_end_ms = int(pd.Timestamp(score_end_str, tz="UTC").timestamp() * 1000)

    sub = features_df[(features_df["open_time"] >= score_start_ms) & (features_df["open_time"] < score_end_ms)]
    unique_times = sorted(sub["open_time"].unique())

    # Group by (year, month)
    months_dict = {}
    for t in unique_times:
        dt = pd.to_datetime(t, unit="ms", utc=True)
        ym = (dt.year, dt.month)
        if ym not in months_dict:
            months_dict[ym] = []
        months_dict[ym].append(t)

    month_ranges = []
    for ym, times in sorted(months_dict.items()):
        month_ranges.append((min(times), max(times)))
    return month_ranges


def run_momentum_baseline(
    features_df: pd.DataFrame,
    score_start_str: str = DEV_SCORE_START,
    score_end_str: str = DEV_END,
) -> pd.DataFrame:
    """Momentum baseline: signal = sign(ret6)."""
    score_start_ms = int(pd.Timestamp(score_start_str, tz="UTC").timestamp() * 1000)
    score_end_ms = int(pd.Timestamp(score_end_str, tz="UTC").timestamp() * 1000)

    # Compute ret6 per symbol
    dfs = []
    for sym, sdf in features_df.groupby("sym"):
        s_sorted = sdf.sort_values("open_time").copy()
        ret6 = np.log(s_sorted["close"] / s_sorted["close"].shift(6))
        sig = np.sign(ret6).fillna(0.0)
        sub = pd.DataFrame({
            "sym": sym,
            "open_time": s_sorted["open_time"],
            "score": ret6,
            "signal": sig,
        })
        dfs.append(sub)

    res = pd.concat(dfs, ignore_index=True)
    mask = (res["open_time"] >= score_start_ms) & (res["open_time"] < score_end_ms)
    return res[mask].sort_values(["open_time", "sym"]).reset_index(drop=True)


def run_monthly_classifier(
    features_df: pd.DataFrame,
    group: str,
    model_family: str,  # "logistic" or "lgbm"
    theta: float,
    score_start_str: str = DEV_SCORE_START,
    score_end_str: str = DEV_END,
) -> pd.DataFrame:
    """Train monthly models and predict for each month in the scoring window."""
    feature_cols = GROUPS[group]
    month_bounds = get_monthly_boundaries(features_df, score_start_str, score_end_str)

    min_history_ms = 365 * 86400 * 1000
    max_history_ms = 3 * 365 * 86400 * 1000

    out_records = []

    # Valid bank candidates
    valid_bank_mask = features_df[feature_cols].notna().all(axis=1) & features_df["y"].notna()
    bank_df = features_df[valid_bank_mask].copy()

    for m_start_t, m_end_t in month_bounds:
        # Bank for this month
        mask_eligible = (bank_df["open_time"] <= m_start_t - MATURITY_MS) & (
            bank_df["open_time"] >= m_start_t - max_history_ms
        )
        cur_bank = bank_df[mask_eligible]

        # Check warmup requirement (at least 365d of history)
        has_warmup = len(cur_bank) > 0 and (cur_bank["open_time"].min() <= m_start_t - min_history_ms)

        # Month query data
        q_mask = (features_df["open_time"] >= m_start_t) & (features_df["open_time"] <= m_end_t)
        cur_queries = features_df[q_mask].sort_values(["open_time", "sym"]).reset_index(drop=True)

        if not has_warmup or len(cur_bank) < 50:
            # Not enough warmup -> signal = 0
            for row in cur_queries.itertuples():
                out_records.append((row.sym, int(row.open_time), 0.0, 0.0))
            continue

        X_train = cur_bank[feature_cols].to_numpy(dtype=np.float32)
        y_train = cur_bank["y"].to_numpy(dtype=np.float32)

        if model_family == "logistic":
            model = LogisticRegression(C=1.0, max_iter=2000, random_state=42)
        elif model_family == "lgbm":
            model = LGBMClassifier(
                objective="multiclass",
                num_leaves=15,
                min_child_samples=200,
                learning_rate=0.05,
                n_estimators=300,
                subsample=0.8,
                subsample_freq=1,
                colsample_bytree=0.8,
                random_state=42,
                verbose=-1,
            )
        else:
            raise ValueError(f"Unknown model_family '{model_family}'")

        model.fit(X_train, y_train)

        # Get class indices for -1 and +1
        classes = list(model.classes_)
        idx_pos = classes.index(1.0) if 1.0 in classes else -1
        idx_neg = classes.index(-1.0) if -1.0 in classes else -1

        X_test = cur_queries[feature_cols].to_numpy(dtype=np.float32)
        test_valid = cur_queries[feature_cols].notna().all(axis=1).to_numpy()

        scores = np.zeros(len(cur_queries), dtype=np.float32)
        if np.any(test_valid):
            probs = model.predict_proba(X_test[test_valid])
            p_pos = probs[:, idx_pos] if idx_pos != -1 else np.zeros(len(probs), dtype=np.float32)
            p_neg = probs[:, idx_neg] if idx_neg != -1 else np.zeros(len(probs), dtype=np.float32)
            scores[test_valid] = (p_pos - p_neg).astype(np.float32)

        signals = np.zeros(len(cur_queries), dtype=np.float32)
        signals[scores > theta] = 1.0
        signals[scores < -theta] = -1.0

        for i, row in enumerate(cur_queries.itertuples()):
            out_records.append((row.sym, int(row.open_time), float(scores[i]), float(signals[i])))

    return pd.DataFrame(out_records, columns=["sym", "open_time", "score", "signal"])

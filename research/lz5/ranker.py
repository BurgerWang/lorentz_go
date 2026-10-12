"""Cross-sectional ranking and long-short portfolio construction engine for Lorentzian v5.

Mathematical specification:
- Cross-sectional standard scores: z(Mom) and z(Carry)
- Composite score: Score_i = z(Mom_i) + lambda_carry * z(Carry_i)
- Ranking: Top 2 Long (+0.50), Bottom 2 Short (-0.50), Ranks 3..5 Flat (0.0)
- Risk parity weighting: 1 / rv30 normalization per basket
- Strict Dollar neutrality: sum(w) == 0.0000, sum(|w|) == 1.0000
"""

from typing import Mapping
import numpy as np
import pandas as pd

from lz5.config import BAR_MS


def calc_rv30(close: np.ndarray | pd.Series, min_periods: int = 30) -> np.ndarray:
    """Calculate annualized 30-day realized volatility: std(logret, 180) * sqrt(2190)."""
    c_s = pd.Series(close, dtype=np.float64)
    logret = np.log(c_s / c_s.shift(1))
    rv = logret.rolling(180, min_periods=min_periods).std() * np.sqrt(2190.0)
    return rv.to_numpy(dtype=np.float64)


def calc_funding_3d(
    klines: pd.DataFrame,
    funding: pd.DataFrame | None,
    min_periods: int = 1,
) -> np.ndarray:
    """Calculate 3-day (18 4h-bars) rolling mean of settled funding rates causally.

    For each bar t, only settlements strictly before open_time_t (settle_hour < open_time_t)
    are observed.
    """
    n = len(klines)
    if n == 0:
        return np.zeros(0, dtype=np.float64)
    if funding is None or funding.empty or "settle_hour" not in funding.columns or "rate" not in funding.columns:
        return np.zeros(n, dtype=np.float64)

    f_df = funding
    if "sym" in funding.columns and "sym" in klines.columns and len(klines) > 0:
        target_sym = klines["sym"].iloc[0]
        f_df = funding[funding["sym"] == target_sym]
        if f_df.empty:
            return np.zeros(n, dtype=np.float64)

    f_sorted = f_df.drop_duplicates(subset=["settle_hour"], keep="last").sort_values("settle_hour").reset_index(drop=True)
    f_query = pd.DataFrame({"asof_ts": klines["open_time"].astype(np.int64) - 1})
    merged = pd.merge_asof(f_query, f_sorted, left_on="asof_ts", right_on="settle_hour", direction="backward")
    rate_series = merged["rate"].fillna(0.0).astype(np.float64)
    return rate_series.rolling(18, min_periods=min_periods).mean().to_numpy(dtype=np.float64)


def cross_sectional_zscore(values: Mapping[str, float]) -> dict[str, float]:
    """Compute cross-sectional standard score: z(X_i) = (X_i - mean(X)) / (std(X) + 1e-12).

    Uses population standard deviation (ddof=0). If std is zero, returns 0.0 for all.
    """
    if not values:
        return {}
    keys = list(values.keys())
    arr = np.array([values[k] for k in keys], dtype=np.float64)

    # Handle NaN values by treating as 0 or mean
    nan_mask = ~np.isfinite(arr)
    if np.all(nan_mask):
        return {k: 0.0 for k in keys}
    if np.any(nan_mask):
        arr[nan_mask] = np.nanmean(arr)

    mean_val = float(np.mean(arr))
    std_val = float(np.std(arr, ddof=0))
    if std_val <= 1e-12:
        return {k: 0.0 for k in keys}
    z = (arr - mean_val) / std_val
    return {k: float(v) for k, v in zip(keys, z)}


def compute_cross_sectional_scores(
    mom: Mapping[str, float],
    carry: Mapping[str, float],
    lambda_carry: float = 0.5,
) -> dict[str, float]:
    """Compute composite cross-sectional score for each asset in the universe:

        Score_i = z(Mom_i) + lambda_carry * z(Carry_i)

    Args:
        mom: dict mapping symbol -> momentum value (e.g. close_t / close_{t-W} - 1).
        carry: dict mapping symbol -> carry value (e.g. -funding_3d).
        lambda_carry: weight on the funding carry z-score (0.5 or 1.0).

    Returns:
        dict mapping symbol -> composite Score.
    """
    symbols = sorted(set(mom.keys()) & set(carry.keys()))
    if not symbols:
        return {}

    sub_mom = {s: mom[s] for s in symbols}
    sub_carry = {s: carry[s] for s in symbols}

    z_mom = cross_sectional_zscore(sub_mom)
    z_carry = cross_sectional_zscore(sub_carry)

    scores = {s: z_mom[s] + lambda_carry * z_carry[s] for s in symbols}
    return scores


def allocate_long_short_weights(
    scores: Mapping[str, float],
    rv30: Mapping[str, float],
    n_long: int = 2,
    n_short: int = 2,
    target_long_weight: float = 0.50,
    target_short_weight: float = -0.50,
) -> dict[str, float]:
    """Allocate strict dollar-neutral portfolio weights with 1/rv30 risk parity.

    - Sorts universe by Score descending:
      Rank 1..n_long -> Long leg (weights sum to +0.50)
      Rank (N-n_short+1)..N -> Short leg (weights sum to -0.50)
      Middle ranks -> Flat (weight = 0.0)
    - Within each leg, nominal weight w_i proportional to 1 / rv30_i.
    - If rv30 is invalid / non-positive for any asset in a leg, equal weighting fallback is applied.
    - Net nominal exposure = 0.0000, Gross leverage = 1.0000.
    """
    symbols = list(scores.keys())
    n = len(symbols)
    if n < n_long + n_short:
        raise ValueError(
            f"Universe size ({n}) must be at least n_long ({n_long}) + n_short ({n_short})"
        )

    # Deterministic sort: score descending, then symbol ascending
    ranked_symbols = sorted(symbols, key=lambda s: (-scores[s], s))

    long_basket = ranked_symbols[:n_long]
    short_basket = ranked_symbols[n - n_short :]
    neutral_basket = ranked_symbols[n_long : n - n_short]

    weights: dict[str, float] = {s: 0.0 for s in neutral_basket}

    # 1. Long basket weighting (1/rv30 normalized to target_long_weight, or equal weight fallback)
    long_valid = [s for s in long_basket if (s in rv30 and np.isfinite(rv30[s]) and rv30[s] > 1e-6)]
    if len(long_valid) == len(long_basket):
        inv_rv_long = {s: 1.0 / rv30[s] for s in long_basket}
        sum_inv_long = sum(inv_rv_long.values())
        for s in long_basket:
            weights[s] = float(target_long_weight * (inv_rv_long[s] / sum_inv_long))
    else:
        eq_w = target_long_weight / len(long_basket)
        for s in long_basket:
            weights[s] = float(eq_w)

    # 2. Short basket weighting (1/rv30 normalized to target_short_weight, or equal weight fallback)
    short_valid = [s for s in short_basket if (s in rv30 and np.isfinite(rv30[s]) and rv30[s] > 1e-6)]
    if len(short_valid) == len(short_basket):
        inv_rv_short = {s: 1.0 / rv30[s] for s in short_basket}
        sum_inv_short = sum(inv_rv_short.values())
        for s in short_basket:
            weights[s] = float(target_short_weight * (inv_rv_short[s] / sum_inv_short))
    else:
        eq_w = target_short_weight / len(short_basket)
        for s in short_basket:
            weights[s] = float(eq_w)

    return weights


def rank_and_allocate(
    mom: Mapping[str, float],
    carry: Mapping[str, float],
    rv30: Mapping[str, float],
    lambda_carry: float = 0.5,
    n_long: int = 2,
    n_short: int = 2,
) -> tuple[dict[str, float], dict[str, float]]:
    """Single-call convenience helper to score and allocate weights."""
    scores = compute_cross_sectional_scores(mom, carry, lambda_carry)
    weights = allocate_long_short_weights(scores, rv30, n_long=n_long, n_short=n_short)
    return scores, weights

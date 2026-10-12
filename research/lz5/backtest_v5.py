"""Cross-sectional market-neutral momentum & carry backtest engine for Lorentzian v5.

Execution specification:
- Periodic rebalancing every T_reb bars (e.g. 6 or 12 4h-bars).
- Decision timing: bar t closes -> rank & allocate -> execute at open_{t+1}.
- Bar return: r_bar(k) = sum(w_i * (open_{k+1}/open_k - 1)) - Cost(k) - Funding(k).
- Friction accounting: Cost(k) = sum(|Delta w_i| * (fee + slip) * 1e-4 * cost_mult).
- Funding settlement: Funding(k) = sum(w_i * rate_i) during holding window.
- Period-end forced liquidation at the close of the final bar.
"""

from dataclasses import dataclass
from typing import Mapping
import numpy as np
import pandas as pd
from scipy.stats import norm, skew, kurtosis

from lz5.config import (
    BAR_MS,
    DEV_SCORE_START,
    DEV_END,
    one_way_friction,
)
from lz5.ranker import (
    calc_rv30,
    calc_funding_3d,
    allocate_long_short_weights,
    rank_and_allocate,
)


@dataclass
class BacktestResult:
    equity: np.ndarray
    r_bar: np.ndarray
    weights: np.ndarray  # shape: (n_bars, n_symbols)
    symbols: list[str]
    open_times: list[int]
    cost_bar: np.ndarray
    funding_bar: np.ndarray
    gross_r_bar: np.ndarray
    metrics: dict[str, float]
    rebalance_records: list[dict]


def calc_dsr(r_bar: np.ndarray, v_sr: float = 0.0, n_trials: int = 8) -> float:
    """Bailey & López de Prado (2014) Deflated Sharpe Ratio."""
    if len(r_bar) < 2:
        return 0.5
    mean_r, std_r = float(np.mean(r_bar)), float(np.std(r_bar, ddof=1))
    if std_r <= 1e-12:
        return 0.5
    sr_hat = mean_r / std_r
    if n_trials <= 1 or v_sr <= 1e-12:
        sr0 = 0.0
    else:
        em = 0.5772156649
        p1 = min(max(1.0 - 1.0 / n_trials, 1e-6), 1.0 - 1e-6)
        p2 = min(max(1.0 - 1.0 / (n_trials * np.e), 1e-6), 1.0 - 1e-6)
        sr0 = float(np.sqrt(v_sr) * ((1.0 - em) * norm.ppf(p1) + em * norm.ppf(p2)))
    g3, g4 = float(skew(r_bar)), float(kurtosis(r_bar, fisher=False))
    denom_term = 1.0 - g3 * sr_hat + ((g4 - 1.0) / 4.0) * (sr_hat**2)
    denom = 1e-6 if denom_term <= 1e-12 else np.sqrt(denom_term)
    return float(norm.cdf((sr_hat - sr0) * np.sqrt(len(r_bar) - 1) / denom))


def precompute_bar_funding(
    symbols: list[str],
    all_open_times: list[int],
    funding_df: pd.DataFrame | None,
) -> dict[str, np.ndarray]:
    """Sum actual funding rates settling inside each 4h bar window (open_k, open_{k+1}]."""
    n_bars = len(all_open_times)
    bar_funding = {s: np.zeros(n_bars, dtype=np.float64) for s in symbols}
    if funding_df is None or len(funding_df) == 0:
        return bar_funding

    ot_arr = np.array(all_open_times, dtype=np.int64)
    for s in symbols:
        sub = (
            funding_df[funding_df["sym"] == s]
            if "sym" in funding_df.columns
            else (funding_df if len(symbols) == 1 else pd.DataFrame())
        )
        if sub.empty:
            continue
        sh_arr = sub["settle_hour"].to_numpy(dtype=np.int64)
        r_arr = sub["rate"].to_numpy(dtype=np.float64)
        k_indices = np.searchsorted(ot_arr, sh_arr, side="left") - 1
        for sh, rate, k in zip(sh_arr, r_arr, k_indices):
            if 0 <= k < n_bars:
                next_t = ot_arr[k + 1] if k + 1 < n_bars else ot_arr[k] + BAR_MS
                if ot_arr[k] < sh <= next_t:
                    bar_funding[s][k] += rate
    return bar_funding


def calculate_v5_metrics(
    equity: np.ndarray,
    r_bar: np.ndarray,
    cost_bar: np.ndarray,
    funding_bar: np.ndarray,
    gross_r_bar: np.ndarray,
    weights: np.ndarray,
    n_rebalances: int,
    n_trials: int = 8,
    liquidated_at_end: bool = False,
) -> dict[str, float]:
    """Compute summary quantitative performance metrics for Lorentzian v5."""
    n_bars = len(r_bar)
    if n_bars == 0:
        return {
            "sharpe": 0.0, "total_net_return": 0.0, "max_drawdown": 0.0, "gross_sharpe": 0.0,
            "total_cost": 0.0, "total_funding": 0.0, "turnover_total": 0.0, "n_rebalances": 0.0,
            "mean_turnover_per_reb": 0.0, "dsr": 0.5,
        }

    std_r = float(np.std(r_bar, ddof=1)) if len(r_bar) > 1 else 0.0
    sharpe = float(np.mean(r_bar) / std_r * np.sqrt(2190.0)) if std_r > 1e-12 else 0.0
    total_net_return = float(equity[-1] - 1.0)

    peaks = np.maximum.accumulate(equity)
    drawdowns = (peaks - equity) / np.maximum(peaks, 1e-12)
    max_drawdown = float(np.max(drawdowns)) if len(drawdowns) > 0 else 0.0

    std_gross = float(np.std(gross_r_bar, ddof=1)) if len(gross_r_bar) > 1 else 0.0
    gross_sharpe = float(np.mean(gross_r_bar) / std_gross * np.sqrt(2190.0)) if std_gross > 1e-12 else 0.0

    total_cost = float(np.sum(cost_bar))
    total_funding = float(-np.sum(funding_bar))  # positive indicates net funding revenue

    # Total turnover across the run
    delta_w = np.zeros_like(weights)
    delta_w[0] = weights[0]
    if n_bars > 1:
        delta_w[1:] = weights[1:] - weights[:-1]
    turnover_total = float(np.sum(np.abs(delta_w)))
    if liquidated_at_end and n_bars > 0:
        turnover_total += float(np.sum(np.abs(weights[-1])))

    mean_turnover_per_reb = float(turnover_total / max(n_rebalances, 1))
    dsr_val = calc_dsr(r_bar, v_sr=0.0, n_trials=n_trials)

    return {
        "sharpe": sharpe, "total_net_return": total_net_return, "max_drawdown": max_drawdown,
        "gross_sharpe": gross_sharpe, "total_cost": total_cost, "total_funding": total_funding,
        "turnover_total": turnover_total, "n_rebalances": float(n_rebalances),
        "mean_turnover_per_reb": mean_turnover_per_reb, "dsr": dsr_val,
    }


def run_backtest_v5(
    data: pd.DataFrame | Mapping[str, pd.DataFrame],
    w_mom: int = 18,
    t_reb: int = 6,
    lambda_carry: float = 0.5,
    cost_mult: float = 1.0,
    score_start_str: str | None = DEV_SCORE_START,
    score_end_str: str | None = DEV_END,
    funding_df: pd.DataFrame | None = None,
    force_period_end_liquidation: bool = True,
    n_long: int = 2,
    n_short: int = 2,
    shuffle_cross_section: bool = False,
    rng_seed: int | None = None,
) -> BacktestResult:
    """Execute v5 cross-sectional market-neutral backtest.

    Args:
        data: Either dict mapping symbol -> DataFrame, or concatenated DataFrame with 'sym'.
        w_mom: Momentum lookback window in bars (e.g. 18 or 42).
        t_reb: Rebalance cycle in bars (e.g. 6 or 12).
        lambda_carry: Weight on carry z-score (0.5 or 1.0).
        cost_mult: Friction multiplier (1.0, 1.5, 2.0).
        score_start_str: Evaluation period start string (e.g. "2023-01-01"), or None.
        score_end_str: Evaluation period end string (e.g. "2025-01-01"), or None.
        funding_df: Table of historical settled funding rates (if not precomputed).
        force_period_end_liquidation: Whether to liquidate all positions on final bar close.
        n_long: Number of long leg positions (default 2).
        n_short: Number of short leg positions (default 2).

    Returns:
        BacktestResult containing equity, r_bar, weights, metrics, and records.
    """
    # 1. Unpack data into per-symbol DataFrames
    if isinstance(data, pd.DataFrame):
        symbols = sorted(data["sym"].unique())
        dfs = {s: data[data["sym"] == s].sort_values("open_time").reset_index(drop=True) for s in symbols}
    else:
        symbols = sorted(data.keys())
        dfs = {s: data[s].sort_values("open_time").reset_index(drop=True) for s in symbols}

    def _empty(syms: list[str]) -> BacktestResult:
        n = len(syms)
        return BacktestResult(
            equity=np.ones(1), r_bar=np.zeros(0), weights=np.zeros((0, n)), symbols=syms,
            open_times=[], cost_bar=np.zeros(0), funding_bar=np.zeros(0), gross_r_bar=np.zeros(0),
            metrics=calculate_v5_metrics(np.ones(1), np.zeros(0), np.zeros(0), np.zeros(0), np.zeros(0), np.zeros((0, n)), 0),
            rebalance_records=[],
        )

    if not symbols:
        return _empty([])

    # 2. Extract common timestamps
    common_times = set(dfs[symbols[0]]["open_time"])
    for s in symbols[1:]:
        common_times &= set(dfs[s]["open_time"])
    all_times = sorted(common_times)

    if not all_times:
        return _empty(symbols)

    # Reindex all DataFrames to common_times
    aligned_dfs = {}
    for s in symbols:
        df_s = dfs[s][dfs[s]["open_time"].isin(common_times)].copy()
        df_s = df_s.drop_duplicates(subset=["open_time"]).sort_values("open_time").reset_index(drop=True)
        aligned_dfs[s] = df_s

    # 3. Filter evaluation indices by score_start_str and score_end_str
    s_start = int(pd.Timestamp(score_start_str, tz="UTC").timestamp() * 1000) if score_start_str else all_times[0]
    s_end = int(pd.Timestamp(score_end_str, tz="UTC").timestamp() * 1000) if score_end_str else all_times[-1] + BAR_MS

    eval_indices = [idx for idx, t in enumerate(all_times) if s_start <= t < s_end]
    if not eval_indices:
        return _empty(symbols)

    n_eval_bars = len(eval_indices)
    eval_open_times = [all_times[i] for i in eval_indices]

    # Precompute funding settlements per bar if raw funding is provided
    bar_funding_map = precompute_bar_funding(symbols, all_times, funding_df)

    # Pre-extract arrays for speed
    n_sym = len(symbols)
    sym_to_idx = {s: i for i, s in enumerate(symbols)}
    open_mat = np.zeros((len(all_times), n_sym), dtype=np.float64)
    close_mat = np.zeros((len(all_times), n_sym), dtype=np.float64)
    rv30_mat = np.zeros((len(all_times), n_sym), dtype=np.float64)
    mom_mat = np.zeros((len(all_times), n_sym), dtype=np.float64)
    carry_mat = np.zeros((len(all_times), n_sym), dtype=np.float64)
    settle_mat = np.zeros((len(all_times), n_sym), dtype=np.float64)

    for i, s in enumerate(symbols):
        adf = aligned_dfs[s]
        c_arr = adf["close"].to_numpy(dtype=np.float64)
        o_arr = adf["open"].to_numpy(dtype=np.float64)
        open_mat[:, i] = o_arr
        close_mat[:, i] = c_arr

        # rv30
        if "rv30" in adf.columns and not adf["rv30"].isna().all():
            rv30_mat[:, i] = adf["rv30"].to_numpy(dtype=np.float64)
        else:
            rv30_mat[:, i] = calc_rv30(c_arr)

        # mom
        if f"mom_{w_mom}" in adf.columns:
            mom_mat[:, i] = adf[f"mom_{w_mom}"].to_numpy(dtype=np.float64)
        elif "mom" in adf.columns:
            mom_mat[:, i] = adf["mom"].to_numpy(dtype=np.float64)
        else:
            # compute causal momentum close_t / close_{t - w_mom} - 1
            s_c = pd.Series(c_arr)
            mom_mat[:, i] = (s_c / s_c.shift(w_mom) - 1.0).to_numpy(dtype=np.float64)

        # carry = -funding_3d
        if "carry" in adf.columns:
            carry_mat[:, i] = adf["carry"].to_numpy(dtype=np.float64)
        elif "funding_3d" in adf.columns:
            carry_mat[:, i] = -adf["funding_3d"].to_numpy(dtype=np.float64)
        else:
            f3d = calc_funding_3d(adf, funding_df)
            carry_mat[:, i] = -f3d

        # bar funding settlements
        if "bar_funding" in adf.columns:
            settle_mat[:, i] = adf["bar_funding"].to_numpy(dtype=np.float64)
        else:
            settle_mat[:, i] = bar_funding_map[s]

    # Precompute one-way friction per symbol
    fric_arr = np.array([one_way_friction(s, cost_mult) for s in symbols], dtype=np.float64)

    # State variables across evaluation bars
    r_bar = np.zeros(n_eval_bars, dtype=np.float64)
    cost_bar = np.zeros(n_eval_bars, dtype=np.float64)
    funding_bar = np.zeros(n_eval_bars, dtype=np.float64)
    gross_r_bar = np.zeros(n_eval_bars, dtype=np.float64)
    weights_mat = np.zeros((n_eval_bars, n_sym), dtype=np.float64)
    equity = np.ones(n_eval_bars + 1, dtype=np.float64)

    curr_weights = np.zeros(n_sym, dtype=np.float64)
    rebalance_records: list[dict] = []
    rebalance_count = 0

    # 4. Simulation loop over evaluation bars
    for k in range(n_eval_bars):
        global_idx = eval_indices[k]

        # Check rebalance trigger (at k=0 initial entry, and every t_reb bars thereafter)
        is_rebalance_bar = (k % t_reb == 0)

        if is_rebalance_bar:
            rebalance_count += 1
            # Ranking decision time:
            # If a preceding bar exists (global_idx - 1 >= 0), use data at close of global_idx - 1.
            # Else (e.g. synthetic test starting at bar 0 without prior history), use bar 0.
            decision_idx = global_idx - 1 if (global_idx > 0) else global_idx

            mom_d = {s: mom_mat[decision_idx, i] for i, s in enumerate(symbols)}
            carry_d = {s: carry_mat[decision_idx, i] for i, s in enumerate(symbols)}
            rv30_d = {s: rv30_mat[decision_idx, i] for i, s in enumerate(symbols)}

            scores, new_w_dict = rank_and_allocate(
                mom_d, carry_d, rv30_d,
                lambda_carry=lambda_carry,
                n_long=n_long, n_short=n_short,
            )
            if shuffle_cross_section:
                rng = np.random.default_rng(rng_seed + k if rng_seed is not None else None)
                keys = list(scores.keys())
                permuted_keys = list(rng.permutation(keys))
                shuffled_scores = {keys[i]: scores[permuted_keys[i]] for i in range(len(keys))}
                scores = shuffled_scores
                new_w_dict = allocate_long_short_weights(
                    scores, rv30_d,
                    n_long=n_long, n_short=n_short,
                )
            target_weights = np.array([new_w_dict.get(s, 0.0) for s in symbols], dtype=np.float64)

            # Turnover friction at open of bar k
            delta_w = target_weights - curr_weights
            turnover_cost = np.sum(np.abs(delta_w) * fric_arr)
            cost_bar[k] += turnover_cost
            curr_weights = target_weights.copy()

            rebalance_records.append({
                "bar_idx": k,
                "open_time": all_times[global_idx],
                "scores": scores,
                "weights": new_w_dict,
                "turnover_cost": turnover_cost,
            })

        weights_mat[k] = curr_weights.copy()

        # Compute price return during bar k
        # Standard holding: open_{k+1} / open_k - 1
        # When forced liquidation at period-end or final dataset bar: close_k / open_k - 1
        is_final_eval_bar = (k == n_eval_bars - 1)
        if (is_final_eval_bar and force_period_end_liquidation) or (global_idx == len(all_times) - 1):
            px_ret = (close_mat[global_idx] / open_mat[global_idx]) - 1.0
        else:
            px_ret = (open_mat[global_idx + 1] / open_mat[global_idx]) - 1.0

        gross_pnl = float(np.sum(curr_weights * px_ret))
        gross_r_bar[k] = gross_pnl

        # Funding settled during holding window of bar k
        f_cost = float(np.sum(curr_weights * settle_mat[global_idx]))
        funding_bar[k] = f_cost

        # Net return of bar k before liquidation
        bar_ret = gross_pnl - cost_bar[k] - f_cost

        # Period-end forced liquidation on final bar close
        if is_final_eval_bar and force_period_end_liquidation:
            liq_cost = float(np.sum(np.abs(curr_weights) * fric_arr))
            cost_bar[k] += liq_cost
            bar_ret -= liq_cost
            curr_weights = np.zeros(n_sym, dtype=np.float64)

        r_bar[k] = bar_ret
        equity[k + 1] = equity[k] * (1.0 + bar_ret)

    metrics = calculate_v5_metrics(
        equity=equity,
        r_bar=r_bar,
        cost_bar=cost_bar,
        funding_bar=funding_bar,
        gross_r_bar=gross_r_bar,
        weights=weights_mat,
        n_rebalances=rebalance_count,
        n_trials=8,
        liquidated_at_end=force_period_end_liquidation,
    )

    return BacktestResult(
        equity=equity,
        r_bar=r_bar,
        weights=weights_mat,
        symbols=symbols,
        open_times=eval_open_times,
        cost_bar=cost_bar,
        funding_bar=funding_bar,
        gross_r_bar=gross_r_bar,
        metrics=metrics,
        rebalance_records=rebalance_records,
    )

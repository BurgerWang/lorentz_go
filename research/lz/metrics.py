"""Performance metrics and Deflated Sharpe Ratio calculation."""

import numpy as np
import pandas as pd
from scipy.stats import kurtosis, norm, skew


def calc_dsr(
    r_bar: np.ndarray,
    v_sr: float,
    n_trials: int,
) -> float:
    """Calculate Deflated Sharpe Ratio (Bailey & López de Prado 2014) on 4h returns.

    Args:
        r_bar: 1D array of per-bar returns.
        v_sr: Variance of SR_hat across all recorded dev trials.
        n_trials: Cumulative number of dev trials N recorded in LOG.md.

    Returns:
        DSR value in [0, 1].
    """
    t_bars = len(r_bar)
    if t_bars < 2:
        return 0.5

    mean_r = float(np.mean(r_bar))
    std_r = float(np.std(r_bar, ddof=1))
    if std_r <= 1e-12:
        return 0.5

    sr_hat = mean_r / std_r

    if n_trials <= 1 or v_sr <= 1e-12:
        sr0 = 0.0
    else:
        # Euler-Mascheroni approximation of expected maximum
        em = 0.5772156649
        p1 = 1.0 - 1.0 / n_trials
        p2 = 1.0 - 1.0 / (n_trials * np.e)
        # Avoid ppf(1.0) numerical edge case
        p1 = min(max(p1, 1e-6), 1.0 - 1e-6)
        p2 = min(max(p2, 1e-6), 1.0 - 1e-6)
        sr0 = float(np.sqrt(v_sr) * ((1.0 - em) * norm.ppf(p1) + em * norm.ppf(p2)))

    gamma3 = float(skew(r_bar))
    gamma4 = float(kurtosis(r_bar, fisher=False))  # Pearson kurtosis (Normal = 3)

    denom_term = 1.0 - gamma3 * sr_hat + ((gamma4 - 1.0) / 4.0) * (sr_hat**2)
    if denom_term <= 1e-12:
        denom = 1e-6
    else:
        denom = np.sqrt(denom_term)

    z = (sr_hat - sr0) * np.sqrt(t_bars - 1) / denom
    return float(norm.cdf(z))


def calculate_metrics(
    equity: np.ndarray,
    r_bar: np.ndarray,
    trade_records: list[dict],
    per_asset_r_bars: dict[str, np.ndarray],
    v_sr: float = 0.0,
    n_trials: int = 1,
) -> dict:
    """Calculate standard backtest evaluation metrics."""
    t_bars = len(r_bar)
    net_return = float(equity[-1] - 1.0) if len(equity) > 0 else 0.0

    if t_bars > 1:
        mean_r = float(np.mean(r_bar))
        std_r = float(np.std(r_bar, ddof=1))
        sharpe_ann = float((mean_r / std_r) * np.sqrt(2190)) if std_r > 1e-12 else 0.0
    else:
        sharpe_ann = 0.0

    # Max drawdown based on bar equity
    if len(equity) > 0:
        cummax = np.maximum.accumulate(equity)
        dd = (equity - cummax) / np.maximum(cummax, 1e-12)
        max_dd = float(np.abs(np.min(dd)))
    else:
        max_dd = 0.0

    n_trades = len(trade_records)
    if n_trades > 0:
        gross_arr = np.array([tr["gross"] for tr in trade_records], dtype="float64")
        rt_arr = np.array([tr["rt"] for tr in trade_records], dtype="float64")
        gross_mean_bps = float(np.mean(gross_arr) * 1e4)
        rt_mean_bps = float(np.mean(rt_arr) * 1e4)

        if n_trades > 1:
            g_std = float(np.std(gross_arr, ddof=1))
            gross_t = float(np.mean(gross_arr) / (g_std / np.sqrt(n_trades))) if g_std > 1e-12 else 0.0
        else:
            gross_t = 0.0
    else:
        gross_mean_bps = 0.0
        rt_mean_bps = 0.0
        gross_t = 0.0

    per_asset_net = {}
    assets_pos = 0
    for sym, arr in per_asset_r_bars.items():
        sym_net = float(np.sum(arr))
        per_asset_net[sym] = sym_net
        if sym_net > 0:
            assets_pos += 1

    dsr = calc_dsr(r_bar, v_sr=v_sr, n_trials=n_trials)

    return {
        "net_return": net_return,
        "sharpe_ann": sharpe_ann,
        "max_dd": max_dd,
        "n_trades": n_trades,
        "per_asset_net": per_asset_net,
        "assets_pos": assets_pos,
        "gross_mean_bps": gross_mean_bps,
        "rt_mean_bps": rt_mean_bps,
        "gross_t": gross_t,
        "dsr": dsr,
    }

"""Portfolio backtest simulation matching backtest/ledger.go semantics."""

import numpy as np
import pandas as pd
from lz.config import BAR_MS, DEV_SCORE_START, DEV_END, FEE_BPS, SLIP_BPS, DEFAULT_SLIP_BPS, rt


def precompute_bar_funding(symbols: list[str], all_open_times: list[int], funding_df: pd.DataFrame | None) -> dict[str, np.ndarray]:
    n_bars = len(all_open_times)
    bar_funding = {s: np.zeros(n_bars, dtype=np.float64) for s in symbols}
    if funding_df is None or len(funding_df) == 0:
        return bar_funding

    ot_arr = np.array(all_open_times, dtype=np.int64)
    for s in symbols:
        sub = funding_df[funding_df["sym"] == s]
        if sub.empty:
            continue
        sh_arr, r_arr = sub["settle_hour"].to_numpy(dtype=np.int64), sub["rate"].to_numpy(dtype=np.float64)
        k_indices = np.searchsorted(ot_arr, sh_arr, side="left") - 1
        for sh, rate, k in zip(sh_arr, r_arr, k_indices):
            if 0 <= k < n_bars:
                next_t = ot_arr[k + 1] if k + 1 < n_bars else ot_arr[k] + BAR_MS
                if ot_arr[k] < sh <= next_t:
                    bar_funding[s][k] += rate
    return bar_funding


def run_portfolio_backtest(
    features_df: pd.DataFrame,
    signals_df: pd.DataFrame,
    funding_df: pd.DataFrame | None = None,
    cost_mult: float = 1.0,
    score_start_str: str = DEV_SCORE_START,
    score_end_str: str = DEV_END,
) -> tuple[np.ndarray, np.ndarray, list[dict], dict[str, np.ndarray]]:
    s_start, s_end = int(pd.Timestamp(score_start_str, tz="UTC").timestamp() * 1000), int(pd.Timestamp(score_end_str, tz="UTC").timestamp() * 1000)
    sub = features_df[(features_df["open_time"] >= s_start) & (features_df["open_time"] < s_end)].copy()
    symbols, all_times = sorted(sub["sym"].unique()), sorted(sub["open_time"].unique())
    n_bars = len(all_times)
    if n_bars == 0:
        return np.ones(1), np.zeros(0), [], {s: np.zeros(0) for s in symbols}

    sig_map = {(r.sym, int(r.open_time)): float(r.signal) for r in signals_df.itertuples()}
    asset_by_time = {s: sub[sub["sym"] == s].set_index("open_time") for s in symbols}
    bar_funding = precompute_bar_funding(symbols, all_times, funding_df)

    def cost_per_side(s: str) -> float:
        return (FEE_BPS + SLIP_BPS.get(s, DEFAULT_SLIP_BPS)) / 1e4 * cost_mult

    pos = {s: 0.0 for s in symbols}
    weight, entry_px, entry_time = {s: 0.0 for s in symbols}, {s: 0.0 for s in symbols}, {s: 0 for s in symbols}
    exit_bar, trade_funding_sum = {s: -1 for s in symbols}, {s: 0.0 for s in symbols}
    pending_orders, trades = {}, []
    r_bar = np.zeros(n_bars, dtype=np.float64)
    per_asset = {s: np.zeros(n_bars, dtype=np.float64) for s in symbols}
    equity = np.ones(n_bars + 1, dtype=np.float64)

    def record_trade(s: str, ex_px: float, ex_t: int):
        g = pos[s] * (ex_px / entry_px[s] - 1.0)
        trades.append({
            "sym": s, "direction": pos[s], "entry_time": entry_time[s], "entry_px": entry_px[s],
            "exit_time": ex_t, "exit_px": ex_px, "gross": g,
            "net": g - rt(s) * cost_mult - trade_funding_sum[s],
            "rt": rt(s) * cost_mult, "funding_sum": trade_funding_sum[s],
        })

    for k in range(n_bars):
        cur_t = all_times[k]
        next_t = all_times[k + 1] if k + 1 < n_bars else cur_t + BAR_MS
        is_last = k == n_bars - 1

        # 1. Execute scheduled transitions at open[k]
        for s in symbols:
            od = pending_orders.pop(s, None)
            if od is None:
                continue
            act, old_pos, old_w = od["action"], pos[s], weight[s]
            adf = asset_by_time[s]
            if cur_t not in adf.index:
                continue
            cur_op = float(adf.loc[cur_t, "open"])

            if act == "open":
                pos[s], weight[s], entry_px[s], entry_time[s], exit_bar[s], trade_funding_sum[s] = od["dir"], od["weight"], cur_op, cur_t, od["exit_bar"], 0.0
                fee = weight[s] * cost_per_side(s)
                r_bar[k] -= fee
                per_asset[s][k] -= fee
            elif act == "close":
                record_trade(s, cur_op, cur_t)
                fee = old_w * cost_per_side(s)
                r_bar[k] -= fee
                per_asset[s][k] -= fee
                pos[s], weight[s], exit_bar[s] = 0.0, 0.0, -1
            elif act == "reverse":
                record_trade(s, cur_op, cur_t)
                new_dir, new_w = od["dir"], od["weight"]
                pos[s], weight[s], entry_px[s], entry_time[s], exit_bar[s], trade_funding_sum[s] = new_dir, new_w, cur_op, cur_t, od["exit_bar"], 0.0
                fee = (old_w + new_w) * cost_per_side(s)
                r_bar[k] -= fee
                per_asset[s][k] -= fee
            elif act == "continue":
                exit_bar[s] = od["exit_bar"]

        # 2. Position returns and funding during bar k
        for s in symbols:
            if pos[s] == 0:
                continue
            adf = asset_by_time[s]
            if cur_t not in adf.index:
                continue
            cur_op = float(adf.loc[cur_t, "open"])
            px_end = float(adf.loc[cur_t, "close"]) if is_last else (float(adf.loc[next_t, "open"]) if next_t in adf.index else float(adf.loc[cur_t, "close"]))
            p_ret = pos[s] * weight[s] * (px_end / cur_op - 1.0) if cur_op > 0 else 0.0
            f_rate = bar_funding[s][k]
            f_ded = pos[s] * weight[s] * f_rate
            trade_funding_sum[s] += pos[s] * f_rate
            asset_ret = p_ret - f_ded
            r_bar[k] += asset_ret
            per_asset[s][k] += asset_ret

        # 3. Decision at close of bar k
        if not is_last:
            inv_rv30 = {}
            for s in symbols:
                adf = asset_by_time[s]
                if cur_t in adf.index:
                    rv = float(adf.loc[cur_t, "rv30"])
                    if rv > 0 and np.isfinite(rv):
                        inv_rv30[s] = 1.0 / rv
            denom = sum(inv_rv30.values()) if inv_rv30 else 1.0

            for s in symbols:
                s_t, d = sig_map.get((s, cur_t), 0.0), pos[s]
                if d == 0:
                    if s_t != 0.0 and s in inv_rv30:
                        pending_orders[s] = {"action": "open", "dir": s_t, "weight": inv_rv30[s] / denom, "exit_bar": k + 7}
                else:
                    if k + 1 == exit_bar[s]:
                        if s_t == d:
                            pending_orders[s] = {"action": "continue", "exit_bar": k + 7}
                        elif s_t == -d and s in inv_rv30:
                            pending_orders[s] = {"action": "reverse", "dir": -d, "weight": inv_rv30[s] / denom, "exit_bar": k + 7}
                        else:
                            pending_orders[s] = {"action": "close"}
                    elif s_t == -d and s in inv_rv30:
                        pending_orders[s] = {"action": "reverse", "dir": -d, "weight": inv_rv30[s] / denom, "exit_bar": k + 7}
        else:
            for s in symbols:
                if pos[s] != 0:
                    adf = asset_by_time[s]
                    close_px = float(adf.loc[cur_t, "close"])
                    record_trade(s, close_px, cur_t + BAR_MS)
                    fee = weight[s] * cost_per_side(s)
                    r_bar[k] -= fee
                    per_asset[s][k] -= fee
                    pos[s] = 0.0

        equity[k + 1] = equity[k] * (1.0 + r_bar[k])

    return equity, r_bar, trades, per_asset

"""Multi-asset portfolio backtesting engine with dynamic ATR trailing stop and Carry overlay."""
from dataclasses import dataclass
import numpy as np
import pandas as pd

from lz3.config import BAR_MS, DEV_SCORE_START, DEV_END, FEE_BPS, SLIP_BPS, DEFAULT_SLIP_BPS, rt
from lz3.signals import step_signal


@dataclass
class BacktestResult:
    equity: np.ndarray
    r_bar: np.ndarray
    trades: list[dict]
    per_asset_r: dict[str, np.ndarray]
    metrics: dict[str, float]


def precompute_bar_funding(symbols: list[str], all_open_times: list[int], funding_df: pd.DataFrame | None) -> dict[str, np.ndarray]:
    """Sum actual funding rates settling inside each 4h bar window (open_k, open_{k+1}]."""
    n_bars = len(all_open_times)
    bar_funding = {s: np.zeros(n_bars, dtype=np.float64) for s in symbols}
    if funding_df is None or len(funding_df) == 0:
        return bar_funding
    ot_arr = np.array(all_open_times, dtype=np.int64)
    for s in symbols:
        sub = funding_df[funding_df["sym"] == s] if "sym" in funding_df.columns else (funding_df if len(symbols) == 1 else pd.DataFrame())
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


def calculate_metrics(equity: np.ndarray, r_bar: np.ndarray, trades: list[dict], per_asset_r: dict[str, np.ndarray]) -> dict[str, float]:
    """Calculate summary quantitative metrics for evaluation."""
    n_bars = len(r_bar)
    if n_bars == 0:
        return {"sharpe": 0.0, "total_net_return": 0.0, "max_drawdown": 0.0, "gross_mean_bps": 0.0, "net_mean_bps": 0.0, "rt_mean_bps": 0.0, "n_trades": 0.0, "win_rate": 0.0, "positive_assets": 0.0}

    std_r = float(np.std(r_bar, ddof=1)) if len(r_bar) > 1 else 0.0
    sharpe = float(np.mean(r_bar) / std_r * np.sqrt(2190.0)) if std_r > 1e-12 else 0.0
    total_net_return = float(equity[-1] - 1.0)

    peaks = np.maximum.accumulate(equity)
    drawdowns = (peaks - equity) / np.maximum(peaks, 1e-12)
    max_drawdown = float(np.max(drawdowns)) if len(drawdowns) > 0 else 0.0

    n_trades = len(trades)
    if n_trades > 0:
        grosses, nets, rts = [t["gross"] for t in trades], [t["net"] for t in trades], [t["rt"] for t in trades]
        gross_mean_bps, net_mean_bps, rt_mean_bps = float(np.mean(grosses) * 1e4), float(np.mean(nets) * 1e4), float(np.mean(rts) * 1e4)
        win_rate = float(sum(1 for n in nets if n > 0) / n_trades)
    else:
        gross_mean_bps, net_mean_bps, rt_mean_bps, win_rate = 0.0, 0.0, 0.0, 0.0

    pos_asset_count = sum(1 for s, r_arr in per_asset_r.items() if np.prod(1.0 + r_arr) - 1.0 > 0)
    return {
        "sharpe": sharpe, "total_net_return": total_net_return, "max_drawdown": max_drawdown,
        "gross_mean_bps": gross_mean_bps, "net_mean_bps": net_mean_bps, "rt_mean_bps": rt_mean_bps,
        "n_trades": float(n_trades), "win_rate": win_rate, "positive_assets": float(pos_asset_count),
    }


def run_backtest_v3(
    features_df: pd.DataFrame,
    funding_df: pd.DataFrame | None = None,
    atr_mult: float = 2.0,
    carry_mode: str = "Off",
    cost_mult: float = 1.0,
    score_start_str: str = DEV_SCORE_START,
    score_end_str: str = DEV_END,
    max_leverage: float | None = 1.0,
    profit_trail_act_atr: float | None = None,
) -> BacktestResult:
    """Execute v3 multi-asset backtest with dynamic ATR trailing stop and Carry overlay."""
    s_start = int(pd.Timestamp(score_start_str, tz="UTC").timestamp() * 1000)
    s_end = int(pd.Timestamp(score_end_str, tz="UTC").timestamp() * 1000)

    sub = features_df[(features_df["open_time"] >= s_start) & (features_df["open_time"] < s_end)].copy()
    symbols = sorted(sub["sym"].unique())
    all_times = sorted(sub["open_time"].unique())
    n_bars = len(all_times)

    if n_bars == 0:
        return BacktestResult(
            equity=np.ones(1), r_bar=np.zeros(0), trades=[],
            per_asset_r={s: np.zeros(0) for s in symbols},
            metrics=calculate_metrics(np.ones(1), np.zeros(0), [], {}),
        )

    asset_by_time = {s: sub[sub["sym"] == s].set_index("open_time") for s in symbols}
    bar_funding = precompute_bar_funding(symbols, all_times, funding_df)

    def cost_per_side(s: str) -> float:
        return (FEE_BPS + SLIP_BPS.get(s, DEFAULT_SLIP_BPS)) / 1e4 * cost_mult

    pos = {s: 0 for s in symbols}
    weight = {s: 0.0 for s in symbols}
    entry_px = {s: 0.0 for s in symbols}
    entry_time = {s: 0 for s in symbols}
    entry_atr = {s: np.nan for s in symbols}
    trade_funding_sum = {s: 0.0 for s in symbols}
    stop_loss = {s: np.nan for s in symbols}

    pending_orders: dict[str, dict] = {}
    trades: list[dict] = []
    r_bar = np.zeros(n_bars, dtype=np.float64)
    per_asset_r = {s: np.zeros(n_bars, dtype=np.float64) for s in symbols}
    equity = np.ones(n_bars + 1, dtype=np.float64)

    def record_trade(s: str, ex_px: float, ex_t: int, reason: str):
        g = float(pos[s] * (ex_px / entry_px[s] - 1.0))
        rt_c = float(rt(s) * cost_mult)
        f_sum = float(trade_funding_sum[s])
        trades.append({
            "sym": s, "direction": pos[s], "entry_time": entry_time[s], "entry_px": entry_px[s],
            "exit_time": ex_t, "exit_px": ex_px, "gross": g, "rt": rt_c, "funding_sum": f_sum,
            "net": g - rt_c - f_sum, "reason": reason,
        })

    for k in range(n_bars):
        cur_t = all_times[k]
        next_t = all_times[k + 1] if k + 1 < n_bars else cur_t + BAR_MS
        is_last = (k == n_bars - 1)

        # Step 1: Execute Pending Orders at open_k
        for s in symbols:
            od = pending_orders.pop(s, None)
            if od is None:
                continue
            adf = asset_by_time[s]
            if cur_t not in adf.index:
                continue
            cur_op = float(adf.loc[cur_t, "open"])
            act = od["action"]

            if act == "open":
                new_dir, new_w = od["dir"], od["weight"]
                pos[s], weight[s], entry_px[s], entry_time[s] = new_dir, new_w, cur_op, cur_t
                trade_funding_sum[s] = 0.0
                cur_atr = od.get("atr", np.nan)
                entry_atr[s] = cur_atr
                stop_loss[s] = cur_op - new_dir * (atr_mult * cur_atr) if np.isfinite(cur_atr) else np.nan
                fee = new_w * cost_per_side(s)
                r_bar[k] -= fee
                per_asset_r[s][k] -= fee
            elif act == "close":
                record_trade(s, cur_op, cur_t, reason=od.get("reason", "channel_exit"))
                fee = weight[s] * cost_per_side(s)
                r_bar[k] -= fee
                per_asset_r[s][k] -= fee
                pos[s], weight[s], stop_loss[s] = 0, 0.0, np.nan
            elif act == "reverse":
                record_trade(s, cur_op, cur_t, reason="reverse")
                old_w, new_dir, new_w = weight[s], od["dir"], od["weight"]
                pos[s], weight[s], entry_px[s], entry_time[s] = new_dir, new_w, cur_op, cur_t
                trade_funding_sum[s] = 0.0
                cur_atr = od.get("atr", np.nan)
                entry_atr[s] = cur_atr
                stop_loss[s] = cur_op - new_dir * (atr_mult * cur_atr) if np.isfinite(cur_atr) else np.nan
                fee = (old_w + new_w) * cost_per_side(s)
                r_bar[k] -= fee
                per_asset_r[s][k] -= fee

        # Step 2: Intra-bar Stop Loss and Return Simulation
        for s in symbols:
            if pos[s] == 0:
                continue
            adf = asset_by_time[s]
            if cur_t not in adf.index:
                continue
            cur_op = float(adf.loc[cur_t, "open"])
            cur_hi = float(adf.loc[cur_t, "high"])
            cur_lo = float(adf.loc[cur_t, "low"])
            sl = stop_loss[s]

            stopped_out = False
            exit_px = 0.0
            if pos[s] == 1 and np.isfinite(sl) and cur_lo <= sl:
                stopped_out = True
                exit_px = min(cur_op, sl)
            elif pos[s] == -1 and np.isfinite(sl) and cur_hi >= sl:
                stopped_out = True
                exit_px = max(cur_op, sl)

            if stopped_out:
                p_ret = pos[s] * weight[s] * (exit_px / cur_op - 1.0)
                fee = weight[s] * cost_per_side(s)
                f_rate = bar_funding[s][k]
                f_ded = pos[s] * weight[s] * f_rate
                trade_funding_sum[s] += pos[s] * f_rate
                ret = p_ret - fee - f_ded
                r_bar[k] += ret
                per_asset_r[s][k] += ret
                record_trade(s, exit_px, cur_t, reason="stop_loss")
                pos[s], weight[s], stop_loss[s] = 0, 0.0, np.nan
            else:
                px_end = float(adf.loc[cur_t, "close"]) if is_last else (
                    float(adf.loc[next_t, "open"]) if next_t in adf.index else float(adf.loc[cur_t, "close"])
                )
                p_ret = pos[s] * weight[s] * (px_end / cur_op - 1.0) if cur_op > 0 else 0.0
                f_rate = bar_funding[s][k]
                f_ded = pos[s] * weight[s] * f_rate
                trade_funding_sum[s] += pos[s] * f_rate
                ret = p_ret - f_ded
                r_bar[k] += ret
                per_asset_r[s][k] += ret

        # Step 3: Bar Close Decision & State Update
        if is_last:
            for s in symbols:
                if pos[s] != 0:
                    adf = asset_by_time[s]
                    if cur_t in adf.index:
                        close_px = float(adf.loc[cur_t, "close"])
                        fee = weight[s] * cost_per_side(s)
                        r_bar[k] -= fee
                        per_asset_r[s][k] -= fee
                        record_trade(s, close_px, cur_t + BAR_MS, reason="period_end")
                        pos[s], weight[s], stop_loss[s] = 0, 0.0, np.nan
        else:
            # 3a. Update Trailing Stop for currently held positions
            for s in symbols:
                if pos[s] != 0:
                    adf = asset_by_time[s]
                    if cur_t in adf.index:
                        cur_hi = float(adf.loc[cur_t, "high"])
                        cur_lo = float(adf.loc[cur_t, "low"])
                        cur_atr = float(adf.loc[cur_t, "atr"])
                        e_atr = entry_atr.get(s, cur_atr)
                        if pos[s] == 1:
                            can_trail = (profit_trail_act_atr is None) or (
                                np.isfinite(e_atr) and e_atr > 0 and ((cur_hi - entry_px[s]) / e_atr >= profit_trail_act_atr)
                            )
                            if can_trail:
                                new_sl = cur_hi - atr_mult * cur_atr if np.isfinite(cur_atr) else np.nan
                                if np.isnan(stop_loss[s]):
                                    stop_loss[s] = new_sl
                                elif np.isfinite(new_sl):
                                    stop_loss[s] = max(stop_loss[s], new_sl)
                        elif pos[s] == -1:
                            can_trail = (profit_trail_act_atr is None) or (
                                np.isfinite(e_atr) and e_atr > 0 and ((entry_px[s] - cur_lo) / e_atr >= profit_trail_act_atr)
                            )
                            if can_trail:
                                new_sl = cur_lo + atr_mult * cur_atr if np.isfinite(cur_atr) else np.nan
                                if np.isnan(stop_loss[s]):
                                    stop_loss[s] = new_sl
                                elif np.isfinite(new_sl):
                                    stop_loss[s] = min(stop_loss[s], new_sl)

            # 3b. Weight allocation base (1 / rv30)
            inv_rv30 = {}
            for s in symbols:
                adf = asset_by_time[s]
                if cur_t in adf.index:
                    rv = float(adf.loc[cur_t, "rv30"])
                    if rv > 0 and np.isfinite(rv):
                        inv_rv30[s] = 1.0 / rv
            denom = sum(inv_rv30.values()) if inv_rv30 else 1.0

            # 3c. Evaluate signals & compute target orders
            target_signals: dict[str, int] = {}
            raw_weights: dict[str, float] = {}
            for s in symbols:
                adf = asset_by_time[s]
                if cur_t not in adf.index:
                    continue
                row = adf.loc[cur_t]
                c_px, bh, bl, ef, es = float(row["close"]), float(row["breakout_high"]), float(row["breakout_low"]), float(row["ema_fast"]), float(row["ema_slow"])
                f3d = float(row["funding_3d"]) if "funding_3d" in row else 0.0
                sig, mult = step_signal(c_px, bh, bl, ef, es, pos[s], f3d, carry_mode)
                target_signals[s] = sig
                if sig != 0:
                    base_w = (inv_rv30[s] / denom) if s in inv_rv30 else (1.0 / len(symbols))
                    raw_weights[s] = base_w * mult
                else:
                    raw_weights[s] = 0.0

            held_w = sum(weight[s] for s in symbols if pos[s] != 0 and target_signals.get(s, 0) == pos[s])
            avail_w = max(0.0, max_leverage - held_w) if max_leverage is not None else float("inf")
            new_syms = [s for s in symbols if target_signals.get(s, 0) != 0 and target_signals.get(s, 0) != pos[s]]
            tot_new_w = sum(raw_weights.get(s, 0.0) for s in new_syms)
            scale = (avail_w / tot_new_w) if (tot_new_w > avail_w and tot_new_w > 0) else 1.0
            final_weights = {s: raw_weights.get(s, 0.0) * scale for s in new_syms}

            # 3d. Generate pending orders
            for s in symbols:
                adf = asset_by_time[s]
                if cur_t not in adf.index:
                    continue
                cur_atr = float(adf.loc[cur_t, "atr"])
                t_sig, cur_pos, w = target_signals.get(s, 0), pos[s], final_weights.get(s, 0.0)
                if cur_pos == 0 and t_sig != 0:
                    pending_orders[s] = {"action": "open", "dir": t_sig, "weight": w, "atr": cur_atr}
                elif cur_pos != 0:
                    if t_sig == 0:
                        pending_orders[s] = {"action": "close", "reason": "channel_exit"}
                    elif t_sig == -cur_pos:
                        pending_orders[s] = {"action": "reverse", "dir": t_sig, "weight": w, "atr": cur_atr}

        # Step 4: Equity Compounding
        equity[k + 1] = equity[k] * (1.0 + r_bar[k])

    metrics = calculate_metrics(equity, r_bar, trades, per_asset_r)
    return BacktestResult(equity=equity, r_bar=r_bar, trades=trades, per_asset_r=per_asset_r, metrics=metrics)

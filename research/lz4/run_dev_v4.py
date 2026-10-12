"""Development grid runner (16 fixed configurations) and gate evaluator for v4."""

from datetime import datetime, timezone
import json
from pathlib import Path
import numpy as np
import pandas as pd
from scipy.stats import kurtosis, norm, skew

from lz4.backtest_v4 import run_backtest_v4
from lz4.config import DEV_ASSETS, GRID_CONFIGS, OUT_DIR, load_asset_v4
from lz4.indicators import build_indicators_df


def calc_dsr(r_bar: np.ndarray, v_sr: float, n_trials: int) -> float:
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


def run_dev_grid() -> dict:
    """Execute 16 pre-registered dev grid backtests, evaluate G1-G5 gates, and write logs."""
    raw = {s: load_asset_v4(s, split="dev") for s in DEV_ASSETS}
    funding_combined = pd.concat([d["funding"] for d in raw.values()], ignore_index=True)

    unique_params = set((c["l_fast"], c["l_slow"], c["n_breakout"]) for c in GRID_CONFIGS)
    features_by_params = {
        (lf, ls, nb): pd.concat(
            [build_indicators_df(raw[s]["klines"], raw[s]["funding"], lf, ls, nb, sym=s) for s in DEV_ASSETS],
            ignore_index=True,
        )
        for lf, ls, nb in unique_params
    }

    sim_results, sr_hats = [], []
    for cfg in GRID_CONFIGS:
        f_df = features_by_params[(cfg["l_fast"], cfg["l_slow"], cfg["n_breakout"])]
        res = run_backtest_v4(
            f_df,
            funding_combined,
            stop_mode=cfg["stop_mode"],
            carry_mode=cfg["carry_mode"],
            chop_filter=cfg["chop_filter"],
            cost_mult=1.0,
        )
        std_r = float(np.std(res.r_bar, ddof=1)) if len(res.r_bar) > 1 else 0.0
        sr_hat = float(np.mean(res.r_bar) / std_r) if std_r > 1e-12 else 0.0
        sr_hats.append(sr_hat)
        sim_results.append((cfg, res))

    v_sr = float(np.var(sr_hats, ddof=1)) if len(sr_hats) > 1 else 0.0
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")

    records = [
        {
            "id": i, "utc": now_utc, **cfg,
            "n_trades": int(res.metrics["n_trades"]),
            "total_net_return": res.metrics["total_net_return"],
            "net_%": res.metrics["total_net_return"] * 100.0,
            "sharpe": res.metrics["sharpe"],
            "max_drawdown": res.metrics["max_drawdown"],
            "max_dd_%": res.metrics["max_drawdown"] * 100.0,
            "gross_mean_bps": res.metrics["gross_mean_bps"],
            "net_mean_bps": res.metrics["net_mean_bps"],
            "rt_mean_bps": res.metrics["rt_mean_bps"],
            "win_rate": res.metrics["win_rate"],
            "positive_assets": int(res.metrics["positive_assets"]),
            "dsr": calc_dsr(res.r_bar, v_sr=v_sr, n_trials=16),
            "status": "OK",
        }
        for i, (cfg, res) in enumerate(sim_results, start=1)
    ]

    if len(records) > 30:
        raise RuntimeError(f"R7 violation: total dev trials N={len(records)} exceeds cap of 30")

    out_dir = Path(OUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(records).to_csv(out_dir / "dev_v4_results.csv", index=False)

    cand = max(records, key=lambda x: x["sharpe"])
    best_filtered = max((r for r in records if r["chop_filter"] == "ADX_22"), key=lambda x: x["sharpe"])
    best_baseline = max((r for r in records if r["chop_filter"] == "None"), key=lambda x: x["sharpe"])

    cand_res = sim_results[cand["id"] - 1][1]
    cand_per_asset = {s: float(np.prod(1.0 + cand_res.per_asset_r[s]) - 1.0) for s in DEV_ASSETS}
    cand["per_asset_net_return"] = cand_per_asset

    cand_f_df = features_by_params[(cand["l_fast"], cand["l_slow"], cand["n_breakout"])]
    stress_1_5 = run_backtest_v4(cand_f_df, funding_combined, stop_mode=cand["stop_mode"], carry_mode=cand["carry_mode"], chop_filter=cand["chop_filter"], cost_mult=1.5)
    stress_2_0 = run_backtest_v4(cand_f_df, funding_combined, stop_mode=cand["stop_mode"], carry_mode=cand["carry_mode"], chop_filter=cand["chop_filter"], cost_mult=2.0)

    g1 = cand["gross_mean_bps"] > 100.0 and cand["gross_mean_bps"] > 5.0 * cand["rt_mean_bps"]
    g2 = best_filtered["sharpe"] > best_baseline["sharpe"]
    g3 = cand["positive_assets"] >= 5
    g4 = cand["dsr"] >= 0.95
    g5 = stress_2_0.metrics["total_net_return"] > 0.0 and stress_2_0.metrics["sharpe"] >= 0.70
    branch = "a" if (g1 and g2 and g3 and g4 and g5) else ("b" if (not g2 and g1 and g3 and g4 and g5) else "c")

    rng = np.random.default_rng(42)
    f_shuff = cand_f_df.copy()
    for s in DEV_ASSETS:
        f_shuff.loc[f_shuff["sym"] == s, "funding_3d"] = rng.permutation(f_shuff.loc[f_shuff["sym"] == s, "funding_3d"].to_numpy())
    f_fund = funding_combined.copy()
    f_fund["rate"] = rng.permutation(f_fund["rate"].to_numpy())
    p_fund = run_backtest_v4(f_shuff, f_fund, stop_mode=cand["stop_mode"], carry_mode=cand["carry_mode"], chop_filter=cand["chop_filter"])

    f_trend = cand_f_df.copy()
    for s in DEV_ASSETS:
        shift = int(rng.integers(100, 3000))
        for col in ["ema_fast", "ema_slow", "breakout_high", "breakout_low", "adx", "plus_di", "minus_di"]:
            f_trend.loc[f_trend["sym"] == s, col] = np.roll(f_trend.loc[f_trend["sym"] == s, col].to_numpy(), shift)
    p_trend = run_backtest_v4(f_trend, funding_combined, stop_mode=cand["stop_mode"], carry_mode=cand["carry_mode"], chop_filter=cand["chop_filter"])

    selected = {
        "branch": branch, "candidate": cand, "best_filtered": best_filtered, "best_baseline": best_baseline,
        "thresholds": {
            "G1": {"passed": bool(g1), "gross_bps": cand["gross_mean_bps"], "5x_rt_bps": 5.0 * cand["rt_mean_bps"]},
            "G2": {"passed": bool(g2), "filtered_sr": best_filtered["sharpe"], "baseline_sr": best_baseline["sharpe"]},
            "G3": {"passed": bool(g3), "positive_assets": cand["positive_assets"], "per_asset": cand_per_asset},
            "G4": {"passed": bool(g4), "dsr": cand["dsr"]},
            "G5": {"passed": bool(g5), "net_2x": stress_2_0.metrics["total_net_return"], "sr_2x": stress_2_0.metrics["sharpe"]},
        },
        "cost_stress": {
            "1.0x": {"net_return": cand["total_net_return"], "sharpe": cand["sharpe"], "max_drawdown": cand["max_drawdown"]},
            "1.5x": {"net_return": stress_1_5.metrics["total_net_return"], "sharpe": stress_1_5.metrics["sharpe"], "max_drawdown": stress_1_5.metrics["max_drawdown"]},
            "2.0x": {"net_return": stress_2_0.metrics["total_net_return"], "sharpe": stress_2_0.metrics["sharpe"], "max_drawdown": stress_2_0.metrics["max_drawdown"]},
        },
        "placebo": {
            "shuffled_funding": {"net_return": p_fund.metrics["total_net_return"], "sharpe": p_fund.metrics["sharpe"], "gross_mean_bps": p_fund.metrics["gross_mean_bps"]},
            "shuffled_trend": {"net_return": p_trend.metrics["total_net_return"], "sharpe": p_trend.metrics["sharpe"], "gross_mean_bps": p_trend.metrics["gross_mean_bps"]},
        },
        "total_trials_N": len(records),
    }

    with open(out_dir / "dev_v4_selected.json", "w", encoding="utf-8") as f:
        json.dump(selected, f, indent=2)

    per_asset_str = ", ".join(f"{s.replace('USDT','')}: {v*100:+.2f}%" for s, v in cand_per_asset.items())
    with open(Path("research/LOG_V4.md"), "w", encoding="utf-8") as f:
        f.write("# Trial log v4 (dev only). N = number of rows in the table.\n")
        f.write("| id | config_id | lf | ls | nb | chop | stop | trades | net_% | sharpe | max_dd_% | gross_bps | rt_bps | pos_assets | dsr | status |\n")
        f.write("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|\n")
        for r in records:
            f.write(
                f"| {r['id']} | {r['config_id']} | {r['l_fast']} | {r['l_slow']} | {r['n_breakout']} | "
                f"{r['chop_filter']} | {r['stop_mode']} | {r['n_trades']} | {r['net_%']:.2f}% | {r['sharpe']:.2f} | "
                f"{r['max_dd_%']:.2f}% | {r['gross_mean_bps']:.1f} | {r['rt_mean_bps']:.1f} | "
                f"{r['positive_assets']}/7 | {r['dsr']:.4f} | {r['status']} |\n"
            )
        f.write(
            f"\n## Diagnostics\n"
            f"- Total dev trials N: {len(records)}\n"
            f"- Candidate strategy S*: {cand['config_id']} (lf={cand['l_fast']}, ls={cand['l_slow']}, nb={cand['n_breakout']}, chop={cand['chop_filter']}, stop={cand['stop_mode']})\n"
            f"- Branch decision: ({branch})\n"
            f"- Threshold G1 (gross_bps > 100 & > 5*rt_bps): {'PASS' if g1 else 'FAIL'} (gross={cand['gross_mean_bps']:.1f} bps, 5*rt={5.0 * cand['rt_mean_bps']:.1f} bps)\n"
            f"- Threshold G2 (Filtered SR > Baseline SR): {'PASS' if g2 else 'FAIL'} (Filtered SR={best_filtered['sharpe']:.2f} > Baseline SR={best_baseline['sharpe']:.2f})\n"
            f"- Threshold G3 (pos_assets >= 5): {'PASS' if g3 else 'FAIL'} ({cand['positive_assets']}/7 positive assets: {per_asset_str})\n"
            f"- Threshold G4 (DSR >= 0.95): {'PASS' if g4 else 'FAIL'} (DSR={cand['dsr']:.4f})\n"
            f"- Threshold G5 (2.0x cost net > 0 & SR >= 0.70): {'PASS' if g5 else 'FAIL'} (net={stress_2_0.metrics['total_net_return']*100:.2f}%, SR={stress_2_0.metrics['sharpe']:.2f})\n"
            f"- Cost stress: 1.0x (net={cand['net_%']:.2f}%, SR={cand['sharpe']:.2f}), 1.5x (net={stress_1_5.metrics['total_net_return']*100:.2f}%, SR={stress_1_5.metrics['sharpe']:.2f}), 2.0x (net={stress_2_0.metrics['total_net_return']*100:.2f}%, SR={stress_2_0.metrics['sharpe']:.2f})\n"
            f"- Placebo (shuffled funding): net={p_fund.metrics['total_net_return']*100:.2f}%, SR={p_fund.metrics['sharpe']:.2f}, gross_bps={p_fund.metrics['gross_mean_bps']:.1f}\n"
            f"- Placebo (shuffled trend): net={p_trend.metrics['total_net_return']*100:.2f}%, SR={p_trend.metrics['sharpe']:.2f}, gross_bps={p_trend.metrics['gross_mean_bps']:.1f}\n"
        )

    return selected


if __name__ == "__main__":
    sel = run_dev_grid()
    print(f"Phase 2 dev grid completed. Branch: ({sel['branch']}), Candidate: {sel['candidate']['config_id']}")

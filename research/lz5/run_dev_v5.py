"""Development grid runner (8 fixed configurations) and gate evaluator for Lorentzian v5.

Dev-only protocol:
- Evaluates 8 pre-registered cross-sectional market-neutral momentum & carry configs.
- Development universe: 7 assets, 2023-01-01 to 2024-12-31.
- Evaluates G1-G5 gates, cost stress tests (1.5x, 2.0x), and placebo causal tests.
- Outputs dev_v5_results.csv, dev_v5_selected.json, and LOG_V5.md (N <= 20 rows).
"""

from datetime import datetime, timezone
import json
from pathlib import Path
import numpy as np
import pandas as pd

from lz5.backtest_v5 import calc_dsr, run_backtest_v5
from lz5.config import (
    DEV_ASSETS,
    GRID_CONFIGS,
    OUT_DIR,
    load_universe_v5,
)


def run_dev_grid() -> dict:
    """Execute 8 pre-registered dev grid backtests, evaluate G1-G5 gates, and write logs."""
    universe = load_universe_v5(split="dev")
    klines_dict = {s: universe[s]["klines"] for s in DEV_ASSETS}
    funding_combined = pd.concat([universe[s]["funding"] for s in DEV_ASSETS], ignore_index=True)

    sim_results = []
    sr_hats = []

    for cfg in GRID_CONFIGS:
        res = run_backtest_v5(
            data=klines_dict,
            w_mom=cfg["w_mom"],
            t_reb=cfg["t_reb"],
            lambda_carry=cfg["lambda_carry"],
            funding_df=funding_combined,
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
            "net_return": float(res.metrics["total_net_return"]), "net_%": float(res.metrics["total_net_return"] * 100.0),
            "sharpe": float(res.metrics["sharpe"]), "max_drawdown": float(res.metrics["max_drawdown"]),
            "max_dd_%": float(res.metrics["max_drawdown"] * 100.0), "gross_sharpe": float(res.metrics["gross_sharpe"]),
            "total_cost": float(res.metrics["total_cost"]), "total_funding": float(res.metrics["total_funding"]),
            "turnover_total": float(res.metrics["turnover_total"]), "n_rebalances": int(res.metrics["n_rebalances"]),
            "mean_turnover_per_reb": float(res.metrics["mean_turnover_per_reb"]),
            "dsr": float(calc_dsr(res.r_bar, v_sr=v_sr, n_trials=len(GRID_CONFIGS))), "status": "OK",
        }
        for i, (cfg, res) in enumerate(sim_results, start=1)
    ]

    if len(records) > 20:
        raise RuntimeError(f"R7 violation: total dev trials N={len(records)} exceeds cap of 20")

    out_path = Path(OUT_DIR)
    out_path.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(records).to_csv(out_path / "dev_v5_results.csv", index=False)

    cand = max(records, key=lambda x: x["sharpe"])
    cand_cfg = [c for c in GRID_CONFIGS if c["config_id"] == cand["config_id"]][0]

    # Cost stress tests (1.5x and 2.0x) on candidate S*
    stress_1_5 = run_backtest_v5(
        klines_dict,
        w_mom=cand_cfg["w_mom"],
        t_reb=cand_cfg["t_reb"],
        lambda_carry=cand_cfg["lambda_carry"],
        funding_df=funding_combined,
        cost_mult=1.5,
    )
    stress_2_0 = run_backtest_v5(
        klines_dict,
        w_mom=cand_cfg["w_mom"],
        t_reb=cand_cfg["t_reb"],
        lambda_carry=cand_cfg["lambda_carry"],
        funding_df=funding_combined,
        cost_mult=2.0,
    )

    # Placebo causal tests: shuffle cross-sectional asset ranking
    placebo_42 = run_backtest_v5(
        klines_dict,
        w_mom=cand_cfg["w_mom"],
        t_reb=cand_cfg["t_reb"],
        lambda_carry=cand_cfg["lambda_carry"],
        funding_df=funding_combined,
        shuffle_cross_section=True,
        rng_seed=42,
    )

    # G1 to G5 threshold evaluation
    g1 = cand["sharpe"] >= 1.20
    g2 = cand["net_return"] >= 0.30
    g3 = cand["max_drawdown"] <= 0.12
    g4 = cand["dsr"] >= 0.95
    g5 = (stress_2_0.metrics["total_net_return"] > 0.0) and (stress_2_0.metrics["sharpe"] >= 0.80)

    branch = "a" if (g1 and g2 and g3 and g4 and g5) else "b"
    cand_1_0_metrics = {
        "total_net_return": cand["net_return"], "sharpe": cand["sharpe"],
        "max_drawdown": cand["max_drawdown"], "total_cost": cand["total_cost"],
        "total_funding": cand["total_funding"],
    }
    selected = {
        "branch": branch,
        "candidate": cand,
        "thresholds": {
            "G1": {"passed": bool(g1), "metric": "sharpe_1.0x", "value": cand["sharpe"], "threshold": ">= 1.20"},
            "G2": {"passed": bool(g2), "metric": "net_return_2y", "value": cand["net_return"], "threshold": ">= 30.0%"},
            "G3": {"passed": bool(g3), "metric": "max_drawdown", "value": cand["max_drawdown"], "threshold": "<= 12.0%"},
            "G4": {"passed": bool(g4), "metric": "dsr", "value": cand["dsr"], "threshold": ">= 0.95"},
            "G5": {
                "passed": bool(g5),
                "metric": "stress_2.0x",
                "net_2.0x": stress_2_0.metrics["total_net_return"],
                "sharpe_2.0x": stress_2_0.metrics["sharpe"],
                "threshold": "net > 0 and sharpe >= 0.80",
            },
        },
        "cost_stress": {
            k: {
                "net_return": float(m["total_net_return"]), "sharpe": float(m["sharpe"]),
                "max_drawdown": float(m["max_drawdown"]), "total_cost": float(m["total_cost"]),
                "total_funding": float(m["total_funding"]),
            }
            for k, m in [("1.0x", cand_1_0_metrics), ("1.5x", stress_1_5.metrics), ("2.0x", stress_2_0.metrics)]
        },
        "placebo": {
            "shuffled_cross_section_seed_42": {
                "net_return": float(placebo_42.metrics["total_net_return"]),
                "sharpe": float(placebo_42.metrics["sharpe"]),
                "max_drawdown": float(placebo_42.metrics["max_drawdown"]),
                "gross_sharpe": float(placebo_42.metrics["gross_sharpe"]),
                "total_cost": float(placebo_42.metrics["total_cost"]),
                "total_funding": float(placebo_42.metrics["total_funding"]),
            }
        },
        "total_trials_N": len(records),
    }

    with open(out_path / "dev_v5_selected.json", "w", encoding="utf-8") as f:
        json.dump(selected, f, indent=2)

    holdout_sec = ""
    log_file = Path("research/LOG_V5.md")
    if log_file.exists():
        content = log_file.read_text(encoding="utf-8")
        if "## Holdout Diagnostics" in content:
            holdout_sec = "\n" + content[content.index("## Holdout Diagnostics"):].strip() + "\n"

    with open(log_file, "w", encoding="utf-8") as f:
        f.write("# Trial log v5 (CSM-Carry dev grid). N = number of rows in the table.\n")
        f.write("| id | config_id | w_mom | t_reb | lambda_carry | net_% | sharpe | max_dd_% | gross_sr | cost | funding | dsr | status |\n")
        f.write("|---|---|---|---|---|---|---|---|---|---|---|---|---|\n")
        for r in records:
            f.write(
                f"| {r['id']} | {r['config_id']} | {r['w_mom']} | {r['t_reb']} | {r['lambda_carry']} | "
                f"{r['net_%']:.2f}% | {r['sharpe']:.2f} | {r['max_dd_%']:.2f}% | {r['gross_sharpe']:.2f} | "
                f"{r['total_cost']:.3f} | {r['total_funding']:+.3f} | {r['dsr']:.4f} | {r['status']} |\n"
            )
        f.write(
            f"\n## Diagnostics\n"
            f"- Total dev trials N: {len(records)}\n"
            f"- Candidate strategy S*: {cand['config_id']} (w_mom={cand['w_mom']}, t_reb={cand['t_reb']}, lambda_carry={cand['lambda_carry']})\n"
            f"- Branch decision: ({branch})\n"
            f"- Threshold G1 (Sharpe >= 1.20 @ 1.0x): {'PASS' if g1 else 'FAIL'} (Sharpe={cand['sharpe']:.2f})\n"
            f"- Threshold G2 (2y Net Return >= 30.0%): {'PASS' if g2 else 'FAIL'} (Net={cand['net_%']:.2f}%)\n"
            f"- Threshold G3 (Max Drawdown <= 12.0%): {'PASS' if g3 else 'FAIL'} (MaxDD={cand['max_dd_%']:.2f}%)\n"
            f"- Threshold G4 (DSR >= 0.95 with N=8): {'PASS' if g4 else 'FAIL'} (DSR={cand['dsr']:.4f})\n"
            f"- Threshold G5 (2.0x cost net > 0 & SR >= 0.80): {'PASS' if g5 else 'FAIL'} (net={stress_2_0.metrics['total_net_return']*100:.2f}%, SR={stress_2_0.metrics['sharpe']:.2f})\n"
            f"- Cost stress: 1.0x (net={cand['net_%']:.2f}%, SR={cand['sharpe']:.2f}, DD={cand['max_dd_%']:.2f}%), "
            f"1.5x (net={stress_1_5.metrics['total_net_return']*100:.2f}%, SR={stress_1_5.metrics['sharpe']:.2f}, DD={stress_1_5.metrics['max_drawdown']*100:.2f}%), "
            f"2.0x (net={stress_2_0.metrics['total_net_return']*100:.2f}%, SR={stress_2_0.metrics['sharpe']:.2f}, DD={stress_2_0.metrics['max_drawdown']*100:.2f}%)\n"
            f"- Placebo (shuffled cross-section ranking seed 42): net={placebo_42.metrics['total_net_return']*100:.2f}%, SR={placebo_42.metrics['sharpe']:.2f}, gross_SR={placebo_42.metrics['gross_sharpe']:.2f}, DD={placebo_42.metrics['max_drawdown']*100:.2f}%\n"
        )
        if holdout_sec:
            f.write(holdout_sec)

    return selected


if __name__ == "__main__":
    sel = run_dev_grid()
    print(f"Phase 2 dev grid completed. Branch: ({sel['branch']}), Candidate: {sel['candidate']['config_id']}")

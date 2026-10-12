"""Holdout evaluation runner for Lorentzian v5 (H1 and H2 blind tests).

Single-shot blind test of the candidate frozen by the dev grid:
- Guard: requires LZ_HOLDOUT_APPROVED='yes' and prevents repeated runs (R12).
- Candidate: the grid cell selected by run_dev_v5 (research/out/dev_v5_selected.json).
- H1: 7 dev assets, 2025-01-01 to 2026-10-01. Threshold: 1.5x net > 0 & MaxDD <= 15.0%.
- H2: 8 assets (with SOL), 2023-01-01 to 2026-10-01. Threshold: 1.0x net > 0 & Sharpe >= 1.0.
- Output: research/out/holdout_v5_done.json & updates research/LOG_V5.md.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
import pandas as pd

from lz5.backtest_v5 import run_backtest_v5
from lz5.config import (
    ALL_ASSETS,
    DEV_ASSETS,
    GRID_CONFIGS,
    HOLDOUT_END,
    OUT_DIR,
    load_universe_v5,
)

DONE_FILE = Path(OUT_DIR) / "holdout_v5_done.json"
LOG_FILE = Path("research/LOG_V5.md")
SELECTED_FILE = Path(OUT_DIR) / "dev_v5_selected.json"


def load_candidate() -> dict:
    """Return the grid cell frozen by the dev run."""
    if not SELECTED_FILE.exists():
        raise FileNotFoundError(f"Missing {SELECTED_FILE}; run lz5.run_dev_v5 first")
    with open(SELECTED_FILE, "r", encoding="utf-8") as f:
        config_id = json.load(f)["candidate"]["config_id"]
    return next(c for c in GRID_CONFIGS if c["config_id"] == config_id)


def run_holdout() -> dict:
    if os.environ.get("LZ_HOLDOUT_APPROVED") != "yes":
        raise PermissionError("Holdout access requires environment variable LZ_HOLDOUT_APPROVED='yes'")
    if DONE_FILE.exists():
        raise RuntimeError(f"Holdout already executed! {DONE_FILE} exists (R12 anti-replay).")

    cand = load_candidate()
    universe = load_universe_v5(split="holdout")
    f_all = pd.concat([universe[s]["funding"] for s in ALL_ASSETS], ignore_index=True)

    def _eval_suite(assets: list[str], start_s: str, f_df: pd.DataFrame) -> dict:
        kl = {s: universe[s]["klines"] for s in assets}
        out = {}
        for cm in [1.0, 1.5, 2.0]:
            r = run_backtest_v5(
                kl, w_mom=cand["w_mom"], t_reb=cand["t_reb"], lambda_carry=cand["lambda_carry"],
                cost_mult=cm, score_start_str=start_s, score_end_str=HOLDOUT_END,
                funding_df=f_df, n_long=2, n_short=2,
            )
            m = r.metrics
            out[f"{cm}x"] = {
                "net_return": float(m["total_net_return"]), "net_%": float(m["total_net_return"] * 100.0),
                "sharpe": float(m["sharpe"]), "max_drawdown": float(m["max_drawdown"]),
                "max_dd_%": float(m["max_drawdown"] * 100.0), "gross_sharpe": float(m["gross_sharpe"]),
                "total_cost": float(m["total_cost"]), "total_funding": float(m["total_funding"]),
                "turnover_total": float(m["turnover_total"]), "n_rebalances": int(m["n_rebalances"]),
                "mean_turnover_per_reb": float(m["mean_turnover_per_reb"]), "dsr": float(m["dsr"]),
            }
        return out

    # 1. H1: 7 dev assets (2025-01-01 to 2026-10-01)
    f_h1 = f_all[f_all["sym"].isin(DEV_ASSETS)].copy()
    h1_metrics = _eval_suite(DEV_ASSETS, "2025-01-01", f_h1)
    h1_pass = (h1_metrics["1.5x"]["net_return"] > 0.0) and (h1_metrics["1.5x"]["max_drawdown"] <= 0.15)

    # 2. H2: 8 assets with SOL (2023-01-01 to 2026-10-01)
    h2_metrics = _eval_suite(ALL_ASSETS, "2023-01-01", f_all)
    h2_pass = (h2_metrics["1.0x"]["net_return"] > 0.0) and (h2_metrics["1.0x"]["sharpe"] >= 1.0)

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    overall_pass = h1_pass and h2_pass

    res = {
        "status": "DONE", "timestamp": now_utc, "champion_config": cand,
        "H1": {"passed": bool(h1_pass), "threshold": "1.5x net > 0 and MaxDD <= 15.0%", "metrics": h1_metrics},
        "H2": {"passed": bool(h2_pass), "threshold": "1.0x net > 0 and Sharpe >= 1.0", "metrics": h2_metrics},
        "overall_passed": bool(overall_pass),
    }

    h1_sub = "\n".join(f"  * {k}: net={m['net_%']:.2f}%, SR={m['sharpe']:.2f}, DD={m['max_dd_%']:.2f}%" for k, m in h1_metrics.items())
    h2_sub = "\n".join(f"  * {k}: net={m['net_%']:.2f}%, SR={m['sharpe']:.2f}, DD={m['max_dd_%']:.2f}%" for k, m in h2_metrics.items())
    diag_block = (
        f"## Holdout Diagnostics (Phase 3)\n- Executed at: {now_utc}\n"
        f"- Candidate evaluated: {cand['config_id']} (w_mom={cand['w_mom']}, t_reb={cand['t_reb']}, lambda_carry={cand['lambda_carry']})\n"
        f"- H1 (7 dev assets 2025-2026 OOS): {'PASS' if h1_pass else 'FAIL'} (1.5x net={h1_metrics['1.5x']['net_%']:.2f}%, MaxDD={h1_metrics['1.5x']['max_dd_%']:.2f}%)\n"
        f"{h1_sub}\n"
        f"- H2 (8 assets with SOL 2023-2026 full sample): {'PASS' if h2_pass else 'FAIL'} (1.0x net={h2_metrics['1.0x']['net_%']:.2f}%, SR={h2_metrics['1.0x']['sharpe']:.2f})\n"
        f"{h2_sub}\n- Overall Holdout Verdict: {'PASS' if overall_pass else 'FAIL'}\n"
    )

    if LOG_FILE.exists():
        content = LOG_FILE.read_text(encoding="utf-8")
        if "## Holdout Diagnostics" in content:
            content = content[: content.index("## Holdout Diagnostics")].rstrip() + "\n\n"
        elif not content.endswith("\n\n"):
            content = content.rstrip() + "\n\n"
        content += diag_block
        LOG_FILE.write_text(content, encoding="utf-8")
    else:
        LOG_FILE.write_text(diag_block, encoding="utf-8")

    DONE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(DONE_FILE, "w", encoding="utf-8") as f:
        json.dump(res, f, indent=2)
    return res


if __name__ == "__main__":
    out = run_holdout()
    print(f"Holdout run completed. H1: {out['H1']['passed']}, H2: {out['H2']['passed']}, Overall: {out['overall_passed']}")

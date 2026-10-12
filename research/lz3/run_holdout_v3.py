"""Holdout blind test runner for Lorentzian v3 (Phase 3)."""
import json, os
from datetime import datetime, timezone
from pathlib import Path
import numpy as np, pandas as pd
from lz3.backtest_v3 import run_backtest_v3
from lz3.config import DEV_ASSETS, HOLDOUT_ASSET, OUT_DIR, load_asset_v3
from lz3.indicators import build_indicators_df

HOLDOUT_DONE_FILE = Path(OUT_DIR) / "holdout_v3_done.json"
LOG_V3_FILE = Path("research/LOG_V3.md")
SELECTED_FILE = Path(OUT_DIR) / "dev_v3_selected.json"
CANDIDATE_KEYS = ("config_id", "l_fast", "l_slow", "n_breakout", "atr_mult", "carry_mode")

def summarize_run(res, assets: list[str]) -> dict:
    m = dict(res.metrics)
    tds = res.trades
    h_days = [(t["exit_time"] - t["entry_time"]) / (24 * 3600 * 1000.0) for t in tds] if tds else []
    return {
        "metrics": m,
        "avg_holding_days": float(np.mean(h_days)) if h_days else 0.0,
        "stop_loss_rate": float(sum(1 for t in tds if t.get("reason") == "stop_loss") / len(tds)) if tds else 0.0,
        "per_asset_returns": {s: float(np.prod(1.0 + res.per_asset_r[s]) - 1.0) for s in assets},
    }

def run_holdout() -> dict:
    if HOLDOUT_DONE_FILE.exists():
        raise RuntimeError(f"Guard: {HOLDOUT_DONE_FILE} exists. Refusing re-run.")
    if os.environ.get("LZ_HOLDOUT_APPROVED") != "yes":
        raise PermissionError("LZ_HOLDOUT_APPROVED != 'yes'. Explicit user approval required.")

    if not SELECTED_FILE.exists():
        raise FileNotFoundError(f"Missing {SELECTED_FILE}; run lz3.run_dev_v3 first")
    with open(SELECTED_FILE, "r", encoding="utf-8") as f:
        selected = json.load(f)["candidate"]
    cand = {k: selected[k] for k in CANDIDATE_KEYS}

    # H1: 7 Dev Assets OOS 2025-2026
    raw_dev = {s: load_asset_v3(s, split="holdout") for s in DEV_ASSETS}
    f_df_h1 = pd.concat([build_indicators_df(raw_dev[s]["klines"], raw_dev[s]["funding"], cand["l_fast"], cand["l_slow"], cand["n_breakout"], sym=s) for s in DEV_ASSETS], ignore_index=True)
    funding_h1 = pd.concat([d["funding"] for d in raw_dev.values()], ignore_index=True)
    h1_runs = {
        f"{cm:.1f}x": summarize_run(run_backtest_v3(f_df_h1, funding_h1, atr_mult=cand["atr_mult"], carry_mode=cand["carry_mode"], cost_mult=cm, score_start_str="2025-01-01", score_end_str="2026-10-01"), DEV_ASSETS)
        for cm in [1.0, 1.5, 2.0]
    }
    h1_15 = h1_runs["1.5x"]
    h1_passed = bool(h1_15["metrics"]["total_net_return"] > 0.0 and h1_15["metrics"]["positive_assets"] >= 4)

    # H2: SOLUSDT 2023-2026
    raw_sol = load_asset_v3(HOLDOUT_ASSET, split="holdout")
    f_df_h2 = build_indicators_df(raw_sol["klines"], raw_sol["funding"], cand["l_fast"], cand["l_slow"], cand["n_breakout"], sym=HOLDOUT_ASSET)
    h2_runs = {
        f"{cm:.1f}x": summarize_run(run_backtest_v3(f_df_h2, raw_sol["funding"], atr_mult=cand["atr_mult"], carry_mode=cand["carry_mode"], cost_mult=cm, score_start_str="2023-01-01", score_end_str="2026-10-01"), [HOLDOUT_ASSET])
        for cm in [1.0, 1.5, 2.0]
    }
    h2_10 = h2_runs["1.0x"]
    h2_passed = bool(h2_10["metrics"]["total_net_return"] > 0.0 and h2_10["metrics"]["gross_mean_bps"] > 2.0 * h2_10["metrics"]["rt_mean_bps"])

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    overall = "PASS" if (h1_passed and h2_passed) else "FAIL"
    out = {
        "timestamp": now_utc, "candidate": cand, "overall_verdict": overall,
        "H1": {"passed": h1_passed, "criteria": "1.5x cost net > 0 & pos_assets >= 4", "stress_runs": h1_runs},
        "H2": {"passed": h2_passed, "criteria": "1.0x cost net > 0 & gross > 2*rt", "stress_runs": h2_runs},
    }
    HOLDOUT_DONE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(HOLDOUT_DONE_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)

    content = LOG_V3_FILE.read_text(encoding="utf-8") if LOG_V3_FILE.exists() else ""
    h1_per_str = ", ".join(f"{s.replace('USDT','')}: {v*100:+.2f}%" for s, v in h1_15["per_asset_returns"].items())
    diag = (
        f"\n## Holdout Diagnostics (Phase 3 - {now_utc})\n"
        f"- Candidate: {cand['config_id']} (lf={cand['l_fast']}, ls={cand['l_slow']}, nb={cand['n_breakout']}, atr={cand['atr_mult']}, carry={cand['carry_mode']})\n"
        f"- Overall Verdict: {overall}\n"
        f"- H1 (7 Dev Assets OOS 2025-2026): {'PASS' if h1_passed else 'FAIL'}\n"
        f"  - 1.0x: Net={h1_runs['1.0x']['metrics']['total_net_return']*100:+.2f}%, SR={h1_runs['1.0x']['metrics']['sharpe']:.2f}, Gross={h1_runs['1.0x']['metrics']['gross_mean_bps']:.1f}bps, Trades={int(h1_runs['1.0x']['metrics']['n_trades'])}, AvgHold={h1_runs['1.0x']['avg_holding_days']:.1f}d, SL%={h1_runs['1.0x']['stop_loss_rate']*100:.1f}%\n"
        f"  - 1.5x: Net={h1_15['metrics']['total_net_return']*100:+.2f}%, SR={h1_15['metrics']['sharpe']:.2f}, Gross={h1_15['metrics']['gross_mean_bps']:.1f}bps, PosAssets={int(h1_15['metrics']['positive_assets'])}/7 ({h1_per_str})\n"
        f"  - 2.0x: Net={h1_runs['2.0x']['metrics']['total_net_return']*100:+.2f}%, SR={h1_runs['2.0x']['metrics']['sharpe']:.2f}, Gross={h1_runs['2.0x']['metrics']['gross_mean_bps']:.1f}bps\n"
        f"- H2 (SOLUSDT Full-Period 2023-2026): {'PASS' if h2_passed else 'FAIL'}\n"
        f"  - 1.0x: Net={h2_10['metrics']['total_net_return']*100:+.2f}%, SR={h2_10['metrics']['sharpe']:.2f}, Gross={h2_10['metrics']['gross_mean_bps']:.1f}bps, RT={h2_10['metrics']['rt_mean_bps']:.1f}bps, Trades={int(h2_10['metrics']['n_trades'])}, AvgHold={h2_10['avg_holding_days']:.1f}d, SL%={h2_10['stop_loss_rate']*100:.1f}%\n"
        f"  - 1.5x: Net={h2_runs['1.5x']['metrics']['total_net_return']*100:+.2f}%, SR={h2_runs['1.5x']['metrics']['sharpe']:.2f}, Gross={h2_runs['1.5x']['metrics']['gross_mean_bps']:.1f}bps\n"
        f"  - 2.0x: Net={h2_runs['2.0x']['metrics']['total_net_return']*100:+.2f}%, SR={h2_runs['2.0x']['metrics']['sharpe']:.2f}, Gross={h2_runs['2.0x']['metrics']['gross_mean_bps']:.1f}bps\n"
    )
    if "## Holdout Diagnostics" in content:
        prefix = content.split("## Holdout Diagnostics")[0].rstrip()
        remainder = content.split("## Holdout Diagnostics", 1)[1]
        next_h2_idx = remainder.find("\n## ")
        suffix = remainder[next_h2_idx:] if next_h2_idx != -1 else ""
        new_content = prefix + "\n" + diag + suffix
    else:
        new_content = content.rstrip() + "\n" + diag
    LOG_V3_FILE.write_text(new_content, encoding="utf-8")
    return out

if __name__ == "__main__":
    o = run_holdout()
    print(f"Holdout blind test finished: Overall={o['overall_verdict']}, H1={o['H1']['passed']}, H2={o['H2']['passed']}")

"""Holdout evaluation (Phase 5). Only executable with user approval."""

import json
import os
from pathlib import Path
import numpy as np
import pandas as pd
from lz.backtest import run_portfolio_backtest
from lz.baselines import run_momentum_baseline, run_monthly_classifier
from lz.config import DEV_ASSETS, HOLDOUT_ASSET, DEV_END, HOLDOUT_END, OUT_DIR
from lz.features import build_features
from lz.knn import build_knn_cache_for_group, predict_knn_from_cache
from lz.load import load_asset
from lz.metrics import calculate_metrics


def generate_holdout_signals(df: pd.DataFrame, cand: dict, s_start: str, s_end: str, c_dir: Path) -> pd.DataFrame:
    fam = cand["family"]
    if fam == "KNN":
        cache = np.load(build_knn_cache_for_group(df, group=cand["group"], score_start_str=s_start, score_end_str=s_end, cache_dir=c_dir))
        qs, qt, _, sigs = predict_knn_from_cache(cache, k=int(cand["k"]), weight=cand["weight"], theta=float(cand["theta"]))
        return pd.DataFrame({"sym": qs, "open_time": qt, "signal": sigs})
    elif fam in ("LOGIT", "LGBM"):
        mf = "logistic" if fam == "LOGIT" else "lgbm"
        return run_monthly_classifier(df, group=cand["group"], model_family=mf, theta=float(cand["theta"]), score_start_str=s_start, score_end_str=s_end)
    elif fam == "MOM":
        return run_momentum_baseline(df, score_start_str=s_start, score_end_str=s_end)
    raise ValueError(f"Unknown family {fam}")


def run_holdout():
    out_dir, done_file = Path(OUT_DIR), Path(OUT_DIR) / "holdout_done.json"
    if done_file.exists():
        raise RuntimeError("Holdout has already been executed (R12).")
    if os.environ.get("LZ_HOLDOUT_APPROVED") != "yes":
        raise PermissionError("Holdout evaluation requires explicit user approval: LZ_HOLDOUT_APPROVED='yes'.")

    sel_file = out_dir / "dev_selected.json"
    if not sel_file.exists():
        raise FileNotFoundError(f"Missing {sel_file}")
    with open(sel_file, "r", encoding="utf-8") as f:
        cand = json.load(f).get("candidate")
    if cand is None:
        raise ValueError("No candidate selected.")

    f_hold_file = out_dir / "features_holdout.parquet"
    feat_df = pd.read_parquet(f_hold_file) if f_hold_file.exists() else build_features(split="holdout")
    f_dfs = []
    for s in DEV_ASSETS + [HOLDOUT_ASSET]:
        f_sub = load_asset(s, split="holdout")["funding"].copy()
        f_sub["sym"] = s
        f_dfs.append(f_sub)
    funding_all = pd.concat(f_dfs, ignore_index=True)

    # 1. H1: 7 dev assets [2025-01-01, 2026-10-01)
    dev_feat = feat_df[feat_df["sym"].isin(DEV_ASSETS)].copy()
    h1_sig = generate_holdout_signals(dev_feat, cand, DEV_END, HOLDOUT_END, out_dir / "cache_holdout_h1")
    h1_res = {f"{cm}x": calculate_metrics(*run_portfolio_backtest(dev_feat, h1_sig, funding_all, cost_mult=cm, score_start_str=DEV_END, score_end_str=HOLDOUT_END)) for cm in [1.0, 1.5, 2.0]}
    h1_passed = bool(h1_res["1.5x"]["net_return"] > 0 and h1_res["1.0x"]["assets_pos"] >= 4)

    # 2. H2: SOLUSDT [2023-01-01, 2026-10-01)
    all_h2_sig = generate_holdout_signals(feat_df, cand, "2023-01-01", HOLDOUT_END, out_dir / "cache_holdout_h2")
    sol_feat = feat_df[feat_df["sym"] == HOLDOUT_ASSET].copy()
    h2_sig = all_h2_sig[all_h2_sig["sym"] == HOLDOUT_ASSET].reset_index(drop=True)
    h2_res = {f"{cm}x": calculate_metrics(*run_portfolio_backtest(sol_feat, h2_sig, funding_all, cost_mult=cm, score_start_str="2023-01-01", score_end_str=HOLDOUT_END)) for cm in [1.0, 1.5, 2.0]}
    h2_passed = bool(h2_res["1.0x"]["net_return"] > 0 and h2_res["1.0x"]["gross_mean_bps"] > h2_res["1.0x"]["rt_mean_bps"])

    out = {"candidate": cand, "h1": {"passed": h1_passed, "results": h1_res}, "h2": {"passed": h2_passed, "results": h2_res}, "all_passed": bool(h1_passed and h2_passed)}
    with open(done_file, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, default=str)

    with open(Path("research/LOG.md"), "a", encoding="utf-8") as f:
        f.write(f"\n### Holdout Results\n- H1: passed={h1_passed}, 1.0x={h1_res['1.0x']['net_return']:.2%}, 1.5x={h1_res['1.5x']['net_return']:.2%}, pos={h1_res['1.0x']['assets_pos']}/7\n")
        f.write(f"- H2: passed={h2_passed}, 1.0x={h2_res['1.0x']['net_return']:.2%}, gross_bps={h2_res['1.0x']['gross_mean_bps']:.1f}, rt_bps={h2_res['1.0x']['rt_mean_bps']:.1f}\n")
        f.write(f"- Conclusion: {'SUCCESS -> Proceed to Phase 6' if (h1_passed and h2_passed) else 'TERMINATED -> No edge proven on holdout'}\n")
    return out


if __name__ == "__main__":
    run_holdout()

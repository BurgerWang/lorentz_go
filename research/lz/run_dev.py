"""Run 161 fixed dev configurations, evaluate thresholds, and write LOG.md."""

from datetime import datetime, timezone
import json
from pathlib import Path
import numpy as np
import pandas as pd

from lz.backtest import run_portfolio_backtest
from lz.baselines import run_momentum_baseline, run_monthly_classifier
from lz.config import (
    DEV_ASSETS,
    DEV_SCORE_START,
    DEV_END,
    GROUPS,
    MIN_TRADES,
    OUT_DIR,
    TRIAL_CAP,
)
from lz.features import build_features
from lz.knn import build_knn_cache_for_group, predict_knn_from_cache
from lz.load import load_asset
from lz.metrics import calculate_metrics, calc_dsr


def count_log_trials(log_file: Path) -> int:
    if not log_file.exists():
        return 0
    with open(log_file, "r", encoding="utf-8") as f:
        lines = f.readlines()
    count, in_table = 0, False
    for line in lines:
        s = line.strip()
        if s.startswith("## Diagnostics"):
            break
        if s.startswith("| id |"):
            in_table = True
            continue
        if in_table and not s.startswith("|---") and s.startswith("|") and s.endswith("|"):
            count += 1
    return count


def append_log_row(log_file: Path, r: dict):
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(
            f"| {r['id']} | {r['utc']} | {r['family']} | {r['group']} | {r['k']} | {r['weight']} | {r['theta']} | "
            f"{r['n_trades']} | {r['net_%']:.2f}% | {r['sharpe']:.2f} | {r['gross_bps']:.1f} | {r['rt_bps']:.1f} | "
            f"{r['assets_pos']} | {r['status']} |\n"
        )


def run_dev_grid(rerun: bool = False):
    log_file = Path("research/LOG.md")
    current_trials = count_log_trials(log_file)
    if rerun:
        current_trials = 0
        with open(log_file, "w", encoding="utf-8") as f:
            f.write("# Trial log (dev only). N = number of rows in the table.\n| id | utc | family | group | k | weight | theta | n_trades | net_% | sharpe | gross_bps | rt_bps | assets_pos | status |\n|---|---|---|---|---|---|---|---|---|---|---|---|---|---|\n")

    if current_trials + 161 > TRIAL_CAP and current_trials != 161:
        raise RuntimeError(f"Trial cap violation: current={current_trials}, cap={TRIAL_CAP}")

    out_dir = Path(OUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = out_dir / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    feat_file = out_dir / "features_dev.parquet"
    features_df = pd.read_parquet(feat_file) if feat_file.exists() else build_features(split="dev")

    funding_dfs = []
    for s in DEV_ASSETS:
        f_sub = load_asset(s, split="dev")["funding"].copy()
        f_sub["sym"] = s
        funding_dfs.append(f_sub)
    funding_combined = pd.concat(funding_dfs, ignore_index=True)

    knn_caches = {grp: np.load(build_knn_cache_for_group(features_df, group=grp, cache_dir=cache_dir), allow_pickle=True) for grp in GROUPS}

    results_file = out_dir / "dev_results.csv"
    should_run_sim = True
    if current_trials == 161 and not rerun and results_file.exists():
        results_df = pd.read_csv(results_file)
        if len(results_df) == 161:
            results = results_df.to_dict(orient="records")
            total_trials = 161
            should_run_sim = False

    if should_run_sim:
        grid = []
        for grp in ["A", "B", "C", "D"]:
            for k in [16, 32, 64, 128]:
                for w in ["equal", "inverse"]:
                    for th in [0.1, 0.2, 0.3, 0.4]:
                        grid.append({"family": "KNN", "group": grp, "k": k, "weight": w, "theta": th})
        for grp in ["A", "B", "C", "D"]:
            for th in [0.1, 0.2, 0.3, 0.4]:
                grid.append({"family": "LOGIT", "group": grp, "k": "-", "weight": "-", "theta": th})
        for grp in ["A", "B", "C", "D"]:
            for th in [0.1, 0.2, 0.3, 0.4]:
                grid.append({"family": "LGBM", "group": grp, "k": "-", "weight": "-", "theta": th})
        grid.append({"family": "MOM", "group": "-", "k": "-", "weight": "-", "theta": "-"})

        classifier_scores, sim_data_list, all_sr_hats = {}, [], []
        for cfg in grid:
            fam, grp, th = cfg["family"], cfg["group"], cfg["theta"]
            try:
                if fam == "KNN":
                    q_sym, q_time, _, signals = predict_knn_from_cache(knn_caches[grp], k=cfg["k"], weight=cfg["weight"], theta=th)
                    sig_df = pd.DataFrame({"sym": q_sym, "open_time": q_time, "signal": signals})
                elif fam in ("LOGIT", "LGBM"):
                    mf = "logistic" if fam == "LOGIT" else "lgbm"
                    if (mf, grp) not in classifier_scores:
                        base_df = run_monthly_classifier(features_df, group=grp, model_family=mf, theta=0.0)
                        classifier_scores[(mf, grp)] = base_df[["sym", "open_time", "score"]].copy()
                    cached = classifier_scores[(mf, grp)]
                    s_arr = cached["score"].to_numpy(dtype=np.float32)
                    sigs = np.where(s_arr > th, 1.0, np.where(s_arr < -th, -1.0, 0.0))
                    sig_df = pd.DataFrame({"sym": cached["sym"], "open_time": cached["open_time"], "signal": sigs})
                else:
                    sig_df = run_momentum_baseline(features_df)

                eq, rb, tr, per = run_portfolio_backtest(features_df, sig_df, funding_combined, cost_mult=1.0)
                s_r = float(np.std(rb, ddof=1))
                sr_hat = float(np.mean(rb) / s_r) if s_r > 1e-12 else 0.0
                all_sr_hats.append(sr_hat)
                sim_data_list.append((cfg, eq, rb, tr, per, None))
            except Exception as e:
                sim_data_list.append((cfg, None, None, None, None, str(e)))

        v_sr = float(np.var(all_sr_hats, ddof=1)) if len(all_sr_hats) > 1 else 0.0
        total_trials = 161 if current_trials == 161 else current_trials + len(grid)
        results, trial_id = [], 1 if (current_trials == 161 or rerun) else current_trials + 1

        for cfg, eq, rb, tr, per, err in sim_data_list:
            now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
            if err is not None:
                r_fail = dict(cfg, id=trial_id, utc=now_utc, n_trades=0, **{"net_%": 0.0, "sharpe": 0.0, "gross_bps": 0.0, "rt_bps": 0.0, "assets_pos": 0, "status": f"FAIL:{err}"})
                if current_trials != 161 or rerun:
                    append_log_row(log_file, r_fail)
                trial_id += 1
                continue

            res = calculate_metrics(eq, rb, tr, per, v_sr=v_sr, n_trials=total_trials)
            res_rec = dict(cfg, id=trial_id, utc=now_utc, net_return=res["net_return"], **{"net_%": res["net_return"] * 100.0, "sharpe": res["sharpe_ann"], "max_dd": res["max_dd"], "n_trades": res["n_trades"], "gross_bps": res["gross_mean_bps"], "rt_bps": res["rt_mean_bps"], "gross_t": res["gross_t"], "assets_pos": res["assets_pos"], "dsr": res["dsr"], "status": "OK", "per_asset_net": res["per_asset_net"]})
            results.append(res_rec)
            if current_trials != 161 or rerun:
                append_log_row(log_file, res_rec)
            trial_id += 1

        pd.DataFrame(results).drop(columns=["per_asset_net"], errors="ignore").to_csv(results_file, index=False)

    def select_best(fam: str):
        sub = [r for r in results if r["family"] == fam and r["n_trades"] >= MIN_TRADES]
        return max(sub, key=lambda x: x["sharpe"]) if sub else None

    knn_best, logit_best, lgbm_best = select_best("KNN"), select_best("LOGIT"), select_best("LGBM")
    mom_sub = [r for r in results if r["family"] == "MOM"]
    mom_best = mom_sub[0] if mom_sub else None

    thresholds_knn, cost_stress_knn, placebo_result = {}, {}, {}
    if knn_best is not None:
        g1 = bool(knn_best["gross_bps"] > 3.0 * knn_best["rt_bps"])
        l_net, l_sr = (logit_best["net_return"], logit_best["sharpe"]) if logit_best else (-np.inf, -np.inf)
        lg_net, lg_sr = (lgbm_best["net_return"], lgbm_best["sharpe"]) if lgbm_best else (-np.inf, -np.inf)
        m_net, m_sr = (mom_best["net_return"], mom_best["sharpe"]) if mom_best else (-np.inf, -np.inf)

        g2 = bool(knn_best["net_return"] > max(l_net, lg_net, m_net) and knn_best["sharpe"] > max(l_sr, lg_sr, m_sr))
        g3, g4 = bool(knn_best["assets_pos"] >= 5), bool(knn_best["dsr"] >= 0.95)

        k_int, th_flt = int(knn_best["k"]), float(knn_best["theta"])
        q_sym, q_time, _, sigs = predict_knn_from_cache(knn_caches[knn_best["group"]], k=k_int, weight=str(knn_best["weight"]), theta=th_flt)
        s_df = pd.DataFrame({"sym": q_sym, "open_time": q_time, "signal": sigs})
        e15, r15, t15, p15 = run_portfolio_backtest(features_df, s_df, funding_combined, cost_mult=1.5)
        res_15 = calculate_metrics(e15, r15, t15, p15)
        e20, r20, t20, p20 = run_portfolio_backtest(features_df, s_df, funding_combined, cost_mult=2.0)
        res_20 = calculate_metrics(e20, r20, t20, p20)
        g5 = bool(res_15["net_return"] > 0)

        thresholds_knn = {
            "G1": {"passed": g1, "gross_bps": knn_best["gross_bps"], "3x_rt_bps": 3.0 * knn_best["rt_bps"]},
            "G2": {
                "passed": g2,
                "knn_net": knn_best["net_return"],
                "knn_sr": knn_best["sharpe"],
                "logit_net": l_net,
                "logit_sr": l_sr,
                "lgbm_net": lg_net,
                "lgbm_sr": lg_sr,
                "mom_net": m_net,
                "mom_sr": m_sr,
            },
            "G3": {"passed": g3, "assets_pos": knn_best["assets_pos"]},
            "G4": {"passed": g4, "dsr": knn_best["dsr"]},
            "G5": {"passed": g5, "net_return_1.5x": res_15["net_return"]},
        }
        cost_stress_knn = {
            "1.0x": {"net_return": knn_best["net_return"], "sharpe": knn_best["sharpe"], "max_dd": knn_best["max_dd"]},
            "1.5x": {"net_return": res_15["net_return"], "sharpe": res_15["sharpe_ann"], "max_dd": res_15["max_dd"]},
            "2.0x": {"net_return": res_20["net_return"], "sharpe": res_20["sharpe_ann"], "max_dd": res_20["max_dd"]},
        }

        # Placebo test: shuffle y per asset
        shuffled = features_df.copy()
        rng = np.random.default_rng(0)
        for s in DEV_ASSETS:
            idx = shuffled[shuffled["sym"] == s].index
            shuffled.loc[idx, "y"] = rng.permutation(shuffled.loc[idx, "y"].to_numpy())
        p_cache = np.load(build_knn_cache_for_group(shuffled, group=knn_best["group"], cache_dir=cache_dir / "placebo"), allow_pickle=True)
        p_sym, p_time, _, p_sigs = predict_knn_from_cache(p_cache, k=k_int, weight=str(knn_best["weight"]), theta=th_flt)
        pe, pr, pt, pp = run_portfolio_backtest(shuffled, pd.DataFrame({"sym": p_sym, "open_time": p_time, "signal": p_sigs}), funding_combined, cost_mult=1.0)
        p_res = calculate_metrics(pe, pr, pt, pp)
        placebo_result = {"gross_mean_bps": p_res["gross_mean_bps"], "net_return": p_res["net_return"], "sharpe": p_res["sharpe_ann"], "n_trades": p_res["n_trades"]}
        all_passed_knn = bool(g1 and g2 and g3 and g4 and g5)
    else:
        all_passed_knn = False

    thresholds_ml, cost_stress_ml = {}, {}
    ml_candidates = [c for c in [logit_best, lgbm_best] if c is not None]
    if ml_candidates:
        m_star = max(ml_candidates, key=lambda x: x["sharpe"])
        m_g1, m_g3, m_g4 = bool(m_star["gross_bps"] > 3.0 * m_star["rt_bps"]), bool(m_star["assets_pos"] >= 5), bool(m_star["dsr"] >= 0.95)
        m_sig = run_monthly_classifier(features_df, group=m_star["group"], model_family="logistic" if m_star["family"] == "LOGIT" else "lgbm", theta=float(m_star["theta"]))
        me15, mr15, mt15, mp15 = run_portfolio_backtest(features_df, m_sig, funding_combined, cost_mult=1.5)
        res_m15 = calculate_metrics(me15, mr15, mt15, mp15)
        m_g5 = bool(res_m15["net_return"] > 0)
        me20, mr20, mt20, mp20 = run_portfolio_backtest(features_df, m_sig, funding_combined, cost_mult=2.0)
        res_m20 = calculate_metrics(me20, mr20, mt20, mp20)
        m_net = mom_best["net_return"] if mom_best else -np.inf
        m_sr = mom_best["sharpe"] if mom_best else -np.inf
        beats_mom = bool(m_star["net_return"] > m_net and m_star["sharpe"] > m_sr)
        thresholds_ml = {
            "G1": {"passed": m_g1, "gross_bps": m_star["gross_bps"], "3x_rt_bps": 3.0 * m_star["rt_bps"]},
            "G3": {"passed": m_g3, "assets_pos": m_star["assets_pos"]},
            "G4": {"passed": m_g4, "dsr": m_star["dsr"]},
            "G5": {"passed": m_g5, "net_return_1.5x": res_m15["net_return"]},
            "beats_mom": {"passed": beats_mom, "m_net": m_star["net_return"], "m_sr": m_star["sharpe"], "mom_net": m_net, "mom_sr": m_sr},
        }
        cost_stress_ml = {
            "1.0x": {"net_return": m_star["net_return"], "sharpe": m_star["sharpe"], "max_dd": m_star["max_dd"]},
            "1.5x": {"net_return": res_m15["net_return"], "sharpe": res_m15["sharpe_ann"], "max_dd": res_m15["max_dd"]},
            "2.0x": {"net_return": res_m20["net_return"], "sharpe": res_m20["sharpe_ann"], "max_dd": res_m20["max_dd"]},
        }
    else:
        m_star = None

    if all_passed_knn:
        branch, candidate, candidate_family = "a", knn_best, "KNN"
        cost_stress = cost_stress_knn
    elif thresholds_ml and thresholds_ml["G1"]["passed"] and thresholds_ml["G3"]["passed"] and thresholds_ml["G4"]["passed"] and thresholds_ml["G5"]["passed"] and thresholds_ml["beats_mom"]["passed"]:
        branch, candidate, candidate_family = "b", m_star, m_star["family"]
        cost_stress = cost_stress_ml
    else:
        branch, candidate, candidate_family = "c", None, None
        cost_stress = cost_stress_knn

    selected_output = {
        "branch": branch,
        "candidate_family": candidate_family,
        "candidate": candidate,
        "family_bests": {"KNN": knn_best, "LOGIT": logit_best, "LGBM": lgbm_best, "MOM": mom_best},
        "thresholds_knn": thresholds_knn,
        "thresholds_ml": thresholds_ml,
        "cost_stress": cost_stress,
        "cost_stress_knn": cost_stress_knn,
        "cost_stress_ml": cost_stress_ml,
        "placebo": placebo_result,
        "total_trials_N": total_trials,
    }
    with open(out_dir / "dev_selected.json", "w", encoding="utf-8") as f:
        json.dump(selected_output, f, indent=2, default=str)

    with open(log_file, "r", encoding="utf-8") as f:
        content = f.read()
    if "## Diagnostics" in content:
        content = content[: content.index("## Diagnostics")].rstrip() + "\n"
    with open(log_file, "w", encoding="utf-8") as f:
        f.write(content)
        f.write(f"\n## Diagnostics\n- Total dev trials N: {total_trials}\n- Branch decision: ({branch})\n")
        if candidate is not None:
            f.write(f"- Candidate: {candidate['family']} (group {candidate['group']}, k={candidate['k']}, weight={candidate['weight']}, theta={candidate['theta']})\n")
        if cost_stress_knn:
            f.write(f"- Cost stress (KNN): 1.0x={cost_stress_knn['1.0x']['net_return']:.2%}, 1.5x={cost_stress_knn['1.5x']['net_return']:.2%}, 2.0x={cost_stress_knn['2.0x']['net_return']:.2%}\n")
        if cost_stress_ml and m_star:
            f.write(f"- Cost stress (ML {m_star['family']}): 1.0x={cost_stress_ml['1.0x']['net_return']:.2%}, 1.5x={cost_stress_ml['1.5x']['net_return']:.2%}, 2.0x={cost_stress_ml['2.0x']['net_return']:.2%}\n")
        if placebo_result:
            f.write(f"- Placebo: gross_bps={placebo_result['gross_mean_bps']:.2f}, net={placebo_result['net_return']:.2%}\n")

    return selected_output


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Run dev grid evaluations.")
    parser.add_argument("--rerun", action="store_true", help="Reset LOG.md and rerun all 161 trials from scratch")
    args = parser.parse_args()
    run_dev_grid(rerun=args.rerun)


#!/usr/bin/env python3
"""Compare saved Go accounts to matched and global baselines; no evaluation."""
import argparse
from pathlib import Path
import enhanced
import enhanced_space as api
import optimize as legacy
from continuous_enhanced import validate_result

def report(args):
    manifest=enhanced.strict_loads(args.manifest.read_text());loaded={}
    for entry in manifest["plans"]:
        directory=Path(args.results_dir)/entry["name"] if getattr(args,"results_dir",None) else Path(entry["plan"]).parent/(entry["name"]+"-result")
        state=enhanced.strict_loads((directory/"summary.json").read_text())
        contract=enhanced.strict_loads((directory/"run-contract.json").read_text())
        if state.get("status")!="COMPLETE":raise ValueError("continuous account unfinished: "+entry["name"])
        if contract["kind"]!=entry["kind"] or contract["budget"]!=entry["ledger_budget"] or api.canonical(contract["inspect"])!=api.canonical(entry["inspect"]):raise ValueError("account differs from prepared manifest")
        r=state["result"];validate_result(r,entry["ledger_budget"],entry["inspect"],entry["kind"]);loaded[entry["name"]]=r
    reuse=manifest["global_baseline_reuse"]
    global_result=loaded[reuse] if reuse else loaded["global-classic-fixed"]
    global_accounts={s["cost_multiplier"]:s["baseline" if reuse else "fixed"] for s in global_result["continuous_scenarios"]}
    rows=[]
    for entry in manifest["plans"]:
        if entry["name"]=="global-classic-fixed":continue
        result=loaded[entry["name"]];criteria=result["plan"]["selection"]
        cfg=result["plan"]["baseline"] if entry["kind"]=="fixed" else result["plan"]["candidates"][0]["config"]
        matched=None
        if entry["kind"]=="fixed":
            for group in manifest["plans"]:
                if group["kind"]=="group":
                    b=loaded[group["name"]]["plan"]["baseline"]
                    if api.canonical(b["risk"])==api.canonical(cfg["risk"]) and api.canonical(b["exit"])==api.canonical(cfg["exit"]):matched=loaded[group["name"]]
            if matched is None:raise ValueError("fixed candidate has no matched Risk/Exit baseline account")
        comparisons=[];all_reasons=[]
        for s in result["continuous_scenarios"]:
            multiplier=s["cost_multiplier"];account=s["fixed" if entry["kind"]=="fixed" else "selected"]
            matching=s["baseline"] if entry["kind"]=="group" else next(x["baseline"] for x in matched["continuous_scenarios"] if x["cost_multiplier"]==multiplier)
            m=account["metrics"];b=matching["metrics"];glob=global_accounts[multiplier]["metrics"]
            reasons=[]
            if m["trades"]<criteria["min_trades"]:reasons.append("insufficient_trades")
            if sum(w["net_return_pct"]>0 for w in account["windows"])<criteria["min_positive_windows"]:reasons.append("insufficient_positive_windows")
            if m["net_return_pct"]<=criteria["min_net_return_pct"]:reasons.append("net_return_below_requirement")
            if m["win_rate_pct"]<criteria["min_win_rate_pct"]:reasons.append("win_rate_below_requirement")
            if m["max_drawdown_pct"]>criteria["max_drawdown_pct"]:reasons.append("absolute_drawdown_exceeded")
            if m["net_return_pct"]<=b["net_return_pct"]:reasons.append("no_net_improvement_vs_matched")
            if criteria["require_win_rate_improvement"] and m["win_rate_pct"]<=b["win_rate_pct"]:reasons.append("no_win_improvement_vs_matched")
            if m["max_drawdown_pct"]>b["max_drawdown_pct"]:reasons.append("drawdown_worse_than_matched")
            if m["net_return_pct"]<=glob["net_return_pct"]:reasons.append("no_net_improvement_vs_global")
            if m["max_drawdown_pct"]>glob["max_drawdown_pct"]:reasons.append("drawdown_worse_than_global")
            comparisons.append({"cost_multiplier":multiplier,"account_metrics":m,"matched_metrics":b,"global_classic_metrics":glob,
                "global_delta":{k:m[k]-glob[k] for k in ("net_return_pct","win_rate_pct","max_drawdown_pct")},"rejection_reasons":reasons})
            all_reasons+=reasons
        rows.append({"name":entry["name"],"account_kind":"inner-selected continuous" if entry["kind"]=="group" else "fixed configuration continuous",
            "selections":result.get("selections"),"comparisons":comparisons,"status":"keep_disabled" if all_reasons else "retrospective_support_only; final evidence pending"})
    output={"data_use":"retrospective","total_reserved_ledger_evaluations":manifest["total_ledger_budget"],"global_classic_config":manifest["global_baseline_config"],
        "accounts":rows,"adoption":"default unchanged; exposed history cannot establish independent final performance"}
    legacy.atomic_json(args.out,output);return output

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument("--manifest",required=True,type=Path);p.add_argument("--out",required=True,type=Path);p.add_argument("--results-dir",type=Path)
    print(api.canonical(report(p.parse_args())))

if __name__=="__main__":main()

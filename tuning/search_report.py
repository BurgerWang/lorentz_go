#!/usr/bin/env python3
"""Compare saved development candidates to frozen controls, with no backtest."""
import argparse
from pathlib import Path
import enhanced
import enhanced_space as api
from enhanced_protocol import validate_result
from validation_groups import load_candidates
import optimize as legacy

def report(args):
    candidates,common=load_candidates(args.summaries)
    controls=[];budgets=[];timings=[]
    for path in args.summaries:
        s=enhanced.strict_loads(path.read_text());ready=s["contract"]["ready"]
        budgets.append({"study":str(path.parent.resolve()),"budget":s["budget"],"states":s["trial_states"],"halted_protocol":s.get("halted_protocol")})
        timings.append({"study":str(path.parent.resolve()),"timing":s["timing"]})
        for control in s["controls"]:
            validate_result(control["response"],control["response"]["config"],ready)
            controls.append({"study":str(path.parent.resolve()),"name":control["name"],"config":control["response"]["config"],"aggregate":control["response"]["aggregate"]})
    comparisons=[]
    for candidate in candidates:
        a=candidate["response"]["aggregate"]
        comparisons.append({"source":candidate["source"],"config_identity":candidate["config_identity"],"aggregate":a,
            "control_deltas":[{"study":c["study"],"control":c["name"],"delta":{k:a[k]-c["aggregate"][k] for k in ("net_return_pct","win_rate_pct","max_fold_drawdown_pct","trades")}} for c in controls]})
    result={"mode":"saved-development-comparison","development_contract":common,"controls":controls,"candidates":comparisons,"studies":budgets,"timing":timings,
        "status":"development only; fold drawdown is not continuous account drawdown; repeated cache results are not independent evidence"}
    legacy.atomic_json(args.out,result);return result

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument("--summaries",required=True,nargs="+",type=Path);p.add_argument("--out",required=True,type=Path)
    print(api.canonical(report(p.parse_args())))

if __name__=="__main__":main()

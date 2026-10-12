#!/usr/bin/env python3
"""Execute one source-checked group or fixed continuous plan, with exact budget."""
import argparse
import fcntl
from pathlib import Path
import enhanced
import enhanced_space as api
import optimize as legacy
import walkforward
import validation_groups as groups
import candidate_source
import enhanced_protocol
from copy import deepcopy
from enhanced_protocol import utc

def normalized_plan(plan):
    p=deepcopy(plan)
    p["dataset_dir"],p["exposure_file"]=str(Path(p["dataset_dir"]).resolve()),str(Path(p["exposure_file"]).resolve())
    p["history_start"]=utc(p["history_start"])
    for o in p["outer"]:
        o["start"],o["end"]=utc(o["start"]),utc(o["end"])
        for f in o["inner"]:f["start"],f["end"]=utc(f["start"]),utc(f["end"])
    return p

def validate_result(result,budget,ready,kind):
    walkforward.validate_result(result,budget,ready)
    if kind=="fixed":
        for k in ("evaluation_kind","config","config_identity","plan_identity","daily_aggregation"):
            if k not in result or result[k]!=ready[k]:raise legacy.ProtocolError("fixed continuous result identity differs: "+k)
    scenarios=result.get("continuous_scenarios")
    if not isinstance(scenarios,list) or len(scenarios)!=3 or [x.get("cost_multiplier") for x in scenarios]!=[1,1.5,2]:raise legacy.ProtocolError("invalid continuous cost scenarios")
    for scenario in scenarios:
        for key in (["fixed"] if kind=="fixed" else ["selected","baseline"]):
            if not isinstance(scenario.get(key),dict) or not isinstance(scenario[key].get("metrics"),dict) or not isinstance(scenario[key].get("windows"),list):raise legacy.ProtocolError("missing continuous account metrics/windows")

def run(args):
    if not walkforward.positive_int(args.budget) or not walkforward.positive_int(args.timeout):raise ValueError("explicit positive ledger budget/timeout required")
    inputs=(args.plan,args.provenance,args.binary,Path(__file__),Path(groups.__file__),Path(candidate_source.__file__),Path(enhanced_protocol.__file__),Path(api.__file__),Path(walkforward.__file__),Path(enhanced.__file__),Path(legacy.__file__))
    identities={str(p.resolve()):legacy.digest(p) for p in inputs}
    plan=enhanced.strict_loads(args.plan.read_text());prov=enhanced.strict_loads(args.provenance.read_text())
    groups.verify_provenance(plan,prov)
    if any(legacy.digest(Path(p))!=sha for p,sha in identities.items()):raise RuntimeError("validation input changed during source verification")
    snapshots=prov["development_contract"]["data_files"]
    if identities[str(args.binary.resolve())]!=prov["development_contract"]["binary_sha256"]:raise ValueError("continuous strategy binary differs from candidate source")
    if any(legacy.digest(Path(p))!=v for p,v in snapshots.items()):raise ValueError("data differs from candidate source snapshot")
    out=args.out.resolve();out.mkdir(parents=True,exist_ok=True)
    with (out/"run.lock").open("a+") as lock,legacy.controlled_termination() as termination:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        command="fixed-continuous-eval" if args.kind=="fixed" else "walkforward-eval"
        cmd=[str(args.binary.resolve()),command,"-plan",str(args.plan.resolve())]
        ready=walkforward.child_json(cmd+["-inspect"],args.timeout,termination)
        if api.canonical(ready.get("plan"))!=api.canonical(normalized_plan(plan)) or any(legacy.digest(Path(p))!=sha for p,sha in {**identities,**snapshots}.items()):raise RuntimeError("inspect differs from source-checked frozen inputs")
        walkforward.validate_ready(ready,args.budget)
        contract={"mode":"source-checked-continuous-v1","kind":args.kind,"budget":args.budget,"timeout":args.timeout,"inspect":ready,"identities":identities}
        cp,sp=out/"run-contract.json",out/"summary.json"
        fresh=not cp.exists()
        if not fresh:
            legacy.verify_contract(enhanced.strict_loads(cp.read_text()),contract)
            if not sp.exists():raise RuntimeError("contract without state; no automatic evaluation")
            state=enhanced.strict_loads(sp.read_text());walkforward.validate_state(state,args.budget,ready)
            if state["status"]=="COMPLETE":validate_result(state["result"],args.budget,ready,args.kind)
            if state["status"]=="RUNNING":
                state.update(status="FAIL",error="interrupted; entire budget consumed, no replay");legacy.atomic_json(sp,state)
            return state
        if any(p.name!="run.lock" for p in out.iterdir()):raise RuntimeError("nonempty output needs compatible contract")
        with termination.defer():
            legacy.atomic_json(cp,contract);legacy.atomic_json(sp,{"status":"RUNNING","reserved_evaluations":args.budget})
        state=enhanced.strict_loads(sp.read_text())
        try:
            r=walkforward.child_json(cmd,args.timeout,termination)
            validate_result(r,args.budget,ready,args.kind)
            if any(legacy.digest(Path(p))!=sha for p,sha in {**identities,**snapshots}.items()):raise RuntimeError("input changed during continuous evaluation")
            state.update(status="COMPLETE",result=r)
        except BaseException as e:
            state.update(status="FAIL",error=str(e));raise
        finally:
            with termination.defer():legacy.atomic_json(sp,state)
        return state

def main():
    p=argparse.ArgumentParser(description=__doc__)
    for n in ("plan","provenance","binary","out"):p.add_argument("--"+n,required=True,type=Path)
    p.add_argument("--kind",required=True,choices=["group","fixed"])
    p.add_argument("--budget",required=True,type=int);p.add_argument("--timeout",default=600,type=int)
    print(api.canonical(run(p.parse_args())))

if __name__=="__main__":main()

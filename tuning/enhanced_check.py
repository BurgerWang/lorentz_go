#!/usr/bin/env python3
"""Fixed requests and explicit independent replays; zero Optuna trials."""
import argparse
import fcntl
from pathlib import Path
import time
import enhanced
import enhanced_space as api
import enhanced_protocol
from enhanced_protocol import Server,validate_result
import optimize as legacy

def run(args):
    matrix=enhanced.strict_loads(args.matrix.read_text())
    if set(matrix)!={"version","data_use","requests"} or matrix["version"]!="enhanced-b0-v1" or matrix["data_use"] not in {"synthetic","exposed_development"}:raise ValueError("invalid fixed batch contract")
    entries=matrix["requests"]
    if not entries or type(args.max_go_evaluations) is not int or args.max_go_evaluations!=len(entries) or type(args.max_ledger_evaluations) is not int or args.max_ledger_evaluations<0 or args.timeout<=0:raise ValueError("exact fixed batch Go/ledger budgets required")
    names={}
    for e in entries:
        if set(e)!={"name","plan","config","replay_of"} or not isinstance(e["name"],str) or not e["name"] or e["name"] in names:raise ValueError("fixed requests need unique complete records")
        if e["replay_of"] is not None:
            original=names.get(e["replay_of"])
            if not original or api.canonical(original["config"])!=api.canonical(e["config"]) or Path(original["plan"]).resolve()!=Path(e["plan"]).resolve():raise ValueError("independent replay differs from original request")
        names[e["name"]]=e
    api.go_validate(args.binary.resolve(),[e["config"] for e in entries])
    out=args.out.resolve();out.mkdir(parents=True,exist_ok=True)
    servers={}
    with (out/"run.lock").open("a+") as lock,legacy.controlled_termination() as termination:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:
            started=time.monotonic();ledger=0
            for e in entries:
                path=str(Path(e["plan"]).resolve())
                if path not in servers:
                    with termination.defer():servers[path]=Server(args.binary.resolve(),Path(path),args.timeout,termination,await_ready=False)
                    servers[path].read_ready()
                ledger+=len(servers[path].ready["plan"]["folds"])
            if ledger!=args.max_ledger_evaluations:raise ValueError("ledger budget must equal all scoring folds including independent replays")
            deps=[Path(__file__),Path(api.__file__),Path(enhanced.__file__),Path(enhanced_protocol.__file__),Path(legacy.__file__)]
            contract={"mode":"fixed-enhanced-check-v1","matrix":matrix,"matrix_sha256":legacy.digest(args.matrix),"binary_sha256":legacy.digest(args.binary),
                "dependencies":{str(p.resolve()):legacy.digest(p) for p in deps},"plans":{p:s.ready for p,s in servers.items()},
                "timeout":args.timeout,"max_go_evaluations":args.max_go_evaluations,"max_ledger_evaluations":ledger,"max_trials":0}
            if args.inspect:return contract
            cp,sp=out/"run-contract.json",out/"summary.json"
            if cp.exists():
                legacy.verify_contract(enhanced.strict_loads(cp.read_text()),contract)
                if not sp.exists():raise RuntimeError("existing fixed batch contract has no state; preserve and inspect, no automatic replay")
            else:
                if any(p.name!="run.lock" for p in out.iterdir()):raise RuntimeError("nonempty fixed batch output needs compatible contract")
                legacy.atomic_json(cp,contract)
            state=enhanced.strict_loads(sp.read_text()) if sp.exists() else {"mode":"fixed-enhanced-check-v1","records":[],"halted_protocol":None}
            records=state["records"]
            if len(records)>len(entries):raise RuntimeError("invalid fixed batch record count")
            for e,r in zip(entries,records):
                expected_f=len(servers[str(Path(e["plan"]).resolve())].ready["plan"]["folds"])
                if r.get("name")!=e["name"] or r.get("status") not in {"RUNNING","FAIL","COMPLETE"} or r.get("go_reserved")!=1 or r.get("ledger_reserved")!=expected_f:raise RuntimeError("fixed reservation/state differs")
                if r["status"]=="RUNNING":r.update(status="FAIL",error="interrupted; consumed reservation, no replay")
                if r["status"]=="COMPLETE":validate_result(r["result"],e["config"],servers[str(Path(e["plan"]).resolve())].ready)
            def save():
                state["budget"]={"trial_attempts":0,"go_evaluations_reserved":len(records),"ledger_evaluations_reserved":sum(r["ledger_reserved"] for r in records),"cache_hits":0}
                state["load_seconds"]=time.monotonic()-started if not records else state.get("load_seconds",0)
                legacy.atomic_json(sp,state)
            save()
            if state["halted_protocol"]:return state
            for e in entries[len(records):]:
                server=servers[str(Path(e["plan"]).resolve())]
                r={"name":e["name"],"status":"RUNNING","go_reserved":1,"ledger_reserved":len(server.ready["plan"]["folds"]),"independent_replay_of":e["replay_of"]}
                records.append(r);save()
                try:
                    start=time.monotonic();result=server.evaluate(e["config"])
                    r.update(status="COMPLETE",result=result,request_seconds=time.monotonic()-start)
                    if e["replay_of"] is not None:
                        original=next(x for x in records if x["name"]==e["replay_of"])
                        def stable(value):return {k:v for k,v in value.items() if k not in {"id","elapsed_seconds","timing"}}
                        r["replay_equal"]=original["status"]=="COMPLETE" and api.canonical(stable(original["result"]))==api.canonical(stable(result))
                        if not r["replay_equal"]:raise legacy.ProtocolError("independent replay differs")
                except BaseException as exc:
                    r.update(status="FAIL",error=str(exc))
                    if isinstance(exc,legacy.ProtocolError):state["halted_protocol"]=str(exc)
                    with termination.defer():save()
                    raise
                save()
            return state
        finally:
            with termination.defer():
                for s in servers.values():s.close()

def main():
    p=argparse.ArgumentParser(description=__doc__)
    for n in ("matrix","binary","out"):p.add_argument("--"+n,required=True,type=Path)
    p.add_argument("--max-go-evaluations",required=True,type=int);p.add_argument("--max-ledger-evaluations",required=True,type=int)
    p.add_argument("--timeout",type=int,default=600);p.add_argument("--inspect",action="store_true")
    print(api.canonical(run(p.parse_args())))

if __name__=="__main__":main()

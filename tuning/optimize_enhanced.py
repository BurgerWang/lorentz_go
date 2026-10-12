#!/usr/bin/env python3
"""Bounded serial schema5 Optuna5 controller using the real Go protocol2."""
import argparse
from contextlib import nullcontext
import fcntl
import math
from pathlib import Path
import sys
import time
import collections
import enhanced
import enhanced_space as space_api
import feature_groups
from enhanced_protocol import Server, validate_result, number, normalized_plan
import optimize as legacy

STUDY="enhanced-v1"
OBJECTIVES=("net_return_pct","win_rate_pct")

def constraints(result, limits):
    a=result["aggregate"]; net=a["net_return_pct"]
    # A zero return is strictly infeasible without changing the positive threshold.
    margin=limits["min_net_return_pct"]-net
    if margin==0: margin=math.nextafter(0.,1.)
    return {"trades":limits["min_trades"]-a["trades"],"positive_folds":limits["min_positive_folds"]-a["positive_folds"],
        "net_return":margin,"drawdown":a["max_fold_drawdown_pct"]-limits["max_drawdown_pct"]}

def feasible(result,limits): return all(v<=0 for v in constraints(result,limits).values())

def sampler(seed,startup,mode):
    import optuna
    class PerTrial(optuna.samplers.BaseSampler):
        def __init__(self): self.delegates={}
        def delegate(self,t):
            if t.number not in self.delegates:
                n=(seed+t.number)%(2**32)
                self.delegates[t.number]=(optuna.samplers.RandomSampler(seed=n) if mode=="random" else
                    optuna.samplers.TPESampler(seed=n,n_startup_trials=startup,n_ei_candidates=24,
                        multivariate=mode=="grouped",group=mode=="grouped",constant_liar=False))
            return self.delegates[t.number]
        def before_trial(self,s,t): return self.delegate(t).before_trial(s,t)
        def infer_relative_search_space(self,s,t): return self.delegate(t).infer_relative_search_space(s,t)
        def sample_relative(self,s,t,p): return self.delegate(t).sample_relative(s,t,p)
        def sample_independent(self,s,t,n,d): return self.delegate(t).sample_independent(s,t,n,d)
        def after_trial(self,s,t,state,values):
            try: self.delegate(t).after_trial(s,t,state,values)
            finally: self.delegates.pop(t.number,None)
    return PerTrial()

def strict_journal(path):
    if path.exists():
        with path.open("rb") as f:
            for line in f:
                if not line.endswith(b"\n"): break
                enhanced.strict_loads(line)
    legacy.prepare_journal(path)

def budget_counts(trials):
    counts={"trial_attempts":0,"unique_configurations":0,"cache_hits":0,"go_evaluations_reserved":0,
        "ledger_evaluations_reserved":0,"successful_ledger_evaluations":0,"uncertain_ledger_charged":0}
    keys=set()
    for t in trials:
        if t.state.name=="WAITING": continue
        counts["trial_attempts"]+=1
        e=t.user_attrs.get("execution",{})
        if e.get("key"): keys.add(e["key"])
        if e.get("cache_trial") is not None: counts["cache_hits"]+=1
        for source,target in (("go_reserved","go_evaluations_reserved"),("ledger_reserved","ledger_evaluations_reserved")):
            v=e.get(source,0)
            if type(v) is not int or v<0: raise RuntimeError("invalid persistent budget reservation")
            counts[target]+=v
        if e.get("ledger_reserved"):
            counts["successful_ledger_evaluations" if t.state.name=="COMPLETE" else "uncertain_ledger_charged"]+=e["ledger_reserved"]
    counts["unique_configurations"]=len(keys)
    return counts

def export(out,study,contract,ready,space,load_seconds):
    trials=study.trials
    eligible=[t for t in trials if t.state.name=="COMPLETE" and feasible(t.user_attrs["response"],space["constraints"])]
    pareto=[t for t in eligible if not any(all(u.values[i]>=t.values[i] for i in range(2)) and any(u.values[i]>t.values[i] for i in range(2)) for u in eligible)]
    entries=[]
    for t in sorted(eligible,key=lambda t:t.number):
        r=t.user_attrs["response"]
        validate_result(r,r["config"],ready)
        filename=f"candidate-trial-{t.number}.json"
        legacy.atomic_json(out/filename,r["config"])
        source={"study":str(out),"trial":t.number,"stage":space["stage"],"family":space["family"],
            "score_windows":ready["plan"]["folds"],"parents":space_api.inherited_sources(space),"data_use":space["data_use"],
            "objectives":list(OBJECTIVES),"constraints":space["constraints"],"config_identity":space_api.identity(r["config"]),
            "contract_identity":space_api.identity(contract),"cache_trial":t.user_attrs["execution"].get("cache_trial")}
        entries.append({"trial":t.number,"config_file":filename,"config_identity":source["config_identity"],
            "source":source,"response":r,"pareto":t in pareto})
    controls=[{"name":t.user_attrs["seed_name"],"response":t.user_attrs["response"]} for t in trials if t.state.name=="COMPLETE" and t.user_attrs.get("seed_name")]
    for entry in entries:
        a=entry["response"]["aggregate"]
        entry["control_deltas"]=[{"control":control["name"],"delta":{k:a[k]-control["response"]["aggregate"][k] for k in ("net_return_pct","win_rate_pct","max_fold_drawdown_pct","trades")}} for control in controls]
    legacy.atomic_json(out/"summary.json",{"mode":"enhanced-optuna-v1","contract":contract,"plan":ready["plan"],
        "halted_protocol":study.user_attrs.get("halted_protocol"),
        "budget":budget_counts(trials),"trial_states":dict(collections.Counter(t.state.name for t in trials)),
        "eligible_candidates":entries,"controls":controls,"data_use":space["data_use"],
        "timing":{"load_seconds":load_seconds,"sample_seconds":sum(t.user_attrs.get("sample_seconds",0) for t in trials),
                  "go_request_seconds":sum(t.user_attrs.get("go_request_seconds",0) for t in trials),
                  "strategy_seconds":sum(t.user_attrs.get("response",{}).get("timing",{}).get("strategy_seconds",0) for t in trials if t.user_attrs.get("execution",{}).get("go_reserved")),
                  "ledger_seconds":sum(t.user_attrs.get("response",{}).get("timing",{}).get("ledger_seconds",0) for t in trials if t.user_attrs.get("execution",{}).get("go_reserved"))},
        "trial_records":[{"number":t.number,"state":t.state.name,"params":t.params,"values":t.values,"execution":t.user_attrs.get("execution"),"error":t.user_attrs.get("error")} for t in trials],
        "candidate_status":"development candidates; no adoption/final claim" if entries else "no feasible candidate"})

def run(args):
    import optuna,numpy
    if optuna.__version__!="5.0.0" or not hasattr(optuna.trial.Trial,"set_constraint"): raise RuntimeError("Optuna5.0.0 required")
    for n in ("max_trials","max_go_evaluations","max_ledger_evaluations"):
        if type(getattr(args,n)) is not int or getattr(args,n)<0: raise ValueError("nonnegative explicit budgets required")
    if args.timeout<=0 or args.startup<0 or args.seed<0: raise ValueError("invalid timeout/startup/seed")
    space=space_api.load_space(args.space)
    if len(space["seeds"])+(len(space["anchors"]) if space["stage"]=="S2" else 0)>args.max_trials: raise ValueError("seeds exceed trial attempt budget")
    limits=space["constraints"]
    if (any(not number(v) for v in limits.values()) or type(limits["min_trades"]) is not int or type(limits["min_positive_folds"]) is not int
        or limits["min_trades"]<0 or limits["min_positive_folds"]<0 or limits["min_net_return_pct"]<0 or not 0<limits["max_drawdown_pct"]<100): raise ValueError("invalid frozen constraints")
    out=args.out.resolve();out.mkdir(parents=True,exist_ok=True)
    with (out/"run.lock").open("a+") as lock,legacy.controlled_termination() as termination:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        server=None;study=None;contract=None
        try:
            source_plan_sha=legacy.digest(args.plan)
            source_plan=enhanced.strict_loads(args.plan.read_text())
            if legacy.digest(args.plan)!=source_plan_sha:raise RuntimeError("plan changed during source reading")
            dataset=Path(source_plan["dataset_dir"]).resolve()
            data_paths=[dataset/n for n in ("metadata.json",source_plan["interval"]+".csv","funding.json","1d.csv")]
            snapshots={str(p):legacy.digest(p) for p in data_paths}
            started=time.monotonic()
            with termination.defer():server=Server(args.binary.resolve(),args.plan.resolve(),args.timeout,termination,await_ready=False)
            server.read_ready()
            load_seconds=time.monotonic()-started;ready=server.ready
            if legacy.digest(args.plan)!=source_plan_sha or space_api.canonical(ready["plan"])!=space_api.canonical(normalized_plan(source_plan)):
                raise RuntimeError("loaded evaluator plan differs from frozen source plan")
            if any(legacy.digest(Path(p))!=v for p,v in snapshots.items()):raise RuntimeError("dataset changed during protocol loading")
            space_api.go_validate(args.binary.resolve(),[ready["default_config"]])
            files=[Path(__file__),Path(space_api.__file__),Path(feature_groups.__file__),Path(__file__).with_name("candidate_source.py"),Path(enhanced.__file__),Path(legacy.__file__),Path(sys.modules[Server.__module__].__file__)]
            from candidate_source import source_cutoff
            import candidate_source
            for source in space_api.inherited_sources(space):source_cutoff(source)
            seeds=list(space["seeds"])
            if space["stage"]=="S2":
                for i,a in enumerate(space["anchors"]):
                    if not a.get("source"):raise ValueError("S2 anchors require inherited provenance")
                    seeds.append({"name":"anchor-control-"+str(i),"config":a["config"],"source":a["source"]})
            prepared_seeds=[];seed_names=set()
            for seed in seeds:
                if not isinstance(seed.get("name"),str) or not seed["name"] or seed["name"] in seed_names:raise ValueError("seeds need unique names")
                seed_names.add(seed["name"])
                cfg=space_api.normalize_config(seed["config"],ready["default_config"])
                params=space_api.seed_parameters(cfg,ready["default_config"],space)
                prepared_seeds.append((seed["name"],cfg,params))
            if prepared_seeds:space_api.go_validate(args.binary.resolve(),[cfg for _,cfg,_ in prepared_seeds])
            contract={"directory_schema_version":1,"protocol_version":2,"strategy_schema_version":5,"mode":"enhanced-optuna-v1",
                "space":space,"space_sha256":legacy.digest(args.space),"family":space["family"],"stage":space["stage"],"ready":ready,
                "binary_sha256":legacy.digest(args.binary),"plan_sha256":source_plan_sha,"dependencies":{str(p.resolve()):legacy.digest(p) for p in files},
                "python_version":sys.version,"optuna_version":optuna.__version__,"numpy_version":numpy.__version__,
                "sampler":{"mode":args.sampler,"startup":args.startup,"seed":args.seed,"seed_rule":"(seed+trial.number) mod 2**32",
                    "n_ei_candidates":24,"multivariate":args.sampler=="grouped","group":args.sampler=="grouped","constant_liar":False},
                "objectives":list(OBJECTIVES),"constraints":limits,"budget":{"max_trials":args.max_trials,"max_go_evaluations":args.max_go_evaluations,"max_ledger_evaluations":args.max_ledger_evaluations},"timeout":args.timeout}
            contract["data_files"]=snapshots
            if args.inspect:
                print(space_api.canonical(contract));return contract
            cp=out/"run-contract.json"
            if cp.exists(): legacy.verify_contract(enhanced.strict_loads(cp.read_text()),contract)
            else:
                if any(p.name!="run.lock" for p in out.iterdir()): raise RuntimeError("nonempty output has no compatible contract")
                legacy.atomic_json(cp,contract)
            journal=out/"study.journal";strict_journal(journal)
            storage=optuna.storages.JournalStorage(optuna.storages.journal.JournalFileBackend(str(journal)))
            names=optuna.get_all_study_names(storage)
            if names and names!=[STUDY]: raise RuntimeError("unexpected study in bound directory")
            study=optuna.load_study(study_name=STUDY,storage=storage,sampler=sampler(args.seed,args.startup,args.sampler)) if names else optuna.create_study(study_name=STUDY,storage=storage,sampler=sampler(args.seed,args.startup,args.sampler),directions=["maximize","maximize"])
            existing=study.user_attrs.get("run_contract")
            if existing is None:
                if study.trials: raise RuntimeError("study has no contract")
                study.set_user_attr("run_contract",contract)
            else: legacy.verify_contract(existing,contract)
            for t in study.trials:
                if t.state.name=="RUNNING":
                    storage.set_trial_user_attr(t._trial_id,"error","interrupted; reservations consumed, no replay")
                    study.tell(t.number,state=optuna.trial.TrialState.FAIL)
            counts=budget_counts(study.trials)
            if any(counts[k]>contract["budget"][v] for k,v in (("trial_attempts","max_trials"),("go_evaluations_reserved","max_go_evaluations"),("ledger_evaluations_reserved","max_ledger_evaluations"))): raise RuntimeError("persistent budget exceeds contract")
            if not study.user_attrs.get("seed_queue_complete"):
                seen={t.user_attrs.get("seed_name") for t in study.trials}
                for name,cfg,params in prepared_seeds:
                    if name not in seen:study.enqueue_trial(params,user_attrs={"seed_name":name,"seed_config":cfg})
                study.set_user_attr("seed_queue_complete",True)
            cache={}
            for t in study.trials:
                if t.state.name=="COMPLETE":
                    e=t.user_attrs["execution"];r=t.user_attrs["response"]
                    validate_result(r,e["config"],ready)
                    expected_key=space_api.identity({"config":e["config"],"contract":contract})
                    if e["key"]!=expected_key: raise RuntimeError("cache identity differs")
                    cache.setdefault(e["key"],(t.number,r))
            stop=min(args.max_trials,args.until_trials if args.until_trials is not None else args.max_trials)
            while not study.user_attrs.get("halted_protocol") and budget_counts(study.trials)["trial_attempts"]<stop:
                t=study.ask();phase="compile"
                try:
                    start=time.monotonic();cfg=space_api.sample_config(t,ready["default_config"],space)
                    t.set_user_attr("sample_seconds",time.monotonic()-start)
                    if t.user_attrs.get("seed_config") is not None and space_api.canonical(cfg)!=space_api.canonical(t.user_attrs["seed_config"]): raise ValueError("queued seed does not compile identically")
                    space_api.go_validate(args.binary.resolve(),[cfg])
                    key=space_api.identity({"config":cfg,"contract":contract})
                    hit=cache.get(key);counts=budget_counts(study.trials);f=len(ready["plan"]["folds"])
                    if not hit and (counts["go_evaluations_reserved"]>=args.max_go_evaluations or counts["ledger_evaluations_reserved"]+f>args.max_ledger_evaluations):
                        t.set_user_attr("error","evaluation budget exhausted before request");study.tell(t,state=optuna.trial.TrialState.FAIL);break
                    execution={"config":cfg,"key":key,"go_reserved":0 if hit else 1,"ledger_reserved":0 if hit else f,"cache_trial":hit[0] if hit else None}
                    # The journal record is the only budget authority; fsynced before stdin.
                    t.set_user_attr("execution",execution)
                    phase="evaluate";start=time.monotonic()
                    r=hit[1] if hit else server.evaluate(cfg)
                    t.set_user_attr("go_request_seconds",0 if hit else time.monotonic()-start)
                    t.set_user_attr("response",r)
                    cs=constraints(r,limits);t.set_user_attr("constraints",cs)
                    for n,v in cs.items(): t.set_constraint(n,float(v))
                    study.tell(t,[r["aggregate"][k] for k in OBJECTIVES])
                    cache.setdefault(key,(t.number,r))
                except BaseException as exc:
                    with termination.defer():
                        t.set_user_attr("error",phase+": "+str(exc))
                        if study.trials[t.number].state.name=="RUNNING": study.tell(t,state=optuna.trial.TrialState.FAIL)
                        if isinstance(exc,legacy.ProtocolError):study.set_user_attr("halted_protocol",str(exc))
                    if not (phase=="compile" and isinstance(exc,ValueError) or isinstance(exc,legacy.EvaluationError)): raise
                export(out,study,contract,ready,space,load_seconds)
            export(out,study,contract,ready,space,load_seconds)
            return enhanced.strict_loads((out/"summary.json").read_text())
        finally:
            with termination.defer():
                try:
                    if study is not None and contract is not None: export(out,study,contract,server.ready,space,load_seconds)
                finally:
                    if server is not None: server.close()

def parser():
    p=argparse.ArgumentParser(description=__doc__)
    for n in ("plan","space","out","binary"): p.add_argument("--"+n,required=True,type=Path)
    for n in ("max-trials","max-go-evaluations","max-ledger-evaluations"): p.add_argument("--"+n,required=True,type=int)
    p.add_argument("--sampler",choices=["random","independent","grouped"],default="independent")
    p.add_argument("--seed",type=int,default=42);p.add_argument("--startup",type=int,default=32)
    p.add_argument("--timeout",type=int,default=600);p.add_argument("--until-trials",type=int)
    p.add_argument("--inspect",action="store_true")
    return p

if __name__=="__main__":
    try: run(parser().parse_args())
    except legacy.TerminationRequested: raise SystemExit(143)

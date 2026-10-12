#!/usr/bin/env python3
"""Source-aware deterministic candidate grouping; inspection performs no search."""
import argparse
from copy import deepcopy
from pathlib import Path
import enhanced
import enhanced_space as api
from enhanced_protocol import utc, validate_result
import optimize as legacy
from optimize_enhanced import feasible
import walkforward
from candidate_source import source_cutoff, check_cutoff

def development_contract(contract):
    ready=contract["ready"]
    root=Path(ready["plan"]["dataset_dir"]).resolve()
    expected={str(root/n) for n in ("metadata.json",ready["plan"]["interval"]+".csv","funding.json","1d.csv")}
    if set(contract["data_files"])!=expected:raise ValueError("source data snapshots differ from loaded evaluation plan")
    return {"protocol_version":contract["protocol_version"],"schema_version":contract["strategy_schema_version"],
        "plan":ready["plan"],"data_identity":ready["data_identity"],"daily_identity":ready["daily_identity"],
        "daily_aggregation":ready["daily_aggregation"],"binary_sha256":contract["binary_sha256"],
        "data_files":contract["data_files"],
        "objectives":contract["objectives"],"constraints":contract["constraints"],"data_use":contract["space"]["data_use"]}

def stable(c):
    s=c["source"]
    return api.STAGES.index(s["stage"]),api.FAMILIES.index(s["family"]),s["trial"],s["study"]

def load_candidates(paths,deduplicate=True):
    candidates=[];common=None
    for path in paths:
        path=Path(path);summary=enhanced.strict_loads(path.read_text());contract=summary["contract"]
        if summary.get("mode")!="enhanced-optuna-v1": raise ValueError("requires enhanced search summary")
        proposed=development_contract(contract)
        if common is None: common=proposed
        elif api.canonical(common)!=api.canonical(proposed): raise ValueError("candidate development contracts differ")
        for entry in summary["eligible_candidates"]:
            c=deepcopy(entry);r=c["response"];source=c["source"]
            cfg=enhanced.strict_loads((path.parent/c["config_file"]).read_text())
            validate_result(r,cfg,contract["ready"])
            if (api.identity(cfg)!=c["config_identity"] or source["config_identity"]!=c["config_identity"]
                or source.get("contract_identity")!=api.identity(contract) or source["score_windows"]!=contract["ready"]["plan"]["folds"]
                or source["parents"]!=api.inherited_sources(contract["space"]) or source["family"]!=contract["family"] or source["stage"]!=contract["stage"]
                or source.get("constraints")!=contract["constraints"] or source.get("objectives")!=contract["objectives"]
                or source["data_use"]!=contract["space"]["data_use"] or source["study"]!=str(path.parent.resolve())
                or not feasible(r,contract["constraints"])):
                raise ValueError("candidate identity/source/feasibility differs from frozen study")
            source_cutoff(source)
            c["config"]=cfg;c["summary_file"]=str(path.resolve());candidates.append(c)
    if not deduplicate:return candidates,common
    unique={}
    for c in sorted(candidates,key=stable): unique.setdefault(c["config_identity"],c)
    return list(unique.values()),common

def pareto(candidates):
    def values(c): return tuple(c["response"]["aggregate"][k] for k in ("net_return_pct","win_rate_pct"))
    return [c for c in candidates if not any(all(a>=b for a,b in zip(values(u),values(c))) and any(a>b for a,b in zip(values(u),values(c))) for u in candidates)]

def representatives(candidates):
    if not candidates: return []
    def vals(c):
        a=c["response"]["aggregate"];return a["net_return_pct"],a["win_rate_pct"],a["max_fold_drawdown_pct"]
    orders=(lambda c:(-vals(c)[0],-vals(c)[1],vals(c)[2],stable(c)),
            lambda c:(-vals(c)[1],-vals(c)[0],vals(c)[2],stable(c)),
            lambda c:(vals(c)[2],-vals(c)[0],-vals(c)[1],stable(c)))
    chosen=[]
    for key in orders:
        c=min(candidates,key=key)
        if c["config_identity"] not in {x["config_identity"] for x in chosen}:chosen.append(c)
    return chosen

def group_key(config,interval): return api.identity({"interval":interval,"risk":config["risk"],"exit":config["exit"]})

def select_groups(candidates,interval,defaults):
    groups={}
    for c in candidates: groups.setdefault(group_key(c["config"],interval),[]).append(c)
    keys=[]
    for c in representatives(pareto(candidates)):
        k=group_key(c["config"],interval)
        if k not in keys:keys.append(k)
    off=group_key(defaults,interval)
    if off in groups and off not in keys:keys.append(off)
    return [(k,representatives(pareto(groups[k]))) for k in keys]

def inspect(binary,path,kind,timeout,termination):
    cmd="fixed-continuous-eval" if kind=="fixed" else "walkforward-eval"
    ready=walkforward.child_json([str(Path(binary).resolve()),cmd,"-plan",str(path.resolve()),"-inspect"],timeout,termination)
    walkforward.validate_ready(ready,enhanced.strict_loads(path.read_text())["budget"])
    return ready

def verify_provenance(plan,provenance):
    if provenance.get("plan_identity")!=api.identity(plan): raise ValueError("validation plan differs from provenance")
    first=plan["outer"][0]["start"]
    items=provenance.get("candidate_sources",[])
    if len(items)!=len(plan["candidates"]): raise ValueError("candidate provenance count differs")
    for c,s in zip(plan["candidates"],items):
        if c["name"]!=s["name"] or api.identity(c["config"])!=s["source"]["config_identity"]: raise ValueError("candidate source identity mismatch")
        check_cutoff(s["source"],first)
    source=provenance["baseline_source"]
    if api.identity(plan["baseline"])!=source["config_identity"]: raise ValueError("baseline source identity mismatch")
    check_cutoff(source,first)

def prepare(args):
    candidates,common=load_candidates(args.summaries)
    template=enhanced.strict_loads(args.template.read_text());global_config=deepcopy(template["baseline"])
    source=enhanced.strict_loads(args.baseline_source.read_text())
    if api.identity(global_config)!=source["config_identity"]: raise ValueError("frozen global baseline identity mismatch")
    check_cutoff(source,template["outer"][0]["start"])
    if not candidates: return {"status":"no feasible groups; validation stopped","ledger_budget":0}
    if any(legacy.digest(Path(p))!=v for p,v in common["data_files"].items()):raise ValueError("current data differs from searched snapshot")
    if legacy.digest(args.binary)!=common["binary_sha256"]:raise ValueError("validation binary differs from searched strategy")
    if common["plan"]["interval"]!=template["interval"] or common["plan"]["history_start"]!=utc(template["history_start"]) or common["plan"]["dataset_dir"]!=str(Path(template["dataset_dir"]).resolve()) or common["plan"]["fee_bps"]!=template["fee_bps"] or common["plan"]["slippage_bps"]!=template["slippage_bps"]:
        raise ValueError("validation template/history/cost/dataset differs")
    if template["data_use"]!="retrospective": raise ValueError("exposed search can only prepare retrospective validation")
    for c in candidates:check_cutoff(c["source"],template["outer"][0]["start"])
    defaults=api.go_defaults(args.binary)
    selected=select_groups(candidates,template["interval"],defaults)
    out=args.out.resolve();out.mkdir(parents=True,exist_ok=True)
    frozen={"common":common,"template":template,"candidates":candidates,"baseline_source":source,"binary_sha256":legacy.digest(args.binary),"exposure_sha256":legacy.digest(Path(template["exposure_file"])),"fixed_candidates":args.fixed_candidates}
    cp=out/"preparation-contract.json"
    if cp.exists():legacy.verify_contract(enhanced.strict_loads(cp.read_text()),frozen)
    else:
        if any(out.iterdir()):raise RuntimeError("nonempty preparation output needs matching contract")
        legacy.atomic_json(cp,frozen)
    entries=[];global_reuse=None
    with legacy.controlled_termination() as termination:
        def emit(name,plan,kind,baseline_source,candidate_sources):
            path=out/(name+".json");legacy.atomic_json(path,plan)
            provenance={"plan_identity":api.identity(plan),"baseline_source":baseline_source,"candidate_sources":candidate_sources,"development_contract":common}
            verify_provenance(plan,provenance);prov=out/(name+"-source.json");legacy.atomic_json(prov,provenance)
            ready=inspect(args.binary,path,kind,args.timeout,termination)
            if any(legacy.digest(Path(p))!=v for p,v in common["data_files"].items()) or ready["daily_identity"]!=common["daily_identity"] or ready["exposure_identity"]!=frozen["exposure_sha256"]:
                raise ValueError("data/D1/exposure changed during validation inspection")
            # Inspect is authoritative for work count; selection-history plans
            # load a wider identity so dataset snapshot identity is also bound.
            entries.append({"name":name,"kind":kind,"plan":str(path),"provenance":str(prov),"ledger_budget":ready["required_evaluations"],"inspect":ready})
        for i,(key,group) in enumerate(selected):
            plan=deepcopy(template);first=group[0]["config"]
            plan["baseline"]["risk"],plan["baseline"]["exit"]=deepcopy(first["risk"]),deepcopy(first["exit"])
            api.go_validate(args.binary,[plan["baseline"]]+[c["config"] for c in group])
            plan["candidates"]=[{"name":"candidate-"+str(j)+"-"+c["config_identity"][:12],"config":c["config"]} for j,c in enumerate(group)]
            # Supply the expected count to the strict Go plan; inspect verifies it.
            plan["budget"]=2*len(plan["cost_multipliers"])+2*len(plan["outer"])+(len(group)+1)*sum(len(o["inner"]) for o in plan["outer"])
            bs=deepcopy(source);bs["config_identity"]=api.identity(plan["baseline"]);bs["derivation"]="only complete Risk/Exit replaced on frozen global Classic baseline"
            emit("group-"+str(i),plan,"group",bs,[{"name":v["name"],"source":c["source"]} for v,c in zip(plan["candidates"],group)])
            if api.canonical(plan["baseline"])==api.canonical(global_config):global_reuse="group-"+str(i)
        if global_reuse is None:
            p=deepcopy(template);p["candidates"]=[];p["budget"]=3
            emit("global-classic-fixed",p,"fixed",source,[])
        fixed=representatives(pareto(candidates))[:2] if args.fixed_candidates else []
        for i,c in enumerate(fixed):
            p=deepcopy(template);p["baseline"]=c["config"];p["candidates"]=[];p["budget"]=3
            emit("fixed-candidate-"+str(i),p,"fixed",c["source"],[])
    manifest={"status":"prepared; no ledger evaluation performed","plans":entries,"global_baseline_reuse":global_reuse,
        "global_baseline_config":global_config,"total_ledger_budget":sum(e["ledger_budget"] for e in entries),
        "data_use":"retrospective","selection_scope":"whole strategy; group selection account differs from fixed candidate account"}
    legacy.atomic_json(out/"manifest.json",manifest);return manifest

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--summaries",required=True,nargs="+",type=Path)
    for n in ("template","baseline-source","binary","out"):p.add_argument("--"+n,required=True,type=Path)
    p.add_argument("--timeout",type=int,default=600);p.add_argument("--fixed-candidates",action="store_true")
    print(api.canonical(prepare(p.parse_args())))

if __name__=="__main__":main()

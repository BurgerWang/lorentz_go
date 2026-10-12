#!/usr/bin/env python3
"""Freeze S2 anchors or S3 joint space from feasible development results."""
import argparse
from copy import deepcopy
from pathlib import Path
import enhanced
import enhanced_space as api
import optimize as legacy
from validation_groups import load_candidates,pareto,stable
from candidate_source import source_cutoff

def freeze(args):
    candidates,common=load_candidates(args.summaries,deduplicate=False)
    allowed={"S1"} if args.stage=="S2" else {"S1","S2"}
    candidates=[c for c in candidates if c["source"]["family"]==args.family and c["source"]["stage"] in allowed]
    if not candidates:return {"status":"no feasible anchors; stage not started","budget_used":0}
    expected="S1" if args.stage=="S2" else "S2"
    if not any(c["source"]["stage"]==expected for c in candidates):raise ValueError("required parent stage missing")
    stage_source=max((c["source"] for c in candidates if c["source"]["stage"]==expected),key=lambda s:(source_cutoff(s),s["trial"],s["study"]))
    candidates=pareto(candidates)
    def values(c):
        a=c["response"]["aggregate"];return a["net_return_pct"],a["win_rate_pct"],a["max_fold_drawdown_pct"]
    first=min(candidates,key=lambda c:(-values(c)[0],-values(c)[1],values(c)[2],stable(c)))
    anchors=[first];remaining=[c for c in candidates if c["config_identity"]!=first["config_identity"]]
    if remaining:anchors.append(min(remaining,key=lambda c:(-values(c)[1],-values(c)[0],values(c)[2],stable(c))))
    s=api.load_space(args.base_space)
    if s["family"]!=args.family or s["constraints"]!=common["constraints"] or s["data_use"]!=common["data_use"]:raise ValueError("base space differs from parent development contract")
    s["stage"]=args.stage;s["parents"]=[a["source"] for a in anchors]
    if api.canonical(stage_source) not in {api.canonical(x) for x in s["parents"]}:s["parents"].append(stage_source)
    s["anchors"]=[{"config":a["config"],"config_identity":a["config_identity"],"source":a["source"]} for a in anchors] if args.stage=="S2" else []
    s["seeds"]=[] if args.stage=="S2" else [{"name":"joint-seed-"+str(i),"config":a["config"],"source":a["source"]} for i,a in enumerate(anchors)]
    s["domains"]={} if args.stage=="S2" else deepcopy(s["domains"])
    if args.domains:s["domains"]=enhanced.strict_loads(args.domains.read_text());api.validate_domains(s["domains"])
    # No in-place range expansion or overwrite of a previously frozen stage.
    if args.out.exists():legacy.verify_contract(enhanced.strict_loads(args.out.read_text()),s)
    else:args.out.parent.mkdir(parents=True,exist_ok=True);legacy.atomic_json(args.out,s)
    return {"status":"frozen; no evaluation performed","space_file":str(args.out.resolve()),"anchor_count":len(anchors),"parent_contract":common}

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--summaries",required=True,nargs="+",type=Path);p.add_argument("--stage",required=True,choices=["S2","S3"])
    p.add_argument("--family",required=True,choices=api.FAMILIES)
    for n in ("base-space","out"):p.add_argument("--"+n,required=True,type=Path)
    p.add_argument("--domains",type=Path)
    print(api.canonical(freeze(p.parse_args())))

if __name__=="__main__":main()

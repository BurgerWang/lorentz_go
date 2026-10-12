"""Provenance time guards shared by search and continuous validation."""
import enhanced_space as api
from enhanced_protocol import utc

def source_cutoff(source, depth=0):
    if depth>32 or not isinstance(source,dict): raise ValueError("invalid/recursive candidate source")
    required={"study","trial","stage","family","score_windows","parents","data_use","config_identity"}
    if not required<=source.keys() or source["data_use"] not in {"development","retrospective","synthetic"}:
        raise ValueError("complete candidate provenance required")
    if source["stage"] not in api.STAGES or source["family"] not in api.FAMILIES or type(source["trial"]) is not int or source["trial"]<0:
        raise ValueError("invalid candidate source stage/family/trial")
    if not isinstance(source["parents"],list) or not isinstance(source["score_windows"],list): raise ValueError("source windows/parents need lists")
    ends=[]
    for f in source["score_windows"]:
        if set(f)!={"name","start","end"} or utc(f["start"])>=utc(f["end"]): raise ValueError("invalid source scoring window")
        ends.append(utc(f["end"]))
    ends += [source_cutoff(p,depth+1) for p in source["parents"]]
    if not ends: raise ValueError("candidate has no construction/selection scoring history")
    return max(ends)

def check_cutoff(source, outer_start):
    if source_cutoff(source)>utc(outer_start): raise ValueError("candidate or parent used scores after outer start")


"""Schema5 conditional compiler. No indicators, signals or ledger calculations."""
from copy import deepcopy
import hashlib
import json
import math
import subprocess
from pathlib import Path
from enhanced import strict_loads
from feature_groups import EXTRAS, classifier_config, model_config

VERSION = "enhanced-space-v1"
FAMILIES = ("classic-extended", "aligned-extended")
STAGES = ("S1", "S2", "S3")

def canonical(value):
    def typed(v):
        if type(v) is float and math.isfinite(v) and v.is_integer(): return int(v)
        if isinstance(v,dict): return {k:typed(x) for k,x in v.items()}
        if isinstance(v,list): return [typed(x) for x in v]
        return v
    return json.dumps(typed(value), sort_keys=True, separators=(",", ":"), allow_nan=False)

def identity(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()

def go_defaults(binary):
    return strict_loads(subprocess.check_output([str(binary), "config-validate"], timeout=30))

def go_validate(binary, configs):
    result = subprocess.run([str(binary), "config-validate", "-batch"],
        input=canonical(configs).encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    if result.returncode:
        raise ValueError(result.stderr.decode(errors="replace"))
    decoded = strict_loads(result.stdout)
    if decoded != configs:
        raise ValueError("Go configuration roundtrip differs")
    return decoded

def normalize_config(config, defaults):
    """Canonical inactive values; keep active feature order and all dependencies."""
    c, d = deepcopy(config), defaults
    def shape(v,ref):
        if isinstance(ref,dict):
            if not isinstance(v,dict) or set(v)!=set(ref): raise ValueError("configuration has missing/unknown structure fields")
            for k in ref: shape(v[k],ref[k])
        elif isinstance(ref,list):
            if not isinstance(v,list): raise ValueError("configuration list required")
            if ref:
                for x in v: shape(x,ref[0])
        elif type(ref) is bool and type(v) is not bool: raise ValueError("configuration bool required")
        elif type(ref) is str and type(v) is not str: raise ValueError("configuration string required")
        elif type(ref) in (int,float) and (type(v) not in (int,float) or not math.isfinite(v)): raise ValueError("configuration finite number required")
    shape(c,d)
    if c["schema_version"] != 5 or c["classifier"]["family"] not in FAMILIES:
        raise ValueError("search requires schema5 and a supported family")
    b, base = c["classic"], d["classic"]
    original = deepcopy(b)
    group = c["classifier"]["feature_group"]
    name = group["name"]
    if name not in EXTRAS or not 2 <= b["feature_count"] <= 5 or len(b["features"]) != 5:
        raise ValueError("invalid named group/control slots")
    expected = classifier_config(original, "classic")["feature_group"]["features"]
    features = group["features"]
    if len(features) != len(expected)+len(EXTRAS[name]) or features[:len(expected)] != expected:
        raise ValueError("classifier active prefix or feature length mismatch")
    if [f["name"] for f in features[len(expected):]] != list(EXTRAS[name]):
        raise ValueError("classifier extras/order mismatch")
    for f in features[len(expected):]:
        if set(f) != {"name","a","b","normalization","window"} or f["normalization"] != "rolling-z" or type(f["a"]) is not int or type(f["window"]) is not int or f["a"] < 1 or f["window"] < 2 or f["b"] != 1:
            raise ValueError("invalid enhanced feature spec")
        if f["name"] in {"D1_SLOPE","KERNEL_DEVIATION"} and f["a"] != 1: raise ValueError("context feature A must be one")
    family = c["classifier"]["family"]
    if b["algorithm"] != ("original-online" if family == FAMILIES[0] else "aligned-knn"):
        raise ValueError("family/algorithm mismatch")
    if family == FAMILIES[0]:
        if c["model"] != d["model"]:
            raise ValueError("Classic cannot use aligned model")
        b["min_vote_fraction"], b["sample_stride"] = 0, 4
    elif b["include_full_history"]:
        raise ValueError("aligned cannot use full history")
    for i, f in enumerate(b["features"]):
        if i >= b["feature_count"]:
            b["features"][i] = deepcopy(base["features"][i])
        elif f["name"] == "ADX":
            f["b"] = 1
    for enabled, field in (("use_regime_filter", "regime_threshold"), ("use_adx_filter", "adx_threshold"),
                           ("use_ema_filter", "ema_period"), ("use_sma_filter", "sma_period")):
        if not b[enabled]: b[field] = base[field]
    if b["use_dynamic_exits"] and (b["use_ema_filter"] or b["use_sma_filter"] or b["kernel_smoothing"]):
        raise ValueError("dynamic exits incompatible with EMA/SMA/smoothing")
    if not b["use_kernel_filter"] and not b["use_dynamic_exits"]:
        for key in ("kernel_h", "kernel_r", "kernel_x", "kernel_smoothing", "kernel_lag"):
            b[key] = base[key]
    elif not b["kernel_smoothing"]:
        b["kernel_lag"] = base["kernel_lag"]
    group = c["classifier"]["feature_group"]
    extras = {f["name"]: deepcopy(f) for f in group["features"] if f["normalization"] == "rolling-z"}
    rebuilt = classifier_config(b, group["name"], family=family)
    for f in rebuilt["feature_group"]["features"]:
        if f["name"] in extras and f["normalization"] == "rolling-z":
            f.update(extras[f["name"]])
            f["b"] = 1
            if f["name"] in {"KERNEL_DEVIATION", "D1_SLOPE"}: f["a"] = 1
    c["classifier"] = rebuilt
    pull = c["entry_mode"] != "main"
    if c["pullback"]["enabled"] != pull:
        raise ValueError("entry mode and pullback disagree")
    if pull and not c["envelope_enabled"]:
        raise ValueError("pullback requires envelope")
    if not pull: c["pullback"] = deepcopy(d["pullback"])
    c["envelope_enabled"] = pull
    if not pull and "KERNEL_DEVIATION" not in extras:
        c["envelope"] = deepcopy(d["envelope"])
    elif not pull:
        for key in ("near", "far"): c["envelope"][key] = d["envelope"][key]
    if not c["daily"]["enabled"] and "D1_SLOPE" not in extras:
        for key in ("h", "r", "x"): c["daily"][key] = d["daily"][key]
    if not c["risk"]["enabled"]: c["risk"] = deepcopy(d["risk"])
    elif not c["risk"]["trail"]: c["risk"]["trail_multiplier"] = d["risk"]["trail_multiplier"]
    if c["exit"]["policy"] == "signals": c["exit"]["max_hold_bars"] = 4
    return c

def effective_key(config, defaults):
    return identity(normalize_config(config, defaults))

class Compiler:
    def __init__(self, trial, space):
        self.trial, self.domains, self.used = trial, space.get("domains", {}), set()
        validate_domains(self.domains)

    def cat(self, name, values):
        self.used.add(name)
        choices = self.domains.get(name, values)
        if not isinstance(choices, list) or not choices or any(v not in values for v in choices) or len(set(choices)) != len(choices):
            raise ValueError("invalid fixed categorical domain: " + name)
        return self.trial.suggest_categorical(name, choices)

    def number(self, name, low, high, step=1, integer=True):
        self.used.add(name)
        bounds = self.domains.get(name, {"low": low, "high": high, "step": step})
        if (not isinstance(bounds, dict) or set(bounds) != {"low", "high", "step"}
            or bounds["low"] < low or bounds["high"] > high or bounds["low"] > bounds["high"]
            or bounds["step"] < step):
            raise ValueError("invalid fixed numeric domain: " + name)
        fn = self.trial.suggest_int if integer else self.trial.suggest_float
        return fn(name, bounds["low"], bounds["high"], step=bounds["step"])

def sample_config(trial, defaults, space):
    if space.get("version") != VERSION or space.get("family") not in FAMILIES or space.get("stage") not in STAGES:
        raise ValueError("unsupported space version/family/stage")
    c, q = deepcopy(defaults), Compiler(trial, space)
    family, stage = space["family"], space["stage"]
    if stage == "S2":
        anchors = space.get("anchors", [])
        if not anchors or len(anchors) > 2 or len({a["config_identity"] for a in anchors}) != len(anchors):
            raise ValueError("S2 needs one or two distinct frozen feasible anchors")
        name = q.cat("anchor", [a["config_identity"] for a in anchors])
        anchor = next(a for a in anchors if a["config_identity"] == name)
        c = normalize_config(anchor["config"], defaults)
        if identity(c) != name or c["classifier"]["family"] != family:
            raise ValueError("anchor identity/family differs")
        if q.cat("anchor_control", [False, True]):
            c["entry_mode"], c["envelope_enabled"] = "main", False
            c["pullback"], c["risk"], c["exit"] = deepcopy(defaults["pullback"]),deepcopy(defaults["risk"]),deepcopy(defaults["exit"])
            c["daily"]["enabled"] = False
            return normalize_config(c, defaults)
    b = c["classic"]
    b["algorithm"] = "original-online" if family == FAMILIES[0] else "aligned-knn"
    if stage != "S2":
        b["source"] = q.cat("source", ["close", "hlc3", "ohlc4"])
        b["neighbors"] = q.number("neighbors", 1, 100)
        b["max_bars_back"] = q.cat("max_bars_back", [500, 1000, 2000, 4000])
        b["include_full_history"] = q.cat("full_history", [False, True]) if family == FAMILIES[0] else False
        b["feature_count"] = q.number("feature_count", 2, 5)
        for i in range(b["feature_count"]):
            prefix = "f" + str(i)
            name = q.cat(prefix + "_type", ["RSI", "WT", "CCI", "ADX"])
            limits = {"RSI": (5, 40, 1, 8), "WT": (4, 30, 3, 30), "CCI": (8, 50, 1, 8), "ADX": (5, 40, 1, 1)}[name]
            b["features"][i] = {"name": name, "a": q.number(prefix + "_" + name + "_a", *limits[:2]),
                "b": q.number(prefix + "_" + name + "_b", *limits[2:]) if name != "ADX" else 1}
        group = q.cat("feature_group", list(EXTRAS))
        c["classifier"] = classifier_config(b, group, family=family)
        for f in c["classifier"]["feature_group"]["features"]:
            if f["normalization"] == "rolling-z":
                prefix = {"RVOL": "rvol", "ATR_PRICE": "atr_price", "KERNEL_DEVIATION": "kernel_deviation", "D1_SLOPE": "d1_slope"}[f["name"]]
                if f["name"] in {"RVOL", "ATR_PRICE"}: f["a"] = q.cat(prefix + "_period", [7, 14, 28, 56])
                f["window"] = q.cat(prefix + "_window", [10, 20, 40, 80, 160])
        b["use_dynamic_exits"] = q.cat("dynamic_exits", [False, True])
        for short, field in (("volatility", "use_volatility_filter"), ("regime", "use_regime_filter"), ("adx", "use_adx_filter")):
            b[field] = q.cat(short + "_filter", [False, True])
        if b["use_regime_filter"]: b["regime_threshold"] = q.number("regime_threshold", -.5, 1., .1, False)
        if b["use_adx_filter"]: b["adx_threshold"] = q.number("adx_threshold", 10, 40)
        for label in ("ema", "sma"):
            b["use_" + label + "_filter"] = False if b["use_dynamic_exits"] else q.cat(label + "_filter", [False, True])
            if b["use_" + label + "_filter"]: b[label + "_period"] = q.number(label + "_period", 20, 300, 10)
        b["use_kernel_filter"] = q.cat("kernel_filter", [False, True])
        b["kernel_smoothing"] = False if b["use_dynamic_exits"] or not b["use_kernel_filter"] else q.cat("kernel_smoothing", [False, True])
        if b["use_kernel_filter"] or b["use_dynamic_exits"]:
            b["kernel_h"] = q.number("kernel_h", 3, 30)
            b["kernel_r"] = q.number("kernel_r", .25, 16., .25, False)
            b["kernel_x"] = q.number("kernel_x", 2, 40)
            if b["kernel_smoothing"]: b["kernel_lag"] = q.cat("kernel_lag", [1, 2])
        if family == FAMILIES[1]:
            b["sample_stride"] = q.cat("sample_stride", [1, 2, 4, 8])
            b["min_vote_fraction"] = q.number("min_vote_fraction", 0., .75, .05, False)
            age = q.cat("age_mode", ["off", "vote", "rank"])
            c["model"] = model_config(q.cat("distance", ["lorentzian", "euclidean"]), q.cat("vote_weight", ["equal", "inverse"]),
                vote_half_life=q.cat("vote_half_life", [25, 50, 100, 200, 400]) if age == "vote" else 0,
                rank_half_life=q.cat("rank_half_life", [25, 50, 100, 200, 400]) if age == "rank" else 0)
    c["entry_mode"] = "main" if stage == "S1" else q.cat("entry_mode", ["main", "pullback", "both"])
    pull = c["entry_mode"] != "main"
    c["pullback"]["enabled"], c["envelope_enabled"] = pull, pull
    if pull: c["pullback"]["max_wait_bars"] = q.cat("pullback_wait", [4, 8, 12, 24, 48, 96])
    extra_names = EXTRAS[c["classifier"]["feature_group"]["name"]]
    c["daily"]["enabled"] = False if stage == "S1" else q.cat("daily_filter", [False, True])
    c["daily"]["history_start"] = space["daily_history_start"]
    for label, needed in (("daily", c["daily"]["enabled"] or "D1_SLOPE" in extra_names),
                           ("envelope", pull or "KERNEL_DEVIATION" in extra_names)):
        if needed:
            block = c[label]
            block["h"] = q.cat(label + "_h", [4, 8, 12, 16, 24])
            block["r"] = q.number(label + "_r", .25, 16., .25, False)
            block["x"] = q.number(label + "_x", 2, 40)
            if label == "envelope":
                block["atr_length"] = q.cat("envelope_atr_length", [14, 30, 60, 120])
                if pull:
                    block["near"] = q.cat("near", [.5, 1., 1.5, 2., 3.])
                    block["far"] = block["near"] + q.cat("far_gap", [.5, 1., 2., 4., 6.5, 8.])
    c["risk"] = deepcopy(defaults["risk"])
    c["exit"] = deepcopy(defaults["exit"])
    if stage != "S1":
        c["exit"]["policy"] = q.cat("exit_policy", ["signals", "four-bars"])
        if c["exit"]["policy"] == "four-bars": c["exit"]["max_hold_bars"] = q.cat("max_hold_bars", [4, 8, 12, 24, 48, 96])
        risk = q.cat("risk_mode", ["off", "atr", "trail", "breakeven", "trail-breakeven"])
        if risk != "off":
            c["risk"].update(enabled=True, atr_period=q.cat("risk_atr_period", [7, 14, 21, 28]), atr_multiplier=q.number("risk_atr_multiplier", .5, 5., .25, False))
            c["risk"]["trail"] = risk in {"trail", "trail-breakeven"}
            if c["risk"]["trail"]: c["risk"]["trail_multiplier"] = q.number("trail_multiplier", .5, 5., .25, False)
            if risk in {"breakeven", "trail-breakeven"}: c["risk"]["breakeven_r"] = q.cat("breakeven_r", [.5, 1., 1.5, 2., 3.])
    return normalize_config(c, defaults)

def load_space(path):
    space = strict_loads(Path(path).read_text())
    required = {"version", "stage", "family", "daily_history_start", "domains", "constraints", "data_use", "seeds", "parents", "anchors"}
    if not isinstance(space, dict) or set(space) != required:
        raise ValueError("space requires exact complete schema")
    if space["version"] != VERSION or space["stage"] not in STAGES or space["family"] not in FAMILIES or space["data_use"] not in {"development", "retrospective", "synthetic"}:
        raise ValueError("invalid frozen search space")
    if set(space["constraints"]) != {"min_trades", "min_positive_folds", "min_net_return_pct", "max_drawdown_pct"}:
        raise ValueError("explicit development constraints required")
    validate_domains(space["domains"])
    return space

def domain_specs():
    cats = {"source":["close","hlc3","ohlc4"],"max_bars_back":[500,1000,2000,4000],"feature_group":list(EXTRAS),
        "sample_stride":[1,2,4,8],"age_mode":["off","vote","rank"],"distance":["lorentzian","euclidean"],
        "vote_weight":["equal","inverse"],"entry_mode":["main","pullback","both"],"exit_policy":["signals","four-bars"],
        "risk_mode":["off","atr","trail","breakeven","trail-breakeven"],"risk_atr_period":[7,14,21,28],
        "breakeven_r":[.5,1.,1.5,2.,3.],"near":[.5,1.,1.5,2.,3.],"far_gap":[.5,1.,2.,4.,6.5,8.],
        "envelope_atr_length":[14,30,60,120],"kernel_lag":[1,2]}
    for n in ("full_history","dynamic_exits","volatility_filter","regime_filter","adx_filter","ema_filter","sma_filter","kernel_filter","kernel_smoothing","daily_filter","anchor_control"): cats[n]=[False,True]
    for n in ("pullback_wait","max_hold_bars"): cats[n]=[4,8,12,24,48,96]
    for n in ("vote_half_life","rank_half_life"): cats[n]=[25,50,100,200,400]
    for n in ("rvol","atr_price"):
        cats[n+"_period"]=[7,14,28,56]
    for n in ("rvol","atr_price","kernel_deviation","d1_slope"): cats[n+"_window"]=[10,20,40,80,160]
    nums={"neighbors":(1,100,1,True),"feature_count":(2,5,1,True),"regime_threshold":(-.5,1.,.1,False),
        "adx_threshold":(10,40,1,True),"ema_period":(20,300,10,True),"sma_period":(20,300,10,True),
        "kernel_h":(3,30,1,True),"kernel_r":(.25,16.,.25,False),"kernel_x":(2,40,1,True),"min_vote_fraction":(0.,.75,.05,False),
        "risk_atr_multiplier":(.5,5.,.25,False),"trail_multiplier":(.5,5.,.25,False)}
    for n in ("daily","envelope"):
        cats[n+"_h"]=[4,8,12,16,24];nums[n+"_r"]=(.25,16.,.25,False);nums[n+"_x"]=(2,40,1,True)
    for i in range(5):
        cats[f"f{i}_type"]=["RSI","WT","CCI","ADX"]
        for name,limits in {"RSI":(5,40,1,8),"WT":(4,30,3,30),"CCI":(8,50,1,8),"ADX":(5,40,1,1)}.items():
            nums[f"f{i}_{name}_a"]=(*limits[:2],1,True)
            if name!="ADX": nums[f"f{i}_{name}_b"]=(*limits[2:],1,True)
    return cats,nums

def validate_domains(domains):
    cats,nums=domain_specs()
    if not isinstance(domains,dict): raise ValueError("domains must be an object")
    for name,v in domains.items():
        if name in cats:
            choices=cats[name]
            def same(a,b): return (type(a) is type(b) or type(a) in (int,float) and type(b) in (int,float)) and a==b
            if not isinstance(v,list) or not v or len({canonical(x) for x in v})!=len(v) or any(not any(same(x,y) for y in choices) for x in v):
                raise ValueError("invalid categorical domain: "+name)
        elif name in nums:
            lo,hi,step,integer=nums[name]
            if not isinstance(v,dict) or set(v)!={"low","high","step"}: raise ValueError("invalid numeric domain: "+name)
            if any(type(x) not in (int,float) or not math.isfinite(x) or integer and type(x) is not int for x in v.values()): raise ValueError("numeric domain type: "+name)
            if not lo<=v["low"]<=v["high"]<=hi or v["step"]<step: raise ValueError("numeric domain bounds: "+name)
            for x in ((v["low"]-lo)/step, v["step"]/step, (v["high"]-v["low"])/v["step"]):
                if not math.isclose(x,round(x),abs_tol=1e-9): raise ValueError("numeric domain grid: "+name)
        else: raise ValueError("unknown frozen domain: "+name)

def seed_parameters(config, defaults, space):
    """Invert the conditional compiler, then prove the evaluated config matches."""
    c=normalize_config(config,defaults);b=c["classic"];m=c["model"]
    if c["classifier"]["family"] != space["family"]: raise ValueError("seed family differs")
    p={"source":b["source"],"neighbors":b["neighbors"],"max_bars_back":b["max_bars_back"],"full_history":b["include_full_history"],
        "feature_count":b["feature_count"],"feature_group":c["classifier"]["feature_group"]["name"],
        "dynamic_exits":b["use_dynamic_exits"],"kernel_filter":b["use_kernel_filter"],"kernel_smoothing":b["kernel_smoothing"],
        "kernel_h":b["kernel_h"],"kernel_r":b["kernel_r"],"kernel_x":b["kernel_x"],"kernel_lag":b["kernel_lag"],
        "sample_stride":b["sample_stride"],"min_vote_fraction":b["min_vote_fraction"],"distance":m["distance"],"vote_weight":m["vote_weight"],
        "age_mode":"vote" if m["vote_half_life"] else "rank" if m["rank_half_life"] else "off",
        "vote_half_life":m["vote_half_life"],"rank_half_life":m["rank_half_life"],
        "entry_mode":c["entry_mode"],"daily_filter":c["daily"]["enabled"],"pullback_wait":c["pullback"]["max_wait_bars"],
        "exit_policy":c["exit"]["policy"],"max_hold_bars":c["exit"]["max_hold_bars"],
        "risk_atr_period":c["risk"]["atr_period"],"risk_atr_multiplier":c["risk"]["atr_multiplier"],
        "trail_multiplier":c["risk"]["trail_multiplier"],"breakeven_r":c["risk"]["breakeven_r"]}
    risk=c["risk"]
    p["risk_mode"]="off" if not risk["enabled"] else "trail-breakeven" if risk["trail"] and risk["breakeven_r"] else "trail" if risk["trail"] else "breakeven" if risk["breakeven_r"] else "atr"
    for label in ("volatility","regime","adx","ema","sma"):
        p[label+"_filter"]=b["use_"+label+"_filter"]
    for key in ("regime_threshold","adx_threshold","ema_period","sma_period"): p[key]=b[key]
    for i,f in enumerate(b["features"][:b["feature_count"]]):
        p[f"f{i}_type"]=f["name"]
        p[f"f{i}_{f['name']}_a"]=f["a"];p[f"f{i}_{f['name']}_b"]=f["b"]
    for f in c["classifier"]["feature_group"]["features"]:
        if f["normalization"]=="rolling-z":
            label={"RVOL":"rvol","ATR_PRICE":"atr_price","D1_SLOPE":"d1_slope","KERNEL_DEVIATION":"kernel_deviation"}[f["name"]]
            p[label+"_period"],p[label+"_window"]=f["a"],f["window"]
    for label in ("daily","envelope"):
        for key in ("h","r","x"): p[label+"_"+key]=c[label][key]
    p["envelope_atr_length"]=c["envelope"]["atr_length"]
    p["near"]=c["envelope"]["near"];p["far_gap"]=c["envelope"]["far"]-c["envelope"]["near"]
    if space["stage"]=="S2":
        matches=[a["config_identity"] for a in space["anchors"] if canonical(normalize_config(a["config"],defaults))==canonical(c)]
        if not matches: raise ValueError("S2 seed has no frozen anchor")
        p["anchor"],p["anchor_control"]=matches[0],True
    class SeedTrial:
        def __init__(self):self.params={}
        def suggest_categorical(self,n,choices):
            v=p[n]
            if not any(type(v) is type(x) and v==x or type(v) in (int,float) and type(x) in (int,float) and v==x for x in choices): raise ValueError("seed outside categorical domain: "+n)
            self.params[n]=v;return v
        def suggest_int(self,n,lo,hi,step=1):
            v=p[n]
            if type(v) is not int or not lo<=v<=hi or (v-lo)%step: raise ValueError("seed outside integer domain: "+n)
            self.params[n]=v;return v
        def suggest_float(self,n,lo,hi,step):
            v=p[n]
            if not lo<=v<=hi or not math.isclose((v-lo)/step,round((v-lo)/step),abs_tol=1e-9): raise ValueError("seed outside float domain: "+n)
            self.params[n]=v;return v
    t=SeedTrial(); compiled=sample_config(t,defaults,space)
    if canonical(compiled)!=canonical(c): raise ValueError("seed differs from compiled configuration")
    return t.params

def inherited_sources(space):
    result=[]
    for s in list(space["parents"])+[a.get("source") for a in space["anchors"]]+[s.get("source") for s in space["seeds"]]:
        if s is not None and canonical(s) not in {canonical(x) for x in result}:result.append(s)
    return result

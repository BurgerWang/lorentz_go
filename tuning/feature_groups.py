"""Named small groups for strategy schema3+. No arbitrary permutation sampler."""
from copy import deepcopy

EXTRAS = {
    "classic": (), "classic-rvol": ("RVOL",), "classic-atr-price": ("ATR_PRICE",),
    "classic-kernel-deviation": ("KERNEL_DEVIATION",), "classic-d1-slope": ("D1_SLOPE",),
    "classic-rvol-atr-price": ("RVOL", "ATR_PRICE"),
}
def classifier_config(classic, name="classic", period=14, window=20, family="classic-extended"):
    if name not in EXTRAS or not 1 <= period <= 10000 or not 2 <= window <= 10000:
        raise ValueError("unsupported group or periods")
    if family not in {"classic-extended", "aligned-extended"}:
        raise ValueError("unsupported classifier family")
    features = [{**deepcopy(f), "normalization": "legacy", "window": 0}
                for f in classic["features"][:classic["feature_count"]]]
    features.extend({"name": n, "a": 1 if n in {"KERNEL_DEVIATION", "D1_SLOPE"} else period,
                     "b": 1, "normalization": "rolling-z", "window": window} for n in EXTRAS[name])
    if len({tuple(sorted(f.items())) for f in features}) != len(features):
        raise ValueError("duplicate feature specs")
    return {"family": family, "feature_group": {"name": name, "features": features}}

def model_config(distance="lorentzian", vote_weight="equal", epsilon=1e-9,
                 vote_half_life=0, rank_half_life=0):
    """Complete Go schema5 model block; this helper performs no model scoring."""
    import math
    values=(epsilon,vote_half_life,rank_half_life)
    if (distance not in {"lorentzian","euclidean"} or vote_weight not in {"equal","inverse"}
        or any(isinstance(v,bool) or not isinstance(v,(float,int)) or not math.isfinite(v) for v in values)
        or epsilon<=0 or any(v!=0 and v<1 for v in values[1:])
        or vote_half_life>0 and rank_half_life>0
        or vote_weight=="equal" and epsilon!=1e-9):
        raise ValueError("unsupported or inactive model parameters")
    return {"distance":distance,"vote_weight":vote_weight,"distance_epsilon":epsilon,
            "vote_half_life":vote_half_life,"rank_half_life":rank_half_life}

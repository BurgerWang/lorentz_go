import os
import random
import unittest
from copy import deepcopy
from pathlib import Path
from enhanced_space import *

class ParameterTrial:
    def __init__(self, seed=0, forced=None):
        self.rng, self.params, self.domains = random.Random(seed), {}, {}
        self.forced = forced or {}
    def suggest_categorical(self, name, choices):
        self.domains[name] = tuple(choices)
        v = self.forced.get(name, self.rng.choice(choices))
        if v not in choices: raise ValueError(name)
        self.params[name] = v
        return v
    def suggest_int(self, name, low, high, step=1):
        v = self.forced.get(name, self.rng.randrange(low, high+1, step))
        self.params[name] = v
        return v
    def suggest_float(self, name, low, high, step):
        v = self.forced.get(name, low + step*self.rng.randrange(round((high-low)/step)+1))
        self.params[name] = v
        return v

def space(family="classic-extended", stage="S3"):
    return {"version":VERSION,"family":family,"stage":stage,"daily_history_start":"2020-01-01",
        "domains":{},"constraints":{"min_trades":30,"min_positive_folds":2,"min_net_return_pct":0,"max_drawdown_pct":30},
        "data_use":"synthetic","seeds":[],"parents":[],"anchors":[]}

class SpaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.binary = Path(os.environ.get("LORENTZ_TEST_BINARY", "/tmp/lorentz-enhanced-t1"))
        cls.defaults = go_defaults(cls.binary)

    def test_all_branches_go_validation_and_fixed_domains(self):
        configs, domains = [], {}
        for family in FAMILIES:
            for group in EXTRAS:
                for risk in ("off", "atr", "trail", "breakeven", "trail-breakeven"):
                    for age in ("off", "vote", "rank"):
                        t = ParameterTrial(len(configs), {"feature_group":group,"risk_mode":risk,"age_mode":age,"f0_type":"RSI","f1_type":"WT","f2_type":"CCI","f3_type":"ADX","f4_type":"RSI","f0_RSI_a":14,"f4_RSI_a":9})
                        c = sample_config(t,self.defaults,space(family))
                        configs.append(c)
                        for n,d in t.domains.items():
                            self.assertEqual(domains.setdefault(n,d),d)
                        if family == FAMILIES[0]: self.assertEqual(c["model"],self.defaults["model"])
                        else: self.assertFalse(c["classic"]["include_full_history"])
        go_validate(self.binary,configs)
        self.assertEqual(len(configs),180)

    def test_independent_extra_windows_and_dependencies(self):
        t=ParameterTrial(forced={"feature_group":"classic-rvol-atr-price","rvol_period":7,"atr_price_period":56,"rvol_window":10,"atr_price_window":160})
        c=sample_config(t,self.defaults,space())
        a,b=c["classifier"]["feature_group"]["features"][-2:]
        self.assertEqual((a["a"],b["a"],a["window"],b["window"]),(7,56,10,160))
        for group in ("classic-d1-slope","classic-kernel-deviation"):
            t=ParameterTrial(forced={"feature_group":group})
            c=sample_config(t,self.defaults,space(stage="S1"))
            self.assertFalse(c["daily"]["enabled"])
            self.assertFalse(c["envelope_enabled"])
            self.assertIn("daily_h" if "d1" in group else "envelope_h",t.params)
            self.assertNotIn("near",t.params)
            go_validate(self.binary,[c])

    def test_inactive_keys_and_dynamic_generation(self):
        d=normalize_config(self.defaults,self.defaults)
        other=deepcopy(d)
        other["risk"]["atr_multiplier"]=4
        other["classic"]["adx_threshold"]=39
        other["envelope"]["h"]=24
        other["daily"]["h"]=24
        other["classic"]["features"][3]["b"]=9
        other["classifier"]["feature_group"]["features"][3]["b"]=9
        self.assertEqual(effective_key(d,self.defaults),effective_key(other,self.defaults))
        t=ParameterTrial(forced={"dynamic_exits":True})
        c=sample_config(t,self.defaults,space())
        self.assertFalse(c["classic"]["use_ema_filter"])
        self.assertNotIn("ema_filter",t.params)
        self.assertNotIn("kernel_smoothing",t.params)
        c["classic"]["use_sma_filter"]=True
        with self.assertRaises(ValueError): normalize_config(c,self.defaults)

    def test_strict_structure_domains_and_numeric_keys(self):
        d=normalize_config(self.defaults,self.defaults)
        e=deepcopy(d);e["classic"]["kernel_r"]=float(d["classic"]["kernel_r"])
        self.assertEqual(effective_key(d,self.defaults),effective_key(e,self.defaults))
        for kind in ("prefix","missing","extra"):
            c=sample_config(ParameterTrial(forced={"feature_group":"classic-rvol"}),self.defaults,space())
            fs=c["classifier"]["feature_group"]["features"]
            if kind=="prefix": fs[0]["a"]+=1
            elif kind=="missing": fs.pop()
            else: fs.append(deepcopy(fs[-1]))
            with self.assertRaises(ValueError): normalize_config(c,self.defaults)
        for domains in ({"neigbhors":{"low":1,"high":1,"step":1}}, {"neighbors":{"low":1.9,"high":2.9,"step":1}},
                        {"full_history":[0,1]}, {"kernel_r":{"low":.25,"high":float("inf"),"step":.25}},
                        {"ema_period":{"low":21,"high":291,"step":10}}):
            s=space();s["domains"]=domains
            with self.assertRaises(ValueError): sample_config(ParameterTrial(),self.defaults,s)

    def test_stages_anchors_and_invalid_configs(self):
        c=sample_config(ParameterTrial(),self.defaults,space(stage="S1"))
        s=space(stage="S2");s["anchors"]=[{"config":c,"config_identity":identity(c)}]
        t=ParameterTrial(); b=sample_config(t,self.defaults,s)
        self.assertEqual(b["classifier"],c["classifier"])
        self.assertNotIn("neighbors",t.params)
        go_validate(self.binary,[b])
        c["classic"]["algorithm"]="aligned-knn"
        with self.assertRaises(ValueError): normalize_config(c,self.defaults)
        with self.assertRaises(ValueError): go_validate(self.binary,[c])

    def test_s2_context_distinct_seed_identity(self):
        a=sample_config(ParameterTrial(forced={"feature_group":"classic-d1-slope"}),self.defaults,space(stage="S1"))
        b=deepcopy(a);b["daily"]["h"]=8 if a["daily"]["h"]!=8 else 4
        s=space(stage="S2");s["anchors"]=[{"config":c,"config_identity":identity(c)} for c in (a,b)]
        go_validate(self.binary,[a,b])
        self.assertEqual(seed_parameters(a,self.defaults,s)["anchor"],identity(a))
        self.assertEqual(seed_parameters(b,self.defaults,s)["anchor"],identity(b))

if __name__ == "__main__": unittest.main()

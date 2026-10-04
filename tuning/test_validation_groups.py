import argparse
from copy import deepcopy
from datetime import datetime,timezone,timedelta
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
import continuous_enhanced
import freeze_stage
import validation_report
import search_report
import validation_groups as g
import optimize_enhanced as controller
from enhanced_space import *
from synthetic_fixture import make_fixture
from test_enhanced_space import space,ParameterTrial

def source(config,trial=0):
    return {"study":"synthetic-authored","trial":trial,"stage":"S1","family":config["classifier"]["family"],
        "score_windows":[{"name":"dev","start":"2024-03-01","end":"2024-05-30"}],"parents":[],"data_use":"synthetic","config_identity":identity(config)}

class GroupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.binary=Path(os.environ.get("LORENTZ_TEST_BINARY","/tmp/lorentz-enhanced-t2"))
        cls.root=Path(tempfile.mkdtemp(prefix="lorentz-t3-synthetic-"))
        cls.plan=make_fixture(cls.root/"data",days=180,score_folds=((60,90),(90,120),(120,150)))
        cls.defaults=go_defaults(cls.binary)
        cls.space=space(stage="S1");cls.space["daily_history_start"]="2024-01-01"
        cls.space["domains"]={"neighbors":{"low":1,"high":8,"step":1},"max_bars_back":[500]}
        configs=[sample_config(ParameterTrial(i),cls.defaults,cls.space) for i in (2,3)]
        cls.space["seeds"]=[{"name":"fixture-seed-"+str(i),"config":c} for i,c in enumerate(configs)]
        cls.sp=cls.root/"space.json";cls.sp.write_text(json.dumps(cls.space))
        a=argparse.Namespace(plan=cls.plan,space=cls.sp,binary=cls.binary,out=cls.root/"study",max_trials=2,max_go_evaluations=2,max_ledger_evaluations=6,timeout=30,startup=1,seed=42,sampler="independent",until_trials=None,inspect=False)
        cls.summary=controller.run(a)
        if not cls.summary["eligible_candidates"]:raise AssertionError("synthetic fixture needs real feasible Go candidates under the production development thresholds")
        start=datetime(2024,1,1,tzinfo=timezone.utc)
        def day(n):return (start+timedelta(days=n)).isoformat().replace("+00:00","Z")
        baseline=normalize_config(cls.defaults,cls.defaults);baseline["daily"]["history_start"]="2024-01-01"
        cls.template={"schema_version":1,"dataset_dir":str((cls.root/"data").resolve()),"interval":"1h","history_start":day(1),"fee_bps":5,"slippage_bps":2,"baseline":baseline,"candidates":[],
            "outer":[{"name":str(i),"start":day(a),"end":day(b),"inner":[{"name":"a","start":day(60),"end":day(105)},{"name":"b","start":day(105),"end":day(150)}]} for i,(a,b) in enumerate(((150,160),(160,170)))],
            "selection":{"min_trades":30,"min_positive_windows":1,"min_net_return_pct":0,"min_win_rate_pct":40,"max_drawdown_pct":30,"require_net_improvement":True,"require_win_rate_improvement":False,"max_drawdown_increase_pct":0},
            "cost_multipliers":[1,1.5,2],"budget":18,"data_use":"retrospective","exposure_file":str(cls.root/"data"/"exposure.json")}
        cls.tp=cls.root/"template.json";cls.tp.write_text(json.dumps(cls.template))
        cls.bs=cls.root/"baseline-source.json";cls.bs.write_text(json.dumps(source(baseline)))
    @classmethod
    def tearDownClass(cls):shutil.rmtree(cls.root)
    def args(self):
        return argparse.Namespace(summaries=[self.root/"study"/"summary.json"],template=self.tp,baseline_source=self.bs,binary=self.binary,
            out=Path(tempfile.mkdtemp(dir=self.root))/"groups",timeout=30,fixed_candidates=True)
    def test_true_go_group_fixed_accounts_and_budget_inspection(self):
        manifest=g.prepare(self.args());self.assertLessEqual(len([x for x in manifest["plans"] if x["kind"]=="group"]),4)
        self.assertEqual(manifest["global_baseline_reuse"],"group-0")
        for entry in manifest["plans"]:
            a=argparse.Namespace(plan=Path(entry["plan"]),provenance=Path(entry["provenance"]),binary=self.binary,
                out=Path(entry["plan"]).parent/(entry["name"]+"-result"),budget=entry["ledger_budget"],timeout=30,kind=entry["kind"])
            state=continuous_enhanced.run(a)
            self.assertEqual(state["status"],"COMPLETE")
            self.assertEqual(state["result"]["evaluations_used"],entry["inspect"]["required_evaluations"])
            self.assertEqual(continuous_enhanced.run(a),state)
            self.assertEqual(len(state["result"]["continuous_scenarios"]),3)
            if entry["kind"]=="group":self.assertEqual(len(state["result"]["selections"]),2)
        directory=Path(manifest["plans"][0]["plan"]).parent
        report=validation_report.report(argparse.Namespace(manifest=directory/"manifest.json",out=directory/"report.json"))
        self.assertEqual(report["total_reserved_ledger_evaluations"],manifest["total_ledger_budget"])
        self.assertTrue(report["accounts"])
        dev=search_report.report(argparse.Namespace(summaries=[self.root/"study"/"summary.json"],out=directory/"search-report.json"))
        self.assertEqual(len(dev["controls"]),2)
        self.assertTrue(all(len(c["control_deltas"])==2 for c in dev["candidates"]))
    def test_parent_future_rejected(self):
        s=source(self.template["baseline"])
        g.check_cutoff(s,self.template["outer"][0]["start"])
        s["parents"]=[deepcopy(s)];s["parents"][0]["score_windows"][0]["end"]="2025-01-01"
        with self.assertRaises(ValueError):g.check_cutoff(s,self.template["outer"][0]["start"])
        a=self.args();s=json.loads(self.bs.read_text());s["parents"]=[source(self.template["baseline"])];s["parents"][0]["score_windows"][0]["end"]="2025-01-01"
        p=self.root/"future-source.json";p.write_text(json.dumps(s));a.baseline_source=p
        with self.assertRaises(ValueError):g.prepare(a)
    def test_exact_risk_groups_and_deterministic_selection(self):
        cs,_=g.load_candidates([self.root/"study"/"summary.json"])
        c=deepcopy(cs[0]);c["config"]["risk"].update(enabled=True,atr_multiplier=2.0000000000000004)
        d=deepcopy(c);d["config"]["risk"]["atr_multiplier"]=2
        self.assertNotEqual(g.group_key(c["config"],"1h"),g.group_key(d["config"],"1h"))
        self.assertEqual(g.select_groups(cs,"1h",self.defaults),g.select_groups(list(reversed(cs)),"1h",self.defaults))
        plan=deepcopy(self.template);plan["candidates"]=[{"name":"wrong-risk","config":c["config"]}]
        plan["budget"]=18;p=self.root/"mixed-risk.json";p.write_text(json.dumps(plan))
        with controller.legacy.controlled_termination() as term:
            with self.assertRaises(controller.legacy.ProtocolError):g.inspect(self.binary,p,"group",30,term)
    def test_s2_freeze_anchors_with_sources_and_controls(self):
        path=self.root/"s2.json"
        a=argparse.Namespace(summaries=[self.root/"study"/"summary.json"],family="classic-extended",stage="S2",base_space=self.sp,out=path,domains=None)
        result=freeze_stage.freeze(a);self.assertGreaterEqual(result["anchor_count"],1)
        s=load_space(path);self.assertTrue(inherited_sources(s))
        for anchor in s["anchors"]:
            t=ParameterTrial(forced={"anchor":anchor["config_identity"],"anchor_control":True})
            self.assertEqual(sample_config(t,self.defaults,s),anchor["config"])
        n=len(s["anchors"])
        run=argparse.Namespace(plan=self.plan,space=path,binary=self.binary,out=self.root/"s2-controls",max_trials=n,max_go_evaluations=n,max_ledger_evaluations=3*n,timeout=30,startup=1,seed=42,sampler="independent",until_trials=None,inspect=False)
        result=controller.run(run);self.assertEqual(result["trial_states"],{"COMPLETE":n})
        s3=self.root/"s3-after-controls.json"
        a.stage="S3";a.out=s3;a.summaries=[self.root/"study"/"summary.json",run.out/"summary.json"]
        result=freeze_stage.freeze(a);self.assertGreaterEqual(result["anchor_count"],1)
        self.assertTrue(any(s["stage"]=="S2" for s in load_space(s3)["parents"]))

    def test_frozen_preparation_option_and_actual_inspect_race(self):
        a=self.args();a.fixed_candidates=False;manifest=g.prepare(a)
        before=(a.out/"manifest.json").read_bytes();a.fixed_candidates=True
        with self.assertRaises(RuntimeError):g.prepare(a)
        self.assertEqual((a.out/"manifest.json").read_bytes(),before)
        entry=manifest["plans"][0]
        plan_path=Path(entry["plan"]);original=plan_path.read_bytes()
        c=argparse.Namespace(plan=plan_path,provenance=Path(entry["provenance"]),binary=self.binary,
            out=a.out/"race-run",budget=entry["ledger_budget"],timeout=30,kind=entry["kind"])
        real=continuous_enhanced.walkforward.child_json
        def changed_plan(command,timeout,termination):
            p=json.loads(plan_path.read_text());new=(datetime.fromisoformat(p["outer"][0]["start"].replace("Z","+00:00"))-timedelta(days=1)).isoformat().replace("+00:00","Z")
            p["outer"][0]["start"]=new;p["outer"][0]["inner"][-1]["end"]=new
            plan_path.write_text(json.dumps(p))
            return real(command,timeout,termination) # actual Go inspect, no fabricated success
        continuous_enhanced.walkforward.child_json=changed_plan
        try:
            with self.assertRaises(RuntimeError):continuous_enhanced.run(c)
            self.assertFalse((c.out/"summary.json").exists())
        finally:
            continuous_enhanced.walkforward.child_json=real;plan_path.write_bytes(original)

    def test_source_data_snapshot_drift_rejected(self):
        path=self.root/"data"/"funding.json";original=path.read_bytes()
        try:
            path.write_bytes(original+b"\n")
            with self.assertRaises(ValueError):g.prepare(self.args())
        finally:path.write_bytes(original)

if __name__=="__main__":unittest.main()

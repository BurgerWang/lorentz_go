import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import optuna
import optimize_enhanced as controller
from enhanced_space import *
from enhanced_protocol import Server,validate_result
from synthetic_fixture import make_fixture
from test_enhanced_space import space,ParameterTrial

class ControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.binary=Path(os.environ.get("LORENTZ_TEST_BINARY","/tmp/lorentz-enhanced-t2"))
        cls.root=Path(tempfile.mkdtemp(prefix="lorentz-t2-synthetic-"))
        cls.plan=make_fixture(cls.root/"data")
        cls.defaults=go_defaults(cls.binary)
    @classmethod
    def tearDownClass(cls): shutil.rmtree(cls.root)
    def args(self,mode="independent",trials=4):
        root=Path(tempfile.mkdtemp(dir=self.root));s=space(stage="S1");s["daily_history_start"]="2024-01-01"
        s["domains"]={"neighbors":{"low":2,"high":2,"step":1},"max_bars_back":[500],"feature_count":{"low":2,"high":2,"step":1}}
        path=root/"space.json";path.write_text(json.dumps(s))
        return argparse.Namespace(plan=self.plan,space=path,binary=self.binary,out=root/"out",max_trials=trials,
            max_go_evaluations=trials,max_ledger_evaluations=3*trials,timeout=30,startup=1,seed=42,sampler=mode,until_trials=None,inspect=False)
    def test_true_go_three_samplers_and_boundary_resume(self):
        for mode in ("random","independent","grouped"):
            a=self.args(mode);a.until_trials=2;controller.run(a)
            a.until_trials=None;resumed=controller.run(a)
            b=self.args(mode);full=controller.run(b)
            self.assertEqual([x["params"] for x in resumed["trial_records"]],[x["params"] for x in full["trial_records"]])
            self.assertEqual(resumed["budget"]["trial_attempts"],4)
            self.assertEqual(resumed["budget"]["ledger_evaluations_reserved"],12)
            self.assertEqual(resumed["trial_states"],{"COMPLETE":4})
            self.assertEqual(controller.run(a)["budget"],resumed["budget"])
    def test_cache_seeds_and_request_budget(self):
        a=self.args(trials=2);s=json.loads(a.space.read_text())
        c=sample_config(ParameterTrial(),self.defaults,s)
        s["seeds"]=[{"name":"control","config":c},{"name":"repeat","config":c}]
        a.space.write_text(json.dumps(s));a.max_go_evaluations=1;a.max_ledger_evaluations=3
        r=controller.run(a)
        self.assertEqual(r["trial_states"],{"COMPLETE":2})
        self.assertEqual((r["budget"]["cache_hits"],r["budget"]["go_evaluations_reserved"],r["budget"]["ledger_evaluations_reserved"]),(1,1,3))
        self.assertEqual(r["trial_records"][0]["params"],r["trial_records"][1]["params"])
        b=self.args(trials=0);r=controller.run(b)
        self.assertEqual(r["budget"]["go_evaluations_reserved"],0)
    def test_unknown_running_reservation_recovery_no_replay(self):
        a=self.args(trials=2);a.until_trials=1;controller.run(a)
        storage=optuna.storages.JournalStorage(optuna.storages.journal.JournalFileBackend(str(a.out/"study.journal")))
        study=optuna.load_study(study_name=controller.STUDY,storage=storage)
        t=study.ask();t.set_user_attr("execution",{"key":"interrupted","go_reserved":1,"ledger_reserved":3,"cache_trial":None})
        a.until_trials=None;r=controller.run(a)
        self.assertEqual(r["trial_states"],{"COMPLETE":1,"FAIL":1})
        self.assertEqual(r["budget"]["go_evaluations_reserved"],2)
        self.assertEqual(r["budget"]["uncertain_ledger_charged"],3)
    def test_contract_drift_lock_and_bad_journal(self):
        import fcntl
        a=self.args(trials=0);controller.run(a)
        a.max_go_evaluations=1
        with self.assertRaises(RuntimeError): controller.run(a)
        b=self.args(trials=0);b.out.mkdir()
        with (b.out/"run.lock").open("a+") as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):controller.run(b)
        c=self.args(trials=0);controller.run(c)
        with (c.out/"study.journal").open("ab") as f:f.write(b'{"op_code":0,"op_code":0}\n')
        with self.assertRaises(ValueError): controller.run(c)
    def test_actual_protocol_and_mismatch_rejection(self):
        with_controller=Server(self.binary,self.plan,30)
        try:
            c=normalize_config(self.defaults,self.defaults);c["daily"]["history_start"]="2024-01-01"
            r=with_controller.evaluate(c)
            for key,value in (("id",True),("plan",{}),("data_identity","changed"),("daily_identity","changed")):
                bad=deepcopy(r);bad[key]=value
                with self.assertRaises(controller.legacy.ProtocolError):validate_result(bad,c,with_controller.ready,r["id"])
            bad=deepcopy(r);bad["aggregate"]["net_return_pct"]=float("nan")
            with self.assertRaises(controller.legacy.ProtocolError):validate_result(bad,c,with_controller.ready)
            bad=deepcopy(r);bad["folds"][0]["metrics"]={}
            with self.assertRaises(controller.legacy.ProtocolError):validate_result(bad,c,with_controller.ready)
        finally:with_controller.close()
    def test_constraint_boundaries(self):
        limits=space()["constraints"]
        r={"aggregate":{"trades":30,"positive_folds":2,"net_return_pct":1.,"max_fold_drawdown_pct":30.}}
        self.assertTrue(controller.feasible(r,limits))
        for key,v in (("trades",29),("positive_folds",1),("net_return_pct",0),("max_fold_drawdown_pct",30.01)):
            bad=deepcopy(r);bad["aggregate"][key]=v;self.assertFalse(controller.feasible(bad,limits))

    def test_seed_prevalidation_and_partial_queue_recovery(self):
        a=self.args(trials=2);s=json.loads(a.space.read_text());c=sample_config(ParameterTrial(),self.defaults,s)
        invalid=deepcopy(c);invalid["classic"]["neighbors"]=101
        s["seeds"]=[{"name":"first","config":c},{"name":"second","config":invalid}];a.space.write_text(json.dumps(s))
        for _ in range(2):
            with self.assertRaises(ValueError):controller.run(a)
            self.assertFalse((a.out/"study.journal").exists())
        s["seeds"][1]["config"]=c;a.space.write_text(json.dumps(s));a.until_trials=0
        original=optuna.study.Study.enqueue_trial;calls=[]
        def interrupted_enqueue(study,*args,**kwargs):
            if calls:raise RuntimeError("injected failure before second journal enqueue")
            calls.append(1);return original(study,*args,**kwargs)
        optuna.study.Study.enqueue_trial=interrupted_enqueue
        try:
            with self.assertRaises(RuntimeError):controller.run(a)
        finally:optuna.study.Study.enqueue_trial=original
        storage=optuna.storages.JournalStorage(optuna.storages.journal.JournalFileBackend(str(a.out/"study.journal")))
        study=optuna.load_study(study_name=controller.STUDY,storage=storage)
        self.assertEqual(len(study.trials),1)
        # The first queued seed really persisted; the second did not.
        a.until_trials=None;r=controller.run(a)
        self.assertEqual([x["name"] for x in r["controls"]],["first","second"])
        self.assertEqual(r["budget"]["trial_attempts"],2)

    def test_distinct_seeds_keep_distinct_parameter_maps(self):
        a=self.args(trials=2);s=json.loads(a.space.read_text())
        first=sample_config(ParameterTrial(1),self.defaults,s);second=sample_config(ParameterTrial(2),self.defaults,s)
        s["seeds"]=[{"name":"distinct-first","config":first},{"name":"distinct-second","config":second}];a.space.write_text(json.dumps(s))
        r=controller.run(a)
        self.assertEqual(r["trial_states"],{"COMPLETE":2})
        self.assertNotEqual(r["trial_records"][0]["params"],r["trial_records"][1]["params"])
        self.assertEqual(r["controls"][0]["response"]["config"],first)
        self.assertEqual(r["controls"][1]["response"]["config"],second)

    def test_plan_dataset_switch_between_snapshot_and_go_load_refused(self):
        a=self.args(trials=0);original=a.plan
        p=a.space.parent/"plan.json";p.write_bytes(original.read_bytes());a.plan=p
        other=make_fixture(a.space.parent/"other-data")
        original_server=controller.Server
        def changed_server(*args,**kwargs):
            p.write_bytes(other.read_bytes())
            return original_server(*args,**kwargs) # real Go ready for the different dataset
        controller.Server=changed_server
        try:
            with self.assertRaises(RuntimeError):controller.run(a)
            self.assertFalse((a.out/"study.journal").exists())
        finally:controller.Server=original_server

if __name__=="__main__":unittest.main()

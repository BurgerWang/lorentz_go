import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
import enhanced_check
from enhanced_space import go_validate
from synthetic_fixture import make_fixture

class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.binary=Path(os.environ.get("LORENTZ_TEST_BINARY","/tmp/lorentz-enhanced-t2"))
        cls.root=Path(tempfile.mkdtemp(prefix="lorentz-t4-synthetic-"))
        cls.plans={i:make_fixture(cls.root/i,interval=i) for i in ("1h","15m")}
        cls.matrix=json.loads((Path(__file__).parent/"spaces/enhanced-v1/B0-fixed-20.json").read_text())
        cls.matrix["data_use"]="synthetic"
        for e in cls.matrix["requests"]:
            e["plan"]=str(cls.plans["15m" if e["name"].startswith("15m") else "1h"])
            e["config"]["daily"]["history_start"]="2024-01-01"
    @classmethod
    def tearDownClass(cls):shutil.rmtree(cls.root)
    def args(self):
        p=self.root/"matrix.json";p.write_text(json.dumps(self.matrix))
        return argparse.Namespace(matrix=p,binary=self.binary,out=Path(tempfile.mkdtemp(dir=self.root))/"run",max_go_evaluations=20,max_ledger_evaluations=60,timeout=60,inspect=False)
    def test_frozen_b0_shape_actual_go_two_intervals_and_independent_replays(self):
        a=self.args();r=enhanced_check.run(a)
        self.assertEqual(r["budget"],{"trial_attempts":0,"go_evaluations_reserved":20,"ledger_evaluations_reserved":60,"cache_hits":0})
        self.assertTrue(all(x["status"]=="COMPLETE" for x in r["records"]))
        self.assertEqual(sum(x.get("replay_equal",False) for x in r["records"]),10)
        self.assertEqual(enhanced_check.run(a)["budget"],r["budget"])
        (a.out/"summary.json").unlink() # disposable fault: loss of authoritative state
        with self.assertRaises(RuntimeError):enhanced_check.run(a)
    def test_budget_and_replay_input_refused_before_evaluation(self):
        a=self.args();a.max_go_evaluations=19
        with self.assertRaises(ValueError):enhanced_check.run(a)
        self.assertFalse(a.out.exists())
        a=self.args();m=deepcopy(self.matrix);m["requests"][1]["config"]["classic"]["neighbors"]+=1;a.matrix.write_text(json.dumps(m))
        with self.assertRaises(ValueError):enhanced_check.run(a)
        self.assertFalse(a.out.exists())

if __name__=="__main__":unittest.main()

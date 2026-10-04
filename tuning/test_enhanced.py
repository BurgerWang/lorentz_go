import argparse
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import enhanced

class FakeServer:
    calls = []
    ready = {"type": "ready", "protocol_version": 2, "plan": {}, "default_config": {}, "data_identity": "fixture", "daily_identity": "fixture"}
    def __init__(self, *args, **kwargs): pass
    def read_ready(self): pass
    def evaluate(self, config):
        self.calls.append(config)
        return {"type": "result", "config": config, "aggregate": {"trades": 1}}
    def close(self): pass

class MatrixTests(unittest.TestCase):
    def fixture(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(__import__('shutil').rmtree, root)
        matrix = root / 'matrix.json'
        entries = [{"name": str(i), "plan": str(root / 'plan'), "config": {"number": i}} for i in range(2)]
        matrix.write_text(json.dumps(entries))
        binary = root / 'binary'; binary.write_text('fixture')
        return argparse.Namespace(matrix=matrix, budget=2, out=root / 'run', binary=binary, timeout=1)
    def test_budget_and_frozen_resume(self):
        args = self.fixture(); FakeServer.calls = []
        with mock.patch.object(enhanced, 'EnhancedServer', FakeServer):
            enhanced.run(args); enhanced.run(args)
            self.assertEqual(len(FakeServer.calls), 2)
            args.budget = 3
            with self.assertRaises(RuntimeError): enhanced.run(args)
    def test_interrupted_call_consumes_budget(self):
        args = self.fixture(); FakeServer.calls = []
        with mock.patch.object(enhanced, 'EnhancedServer', FakeServer):
            enhanced.run(args)
            state_path = args.out / 'summary.json'
            state = json.loads(state_path.read_text())[:1]; state[0] = {"name": "0", "status": "RUNNING"}
            state_path.write_text(json.dumps(state)); FakeServer.calls = []
            state = enhanced.run(args)
            self.assertEqual(len(FakeServer.calls), 1)
            self.assertEqual(state[0]['status'], 'FAIL')
    def test_budget_rejects_before_process_start(self):
        args = self.fixture(); args.budget=1
        with mock.patch.object(enhanced, 'EnhancedServer') as server:
            with self.assertRaises(ValueError): enhanced.run(args)
            server.assert_not_called()
    def test_strict_nested_json(self):
        for raw in ['[{"name":"x","plan":"p","config":{"daily":{"enabled":false,"enabled":true}}}]', '[{"name":"x","plan":"p","config":{"r":NaN}}]','[{"name":"x","plan":"p","config":{"r":1e309}}]']:
            args=self.fixture(); args.matrix.write_text(raw)
            with self.assertRaises(ValueError): enhanced.load_matrix(args.matrix,2)
    def test_term_closes_all_children(self):
        import os, signal
        args=self.fixture()
        matrix=json.loads(args.matrix.read_text()); matrix[1]['plan']+='second';args.matrix.write_text(json.dumps(matrix))
        instances=[]
        class StoppingServer(FakeServer):
            def __init__(self,*args,**kwargs): self.closed=False;instances.append(self)
            def close(self):
                self.closed=True
                if self is instances[0]: os.kill(os.getpid(),signal.SIGTERM)
        with mock.patch.object(enhanced,'EnhancedServer',StoppingServer):
            with self.assertRaises(enhanced.legacy.TerminationRequested): enhanced.run(args)
        self.assertEqual([s.closed for s in instances],[True,True])

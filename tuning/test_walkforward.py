import argparse
import json
import os
from pathlib import Path
import tempfile
import unittest

import walkforward


FAKE = '''#!/usr/bin/env python3
import json, os, signal, sys, time
from pathlib import Path
plan = json.loads(Path(sys.argv[sys.argv.index("-plan")+1]).read_text())
root = Path(__file__).parent
mode = plan.get("mode", "success")
if "-inspect" in sys.argv:
    if mode == "badinspect": print('{"type":"ready","type":"ready"}');sys.exit(0)
    print(json.dumps({"type":"ready","protocol_version":1,"plan":plan,"required_evaluations":plan["budget"],"data_identity":"fixture","daily_identity":"fixture","exposure_identity":"fixture"}))
    sys.exit(0)
with (root/"calls").open("a") as f: f.write("run\\n")
(root/"child.pid").write_text(str(os.getpid()))
if mode in ("sleep", "term", "interrupt"):
    def stop(signum, frame):
        (root/"terminated").write_text("yes")
        sys.exit(0)
    signal.signal(signal.SIGTERM, stop)
    if mode == "term": os.kill(os.getppid(), signal.SIGTERM)
    if mode == "interrupt": os.kill(os.getppid(), signal.SIGINT)
    time.sleep(30)
elif mode == "badjson": print('{"type":"result","evaluations_used":NaN}')
elif mode == "duplicate": print('{"type":"result","evaluations_used":2,"evaluations_used":2}')
elif mode == "overflow": print('{"type":"result","evaluations_used":2,"x":1e309}')
elif mode == "count": print(json.dumps({"type":"result","evaluations_used":1}))
elif mode == "bool": print(json.dumps({"type":"result","evaluations_used":True}))
elif mode == "exit": print("failed",file=sys.stderr);sys.exit(3)
else:
    result = {"type":"result","protocol_version":1,"evaluations_used":plan["budget"],"plan":plan,"data_identity":"fixture","daily_identity":"fixture","exposure_identity":"fixture","decision":"fixture"}
    if mode.startswith("drift_"):
        field = mode[len("drift_"):]
        result[field] = {"budget":plan["budget"],"changed":True} if field == "plan" else "changed"
    if mode == "protocol": result["protocol_version"] = 2
    if mode == "missing_identity": del result["exposure_identity"]
    print(json.dumps(result))
'''


class WalkforwardTests(unittest.TestCase):
    def fixture(self, mode="success"):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, root)
        plan = root / "plan.json"
        plan.write_text(json.dumps({"budget": 2, "mode": mode}))
        binary = root / "fake"
        binary.write_text(FAKE)
        binary.chmod(0o700)
        return argparse.Namespace(plan=plan, binary=binary, budget=2,
                                  timeout=2, out=root / "out")

    def calls(self, args):
        path = args.plan.parent / "calls"
        return path.read_text().splitlines() if path.exists() else []

    def state(self, args):
        return json.loads((args.out / "summary.json").read_text())

    def test_exact_budget_complete_and_resume(self):
        args = self.fixture()
        state = walkforward.run(args)
        self.assertEqual(state["status"], "COMPLETE")
        self.assertEqual(state["reserved_evaluations"], 2)
        self.assertEqual(walkforward.run(args), state)
        self.assertEqual(self.calls(args), ["run"])

    def test_budget_refused_before_batch_and_output_contract(self):
        for budget in (1, 3):
            args = self.fixture(); args.budget = budget
            with self.assertRaises(ValueError): walkforward.run(args)
            self.assertEqual(self.calls(args), [])
            self.assertEqual([p.name for p in args.out.iterdir()], ["run.lock"])

    def test_running_recovery_is_failed_without_repeat(self):
        args = self.fixture(); walkforward.run(args)
        (args.out / "summary.json").write_text(json.dumps({"status":"RUNNING","reserved_evaluations":2}))
        state = walkforward.run(args)
        self.assertEqual(state["status"], "FAIL")
        self.assertEqual(self.calls(args), ["run"])
        self.assertEqual(walkforward.run(args), state)

    def test_contract_drift_and_nonempty_directory(self):
        for drift in ("plan", "binary", "budget", "timeout"):
            args = self.fixture(); walkforward.run(args)
            if drift == "plan": args.plan.write_text('{"budget":2,"mode":"success","change":1}')
            if drift == "binary": args.binary.write_text(FAKE + "\n# changed\n")
            if drift == "budget": args.budget = 3
            if drift == "timeout": args.timeout = 3
            with self.assertRaises((RuntimeError, ValueError)): walkforward.run(args)
            self.assertEqual(self.calls(args), ["run"])
        args = self.fixture(); args.out.mkdir(); (args.out / "foreign").write_text("keep")
        with self.assertRaises(RuntimeError): walkforward.run(args)
        self.assertEqual(self.calls(args), [])

    def test_failed_protocol_consumes_budget_and_cannot_retry(self):
        for mode in ("badjson", "duplicate", "overflow", "count", "bool", "exit"):
            args = self.fixture(mode)
            with self.assertRaises(walkforward.legacy.ProtocolError): walkforward.run(args)
            self.assertEqual(self.state(args)["status"], "FAIL")
            self.assertEqual(self.state(args)["reserved_evaluations"], 2)
            walkforward.run(args)
            self.assertEqual(self.calls(args), ["run"])

    def test_strict_input_and_inspect(self):
        for raw in ('{"budget":2,"budget":2}', '{"budget":2,"x":NaN}', '{"budget":2,"x":1e309}', '[]'):
            args = self.fixture(); args.plan.write_text(raw)
            with self.assertRaises(ValueError): walkforward.run(args)
            self.assertEqual(self.calls(args), [])
        args = self.fixture("badinspect")
        with self.assertRaises(walkforward.legacy.ProtocolError): walkforward.run(args)
        self.assertFalse((args.out / "summary.json").exists())

    def test_timeout_term_and_interrupt_cleanup(self):
        for mode, exception in (("sleep",walkforward.legacy.ProtocolError),
                                ("term",walkforward.legacy.TerminationRequested),
                                ("interrupt",KeyboardInterrupt)):
            args = self.fixture(mode); args.timeout = 1
            with self.assertRaises(exception): walkforward.run(args)
            self.assertEqual(self.state(args)["status"], "FAIL")
            self.assertEqual(self.state(args)["reserved_evaluations"], 2)
            self.assertTrue((args.plan.parent / "terminated").exists())
            pid = int((args.plan.parent / "child.pid").read_text())
            with self.assertRaises(ProcessLookupError): os.kill(pid, 0)
            walkforward.run(args)
            self.assertEqual(self.calls(args), ["run"])

    def test_locked_output_prevents_inspect_or_batch(self):
        import fcntl
        args = self.fixture(); args.out.mkdir()
        with (args.out / "run.lock").open("a+") as lock:
            fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError): walkforward.run(args)
        self.assertEqual(self.calls(args), [])

    def test_contract_without_state_does_not_restart(self):
        args = self.fixture(); walkforward.run(args)
        (args.out / "summary.json").unlink()
        with self.assertRaises(RuntimeError): walkforward.run(args)
        self.assertEqual(self.calls(args), ["run"])

    def test_actual_identity_plan_protocol_drift_consumes_budget(self):
        for mode in ("drift_plan", "drift_data_identity", "drift_daily_identity",
                     "drift_exposure_identity", "protocol", "missing_identity"):
            args = self.fixture(mode)
            with self.assertRaises(walkforward.legacy.ProtocolError): walkforward.run(args)
            self.assertEqual(self.state(args)["status"], "FAIL")
            self.assertEqual(self.state(args)["reserved_evaluations"], 2)
            walkforward.run(args)
            self.assertEqual(self.calls(args), ["run"])

    def test_complete_restore_revalidates_bound_result(self):
        for field in ("plan", "data_identity", "daily_identity", "exposure_identity", "protocol_version"):
            args = self.fixture(); walkforward.run(args)
            state = self.state(args)
            state["result"][field] = {"changed": True} if field == "plan" else "changed"
            (args.out / "summary.json").write_text(json.dumps(state))
            with self.assertRaises(walkforward.legacy.ProtocolError): walkforward.run(args)
            self.assertEqual(self.state(args)["status"], "FAIL")
            self.assertEqual(self.state(args)["reserved_evaluations"], 2)
            walkforward.run(args)
            self.assertEqual(self.calls(args), ["run"])


if __name__ == "__main__":
    unittest.main()

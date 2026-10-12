"""Faults around actual Go protocol2; all candles and studies are disposable."""
import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

import optuna
import optimize_enhanced as controller
from enhanced_space import go_defaults
from synthetic_fixture import make_fixture
from test_enhanced_space import space


# This bridge never invents a successful ready/result. It executes the real
# binary and only corrupts, delays or drops its actual protocol messages. The
# script stays identical across recovery; the fault mode is external test state.
BRIDGE = r'''#!__PYTHON__
import json, os, signal, subprocess, sys, time
actual = os.environ["LORENTZ_ACTUAL_BINARY"]
if sys.argv[1] != "enhanced-eval":
    os.execv(actual, [actual] + sys.argv[1:])
fault = os.environ.get("LORENTZ_FAULT_MODE", "none")
child = subprocess.Popen([actual] + sys.argv[1:], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=None)
def stop(signum, frame):
    raise SystemExit(128 + signum)
signal.signal(signal.SIGTERM, stop)
def event(kind, **extra):
    with open(os.environ["LORENTZ_FAULT_EVENTS"], "a") as f:
        f.write(json.dumps(dict(kind=kind, wrapper_pid=os.getpid(),
                               go_pid=child.pid, **extra)) + "\n")
        f.flush(); os.fsync(f.fileno())
def flood():
    for i in range(2048):
        os.write(2, ("fault-stderr-%d " % i + "x" * 1024 + "\n").encode())
def emit(line):
    sys.stdout.buffer.write(line); sys.stdout.buffer.flush()
def hold():
    while True: time.sleep(.1)
try:
    ready = child.stdout.readline()
    if not ready: raise RuntimeError("actual Go did not emit ready")
    event("actual_ready")
    if fault == "stderr": flood()
    if fault == "exit_ready": raise SystemExit(7)
    if fault == "timeout_ready": hold()
    if fault == "ready_json": ready = b'{"type":\n'
    if fault == "ready_identity":
        r=json.loads(ready); r["daily_identity"]="not-a-digest"
        ready=(json.dumps(r)+"\n").encode()
    emit(ready)
    for request in sys.stdin.buffer:
        req=json.loads(request)
        event("reserved_request", id=req["id"])
        if fault == "hold_before_request": hold()
        child.stdin.write(request); child.stdin.flush()
        event("forwarded_request", id=req["id"])
        if fault == "hold_after_request": hold()
        result = child.stdout.readline()
        if not result: raise RuntimeError("actual Go did not emit result")
        event("actual_result", id=req["id"])
        if fault == "stderr": flood()
        if fault == "exit_result": raise SystemExit(8)
        if fault == "timeout_result": hold()
        r=json.loads(result)
        if fault == "result_json": result=b'{"id":1,"id":1}\n'
        if fault == "result_id":
            r["id"]=True; result=(json.dumps(r)+"\n").encode()
        if fault == "result_identity":
            r["data_identity"]="0"*64; result=(json.dumps(r)+"\n").encode()
        if fault == "result_config":
            r["config"]["classic"]["neighbors"]+=1
            result=(json.dumps(r)+"\n").encode()
        if fault == "result_nonfinite":
            r["aggregate"]["net_return_pct"]=float("nan")
            result=(json.dumps(r)+"\n").encode()
        if fault == "result_structure":
            r["folds"][0]=None; result=(json.dumps(r)+"\n").encode()
        if fault == "result_overflow":
            r["aggregate"]["net_return_pct"]=10**400
            result=(json.dumps(r)+"\n").encode()
        if fault == "result_depth":
            result=b'{"id":1,"nested":'+b'['*2000+b'0'+b']'*2000+b'}\n'
        emit(result)
finally:
    if child.poll() is None:
        child.terminate()
        try: child.wait(timeout=2)
        except subprocess.TimeoutExpired: child.kill(); child.wait()
'''


class ActualGoFaultTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.binary = Path(os.environ.get("LORENTZ_TEST_BINARY", "/tmp/lorentz-enhanced-t2")).resolve()
        if not cls.binary.is_file():
            raise RuntimeError("build the disposable test binary and set LORENTZ_TEST_BINARY")
        cls.root = Path(tempfile.mkdtemp(prefix="lorentz-t2-faults-"))
        cls.plan = make_fixture(cls.root / "data")
        cls.defaults = go_defaults(cls.binary)
        cls.bridge = cls.root / "actual-go-bridge"
        cls.bridge.write_text(BRIDGE.replace("__PYTHON__", sys.executable))
        cls.bridge.chmod(0o700)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root)

    def job(self, fault="none", timeout=3):
        root = Path(tempfile.mkdtemp(dir=self.root))
        s = space(stage="S1")
        s["daily_history_start"] = "2024-01-01"
        s["domains"] = {"neighbors": {"low": 2, "high": 2, "step": 1},
                        "max_bars_back": [500], "feature_count": {"low": 2, "high": 2, "step": 1}}
        sp = root / "space.json"
        sp.write_text(json.dumps(s))
        args = argparse.Namespace(plan=self.plan, space=sp, binary=self.bridge,
                                  out=root / "out", max_trials=1, max_go_evaluations=1,
                                  max_ledger_evaluations=3, timeout=timeout,
                                  startup=1, seed=42, sampler="random",
                                  until_trials=None, inspect=False)
        env = {"LORENTZ_ACTUAL_BINARY": str(self.binary), "LORENTZ_FAULT_MODE": fault,
               "LORENTZ_FAULT_EVENTS": str(root / "events.jsonl")}
        return args, env

    def events(self, env):
        path = Path(env["LORENTZ_FAULT_EVENTS"])
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def run_controller(self, args, env):
        old = {key: os.environ.get(key) for key in env}
        os.environ.update(env)
        try:
            return controller.run(args)
        finally:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def assert_failed_charge(self, summary):
        self.assertEqual(summary["trial_states"], {"FAIL": 1})
        expected = {"trial_attempts": 1, "go_evaluations_reserved": 1,
                    "ledger_evaluations_reserved": 3, "successful_ledger_evaluations": 0,
                    "uncertain_ledger_charged": 3}
        for key, value in expected.items():
            self.assertEqual(summary["budget"][key], value, key)

    def test_real_ready_corruption_exit_and_timeout(self):
        for fault in ("ready_json", "ready_identity", "exit_ready", "timeout_ready"):
            with self.subTest(fault=fault):
                args, env = self.job(fault, timeout=1)
                with self.assertRaises(controller.legacy.ProtocolError):
                    self.run_controller(args, env)
                self.assertEqual([e["kind"] for e in self.events(env)], ["actual_ready"])
                self.assertFalse((args.out / "study.journal").exists())

    def test_real_result_faults_consume_budget_and_never_replay(self):
        for fault in ("result_json", "result_id", "result_identity", "result_config",
                      "result_nonfinite", "result_structure", "result_overflow", "result_depth", "exit_result", "timeout_result"):
            with self.subTest(fault=fault):
                args, env = self.job(fault, timeout=1)
                with self.assertRaises(controller.legacy.ProtocolError):
                    self.run_controller(args, env)
                self.assert_failed_charge(json.loads((args.out / "summary.json").read_text()))
                self.assertEqual(sum(e["kind"] == "actual_result" for e in self.events(env)), 1)
                env["LORENTZ_FAULT_MODE"] = "none"
                self.assert_failed_charge(self.run_controller(args, env))
                self.assertEqual(sum(e["kind"] == "forwarded_request" for e in self.events(env)), 1)

    def test_structural_protocol_halt_persists_with_remaining_trials(self):
        for fault in ("result_structure","result_overflow","result_depth"):
            with self.subTest(fault=fault):
                args,env=self.job(fault,timeout=3)
                args.max_trials=2;args.max_go_evaluations=2;args.max_ledger_evaluations=6
                with self.assertRaises(controller.legacy.ProtocolError):self.run_controller(args,env)
                env["LORENTZ_FAULT_MODE"]="none"
                summary=self.run_controller(args,env)
                self.assertTrue(summary["halted_protocol"])
                self.assertEqual(summary["budget"]["trial_attempts"],1)
                self.assertEqual(sum(e["kind"]=="forwarded_request" for e in self.events(env)),1)

    def test_stderr_backpressure_preserves_actual_success(self):
        args, env = self.job("stderr", timeout=10)
        result = self.run_controller(args, env)
        self.assertEqual(result["trial_states"], {"COMPLETE": 1})
        self.assertEqual(result["budget"]["successful_ledger_evaluations"], 3)
        self.assertEqual(sum(e["kind"] == "actual_result" for e in self.events(env)), 1)

    def cli(self, args):
        command = [sys.executable, str(Path(controller.__file__).resolve())]
        for key in ("plan", "space", "binary", "out", "max_trials", "max_go_evaluations",
                    "max_ledger_evaluations", "timeout", "startup", "seed", "sampler"):
            command += ["--" + key.replace("_", "-"), str(getattr(args, key))]
        return command

    def wait_event(self, proc, env, kind):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            for event in self.events(env):
                if event["kind"] == kind:
                    return event
            if proc.poll() is not None:
                self.fail("controller exited before interruption checkpoint")
            time.sleep(.01)
        self.fail("actual request checkpoint not reached")

    def test_actual_term_kill_after_reservation_and_during_request(self):
        for mode, checkpoint in (("hold_before_request", "reserved_request"),
                                 ("hold_after_request", "forwarded_request")):
            for stop_signal in (signal.SIGTERM, signal.SIGKILL):
                with self.subTest(mode=mode, signal=stop_signal):
                    args, env = self.job(mode, timeout=30)
                    log = args.out.parent / "controller.log"
                    with log.open("wb") as output:
                        proc = subprocess.Popen(self.cli(args), env={**os.environ, **env},
                                                stdout=output, stderr=output, start_new_session=True)
                        try:
                            event = self.wait_event(proc, env, checkpoint)
                            # The bridge sees stdin only after the durable execution
                            # reservation; verify the real journal at this live point.
                            storage = optuna.storages.JournalStorage(
                                optuna.storages.journal.JournalFileBackend(str(args.out / "study.journal")))
                            study = optuna.load_study(study_name=controller.STUDY, storage=storage)
                            self.assertEqual(study.trials[0].state.name, "RUNNING")
                            self.assertEqual(study.trials[0].user_attrs["execution"]["ledger_reserved"], 3)
                            os.kill(event["go_pid"], 0)  # actual Go is present at interruption
                            if stop_signal == signal.SIGKILL:
                                os.killpg(proc.pid, stop_signal)
                            else:
                                proc.send_signal(stop_signal)
                            code = proc.wait(timeout=10)
                            self.assertEqual(code, 143 if stop_signal == signal.SIGTERM else -signal.SIGKILL)
                        finally:
                            if proc.poll() is None:
                                os.killpg(proc.pid, signal.SIGKILL)
                                proc.wait(timeout=5)
                            # Also clean any orphan from an assertion/timeout path.
                            try:
                                os.killpg(proc.pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                    if stop_signal == signal.SIGKILL:
                        storage = optuna.storages.JournalStorage(
                            optuna.storages.journal.JournalFileBackend(str(args.out / "study.journal")))
                        study = optuna.load_study(study_name=controller.STUDY, storage=storage)
                        self.assertEqual(study.trials[0].state.name, "RUNNING")
                    env["LORENTZ_FAULT_MODE"] = "none"
                    result = self.run_controller(args, env)
                    self.assert_failed_charge(result)
                    self.assertEqual(sum(e["kind"] == "reserved_request" for e in self.events(env)), 1)
                    self.assertEqual(sum(e["kind"] == "forwarded_request" for e in self.events(env)),
                                     1 if mode == "hold_after_request" else 0)
                    if stop_signal == signal.SIGKILL:
                        self.assertIn("interrupted", result["trial_records"][0]["error"])


if __name__ == "__main__":
    unittest.main()

"""Controller recovery regressions; every journal and child belongs to a temp fixture."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import optimize as runner
from test_optimize import defaults


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        try:
            import optuna
        except ImportError:
            self.skipTest("Optuna not installed")
        self.optuna = optuna
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.addCleanup(self.cleanup_evaluators)
        self.plan = self.root / "plan.json"
        self.write_plan("4h")
        self.control = self.root / "control.json"
        self.control.write_text("{}")
        self.marker = self.root / "waiting.json"
        self.binary = self.root / "fake-evaluator"
        self.binary.write_text(f'''#!{sys.executable}
import json, os, sys, time
from pathlib import Path
root = Path(__file__).parent
with (root / 'child-pids').open('a') as output:
    output.write(str(os.getpid()) + '\\n')
control = json.loads((root / 'control.json').read_text())
def pause(phase):
    (root / 'waiting.json').write_text(json.dumps(dict(phase=phase, pid=os.getpid())))
    time.sleep(60)
if control.get('phase') == 'ready':
    pause('ready')
plan = json.loads(Path(sys.argv[-1]).read_text())
print(json.dumps(dict(type='ready', protocol_version=1, default_config={defaults()!r},
                     plan=plan, data_identity='fixture')), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    if control.get('phase') == 'evaluation' and request['id'] == 2:
        pause('evaluation')
    c = request['config']
    improved = c['neighbors'] != 8
    print(json.dumps(dict(type='result', id=request['id'], config=c, folds=[],
        aggregate=dict(trades=30, wins=15, positive_folds=2,
                       net_return_pct=2. if improved else 1.,
                       win_rate_pct=51. if improved else 50.))), flush=True)
''')
        self.binary.chmod(0o755)
        self.out = self.root / "out"

    def write_plan(self, interval):
        self.plan.write_text(json.dumps(dict(interval=interval, min_trades=30,
                                             min_positive_folds=2)))

    def args(self, total=3, out=None):
        return ["--plan", str(self.plan), "--binary", str(self.binary),
                "--out", str(out if out is not None else self.out),
                "--trials", str(total), "--eval-timeout", "5"]

    def study(self, out=None):
        journal = (out or self.out) / "study.journal"
        storage = self.optuna.storages.JournalStorage(
            self.optuna.storages.journal.JournalFileBackend(str(journal)))
        return self.optuna.load_study(study_name=runner.STUDY_NAME, storage=storage)

    def protected_bytes(self, out=None):
        out = out or self.out
        return {p.name: p.read_bytes() for p in out.iterdir()
                if p.name in ("study.journal", "summary.json", runner.CONTRACT_FILE)
                or p.name.startswith("candidate-trial-")}

    @staticmethod
    def pid_alive(pid):
        try:
            # A reaped evaluator must disappear; a zombie is no longer executing.
            return Path(f"/proc/{pid}/stat").read_text().split()[2] not in ("Z", "X")
        except FileNotFoundError:
            return False

    def stop_process(self, process, child=None):
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=8)
        if child is not None and self.pid_alive(child):
            os.kill(child, signal.SIGKILL)

    def cleanup_evaluators(self):
        path = self.root / "child-pids"
        if not path.exists():
            return
        for value in path.read_text().splitlines():
            pid = int(value)
            try:
                command = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
                # A PID may have been reused; only kill this fixture's executable.
                if os.fsencode(self.binary) in command and self.pid_alive(pid):
                    os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except FileNotFoundError:
                pass

    def wait_marker(self, process):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.marker.exists():
                try:
                    return json.loads(self.marker.read_text())
                except json.JSONDecodeError:
                    pass
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                self.fail(f"controller exited before checkpoint: {stdout!r} {stderr!r}")
            time.sleep(.01)
        self.fail("controller did not reach checkpoint within 5 seconds")

    def run_cli(self, total, out=None, cwd=None):
        result = subprocess.run([sys.executable, runner.__file__, *self.args(total, out)],
                                cwd=cwd, capture_output=True, text=True, timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def test_changed_interval_or_seed_rejected_without_touching_results(self):
        self.assertEqual(runner.main(self.args()), 0)
        before = self.protected_bytes()
        self.assertTrue(any(n.startswith("candidate-trial-") for n in before))
        self.write_plan("1h")
        with self.assertRaisesRegex(RuntimeError, "run contract differs"):
            runner.main(self.args())
        self.assertEqual(self.protected_bytes(), before)
        self.write_plan("4h")
        with self.assertRaisesRegex(RuntimeError, "run contract differs"):
            runner.main(self.args() + ["--seed", "43"])
        self.assertEqual(self.protected_bytes(), before)

    def test_legacy_unbound_journal_is_rejected_without_mutation(self):
        self.out.mkdir()
        storage = self.optuna.storages.JournalStorage(
            self.optuna.storages.journal.JournalFileBackend(str(self.out / "study.journal")))
        old = self.optuna.create_study(study_name="original-online-4h", storage=storage,
                                       directions=["maximize", "maximize"])
        old.optimize(lambda t: (1., 2.), n_trials=1)
        (self.out / "summary.json").write_text('{"legacy":true}')
        (self.out / "candidate-trial-0.json").write_text('{"legacy":true}')
        before = self.protected_bytes()
        with self.assertRaisesRegex(RuntimeError, "no directory contract"):
            runner.main(self.args())
        self.assertEqual(self.protected_bytes(), before)
        self.assertFalse((self.out / runner.CONTRACT_FILE).exists())

    def test_incomplete_tail_backup_preserves_trials_constraints_and_total_budget(self):
        runner.main(self.args())
        original_trials = self.study().trials
        interrupted_trial = self.study().ask()
        runner.sample_config(interrupted_trial, defaults())
        interrupted_params = dict(interrupted_trial.params)
        journal = self.out / "study.journal"
        with journal.open("ab") as output:
            output.write(b'{"op_code":')
        interrupted = journal.read_bytes()
        runner.main(self.args(total=5))
        backups = list(self.out.glob("study.journal.recovery-*.bak"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), interrupted)
        recovered = self.study().trials
        self.assertEqual(len(recovered), 5)
        for before, after in zip(original_trials, recovered):
            self.assertEqual((before.number, before.state, before.params, before.values,
                              before.constraints, before.user_attrs),
                             (after.number, after.state, after.params, after.values,
                              after.constraints, after.user_attrs))
        self.assertEqual(recovered[3].state.name, "FAIL")
        self.assertEqual(recovered[3].params, interrupted_params)
        self.assertEqual(recovered[4].state.name, "COMPLETE")
        self.assertTrue(all(t.constraints for t in recovered if t.state.name == "COMPLETE"))
        completed = journal.read_bytes()
        runner.main(self.args(total=5))
        self.assertEqual(journal.read_bytes(), completed)
        self.assertEqual(json.loads((self.out / "summary.json").read_text())["trial_count"], 5)
        self.assertEqual(len(list(self.out.glob("study.journal.recovery-*.bak"))), 1)

    def test_complete_corrupt_record_rejected_without_modifying_original(self):
        runner.main(self.args())
        with (self.out / "study.journal").open("ab") as output:
            output.write(b'{invalid-json}\n')
        before = self.protected_bytes()
        with self.assertRaisesRegex(RuntimeError, "corrupt complete journal record"):
            runner.main(self.args(total=4))
        self.assertEqual(self.protected_bytes(), before)
        self.assertEqual(list(self.out.glob("*.bak")), [])

    def test_failed_backup_does_not_truncate_original_or_replace_results(self):
        runner.main(self.args())
        with (self.out / "study.journal").open("ab") as output:
            output.write(b'{"op_code":')
        before = self.protected_bytes()
        with mock.patch.object(runner.shutil, "copyfileobj", side_effect=OSError("backup write failed")):
            with self.assertRaisesRegex(OSError, "backup write failed"):
                runner.main(self.args(total=4))
        self.assertEqual(self.protected_bytes(), before)
        self.assertEqual(list(self.out.glob("*.bak")), [])
        runner.main(self.args(total=4))
        backup = list(self.out.glob("study.journal.recovery-*.bak"))
        self.assertEqual(len(backup), 1)
        self.assertEqual(backup[0].read_bytes(), before["study.journal"])
        self.assertEqual(len(self.study().trials), 4)

    def test_unknown_lock_blocks_recovery_without_mutation(self):
        runner.main(self.args())
        journal = self.out / "study.journal"
        with journal.open("ab") as output:
            output.write(b'{"op_code":')
        lock = journal.with_name(journal.name + ".lock")
        lock.write_bytes(b"not an owned symlink lock")
        before = self.protected_bytes()
        with self.assertRaisesRegex(RuntimeError, "unexpected journal lock"):
            runner.main(self.args(total=4))
        self.assertEqual(self.protected_bytes(), before)
        self.assertEqual(lock.read_bytes(), b"not an owned symlink lock")
        self.assertEqual(list(self.out.glob("*.bak")), [])

    def test_relative_out_uses_absolute_backend_and_recovers_killed_lock_owner(self):
        relative_out = Path("nested/run")
        absolute_out = self.root / relative_out
        backend = self.optuna.storages.journal.JournalFileBackend
        seen = []
        def capture(path, *args, **kwargs):
            seen.append(Path(path))
            return backend(path, *args, **kwargs)
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            with mock.patch.object(self.optuna.storages.journal, "JournalFileBackend", capture):
                runner.main(self.args(total=1, out=relative_out))
        finally:
            os.chdir(previous)
        self.assertEqual(seen, [absolute_out / "study.journal"])
        journal = absolute_out / "study.journal"
        owner_code = f'''import optuna, time
b = optuna.storages.journal.JournalFileBackend({str(journal)!r})
b._lock.acquire()
print('held', flush=True)
time.sleep(60)
'''
        owner = subprocess.Popen([sys.executable, "-c", owner_code], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.stop_process, owner)
        # Lock creation is a bounded checkpoint even if the owner fails to acquire.
        lock = journal.with_name(journal.name + ".lock")
        deadline = time.monotonic() + 5
        while not os.path.lexists(lock) and time.monotonic() < deadline:
            self.assertIsNone(owner.poll())
            time.sleep(.01)
        self.assertTrue(os.path.lexists(lock))
        self.assertEqual(Path(os.readlink(lock)), journal)
        owner.kill()
        owner.communicate(timeout=5)
        self.run_cli(total=2, out=relative_out, cwd=self.root)
        self.assertFalse(os.path.lexists(lock))
        self.assertEqual(len(self.study(absolute_out).trials), 2)
        # Reproduce a lock generated by the old relative-path backend.
        lock.symlink_to(relative_out / "study.journal")
        self.assertTrue(os.path.lexists(lock))
        self.assertFalse(lock.exists())
        self.run_cli(total=3, out=relative_out, cwd=self.root)
        self.assertFalse(os.path.lexists(lock))
        self.assertEqual(len(self.study(absolute_out).trials), 3)

    def test_sigterm_ready_and_evaluation_clean_child_and_allow_budgeted_resume(self):
        for phase in ("ready", "evaluation"):
            with self.subTest(phase=phase):
                out = self.root / ("signal-" + phase)
                self.marker.unlink(missing_ok=True)
                self.control.write_text(json.dumps(dict(phase=phase)))
                process = subprocess.Popen([sys.executable, runner.__file__,
                                            *self.args(total=3, out=out)],
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                child = None
                try:
                    marker = self.wait_marker(process)
                    child = marker["pid"]
                    self.assertEqual(marker["phase"], phase)
                    self.assertTrue(self.pid_alive(child))
                    process.send_signal(signal.SIGTERM)
                    stdout, stderr = process.communicate(timeout=8)
                    self.assertEqual(process.returncode, 143, (stdout, stderr))
                    self.assertFalse(self.pid_alive(child))
                finally:
                    self.stop_process(process, child)
                self.control.write_text("{}")
                if phase == "evaluation":
                    interrupted = json.loads((out / "summary.json").read_text())
                    self.assertEqual(interrupted["trial_states"], {"FAIL": 1})
                self.run_cli(total=3, out=out)
                summary = json.loads((out / "summary.json").read_text())
                self.assertEqual(summary["trial_count"], 3)
                self.assertEqual(summary["trial_states"],
                                 {"FAIL": 1, "COMPLETE": 2} if phase == "evaluation"
                                 else {"COMPLETE": 3})
                journal = out / "study.journal"
                before = journal.read_bytes()
                self.run_cli(total=3, out=out)
                self.assertEqual(journal.read_bytes(), before)

    def test_sigterm_handler_restored_after_normal_and_deferred_interruption(self):
        previous = signal.getsignal(signal.SIGTERM)
        with runner.controlled_termination():
            self.assertNotEqual(signal.getsignal(signal.SIGTERM), previous)
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)
        with self.assertRaises(runner.TerminationRequested):
            with runner.controlled_termination() as guard:
                with guard.defer():
                    guard.handle(signal.SIGTERM, None)
                    self.assertTrue(guard.pending)
                    self.assertEqual(signal.getsignal(signal.SIGTERM), guard.handle)
                    # Deferred ownership acquisition may still spawn a child. It
                    # must inherit ordinary TERM handling, rather than SIG_IGN.
                    child = subprocess.Popen([sys.executable, "-c",
                        "import signal,sys; sys.exit(0 if signal.getsignal(signal.SIGTERM) "
                        "== signal.SIG_DFL else 1)"], stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE)
                    self.addCleanup(self.stop_process, child)
                    child.communicate(timeout=3)
                    self.assertEqual(child.returncode, 0)
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)


if __name__ == "__main__":
    unittest.main()

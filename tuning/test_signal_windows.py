"""Precise TERM interleavings at evaluator ownership and normal cleanup."""
import fcntl
import os
from pathlib import Path
import signal
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import optimize as runner


class SignalWindowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="lorentz-signal-windows-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.binary = self.root / "evaluator"
        self.binary.write_text(
            "#!" + sys.executable + "\n"
            "import json, time\n"
            "print(json.dumps({'type': 'ready', 'protocol_version': 1}), flush=True)\n"
            "time.sleep(60)\n",
            encoding="utf-8",
        )
        self.binary.chmod(0o700)

    def assert_closed(self, server):
        self.assertIsNotNone(server.process.poll(), "evaluator outlived controller cleanup")
        self.assertFalse(server.thread.is_alive())
        for pipe in (server.process.stdin, server.process.stdout, server.process.stderr):
            self.assertTrue(pipe.closed)

    def test_term_after_construction_before_controller_assignment(self):
        servers = []
        original = runner.GoServer

        def construct_and_interrupt(*args, **kwargs):
            server = original(*args, **kwargs)
            servers.append(server)
            # Ensure failed assertions cannot leave a subprocess behind.
            self.addCleanup(server._close)
            # The real child exists, but run_controller has not stored its owner.
            os.kill(os.getpid(), signal.SIGTERM)
            return server

        args = SimpleNamespace(binary=self.binary, plan=self.root / "plan.json",
                               out=self.root / "out", eval_timeout=5)
        with runner.controlled_termination() as guard:
            with mock.patch.object(runner, "GoServer", side_effect=construct_and_interrupt):
                with self.assertRaises(runner.TerminationRequested):
                    runner.run_controller(args, None, None, guard)
        self.assertEqual(len(servers), 1)
        self.assert_closed(servers[0])
        with (args.out / "run.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_first_term_at_normal_close_entry(self):
        with runner.controlled_termination() as guard:
            server = runner.GoServer(self.binary, self.root / "plan.json", termination=guard)
            self.addCleanup(server._close)
            injected = []

            def inject_at_entry(frame, event, arg):
                if frame.f_code is runner.GoServer.close.__code__ and event == "line" and not injected:
                    injected.append(frame.f_lineno)
                    # Inject at the first body line, before cleanup deferral enters.
                    os.kill(os.getpid(), signal.SIGTERM)
                return inject_at_entry

            previous_trace = sys.gettrace()
            sys.settrace(inject_at_entry)
            try:
                with self.assertRaises(runner.TerminationRequested):
                    server.close()
            finally:
                sys.settrace(previous_trace)
            self.assertEqual(len(injected), 1)
            self.assert_closed(server)


if __name__ == "__main__":
    unittest.main()

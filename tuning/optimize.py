#!/usr/bin/env python3
"""Serial Optuna controller for the Go original-online evaluator.

This bounded research space is not the complete set of legal Go settings.
Trial budget includes failed trials. A completed budget is not a profit claim.
"""
from __future__ import annotations

import argparse
import collections
from contextlib import contextmanager, nullcontext
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import shutil
import sys
import subprocess
import threading
import time
import uuid

SPACE_VERSION = 1
SEED_POLICY = "trial-number-addition-uint32-v1"
OBJECTIVES = ["net_return_pct", "win_rate_pct"]
STUDY_NAME = "original-online"
CONTRACT_FILE = "run-contract.json"


class EvaluationError(Exception):
    """An individual evaluation failed; count it and continue."""


class ProtocolError(RuntimeError):
    pass


class TerminationRequested(KeyboardInterrupt):
    """SIGTERM follows the same unwinding path as an interactive interrupt."""


class TerminationGuard:
    def __init__(self):
        self.deferred = 0
        self.pending = False

    def handle(self, signum, frame):
        if self.deferred or self.in_cleanup(frame):
            self.pending = True
        else:
            # Do not pass an ignored TERM disposition to a child being spawned.
            # Once unwinding starts, repeated TERM must not interrupt cleanup.
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            raise TerminationRequested("stopped by SIGTERM")

    def in_cleanup(self, frame):
        # A signal can arrive at close()'s entry before its context manager has
        # entered. Defer from that entry too, without running cleanup reentrantly
        # inside a signal handler (Popen.wait holds a non-reentrant lock).
        while frame is not None:
            if (frame.f_code is GoServer.close.__code__ and
                    getattr(frame.f_locals.get("self"), "termination", None) is self):
                return True
            frame = frame.f_back
        return False

    @contextmanager
    def defer(self):
        self.deferred += 1
        try:
            yield
        finally:
            self.deferred -= 1
            if not self.deferred and self.pending:
                self.pending = False
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                raise TerminationRequested("stopped by SIGTERM")


@contextmanager
def controlled_termination():
    guard = TerminationGuard()
    if threading.current_thread() is not threading.main_thread():
        # Python only permits signal registration on the main thread.
        yield None
        return
    previous = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, guard.handle)
    try:
        yield guard
    finally:
        signal.signal(signal.SIGTERM, previous)


def sample_config(trial, defaults):
    c = copy.deepcopy(defaults)
    c.update(algorithm="original-online", min_vote_fraction=0, sample_stride=4)
    c["include_full_history"] = trial.suggest_categorical("include_full_history", [False, True])
    c["source"] = trial.suggest_categorical("source", ["close", "hlc3", "ohlc4"])
    c["neighbors"] = trial.suggest_int("neighbors", 1, 100)
    c["max_bars_back"] = trial.suggest_categorical("max_bars_back", [500, 1000, 2000, 4000])
    c["feature_count"] = trial.suggest_int("feature_count", 2, 5)
    domains = {"RSI": (5, 40, 1, 8), "WT": (4, 30, 3, 30), "CCI": (8, 50, 1, 8), "ADX": (5, 40, 1, 1)}
    for i in range(c["feature_count"]):
        prefix = f"feature_{i + 1}"
        name = trial.suggest_categorical(prefix + "_name", list(domains))
        al, ah, bl, bh = domains[name]
        c["features"][i] = {"name": name, "a": trial.suggest_int(prefix + "_" + name + "_a", al, ah),
                             "b": 1 if name == "ADX" else trial.suggest_int(prefix + "_" + name + "_b", bl, bh)}
    for key in ("use_volatility_filter", "use_regime_filter", "use_adx_filter"):
        c[key] = trial.suggest_categorical(key, [False, True])
    if c["use_regime_filter"]:
        c["regime_threshold"] = trial.suggest_float("regime_threshold", -.5, 1., step=.1)
    if c["use_adx_filter"]:
        c["adx_threshold"] = trial.suggest_int("adx_threshold", 10, 40)
    c["use_dynamic_exits"] = trial.suggest_categorical("exit_mode", ["fixed", "dynamic"]) == "dynamic"
    if c["use_dynamic_exits"]:
        c.update(use_ema_filter=False, use_sma_filter=False, kernel_smoothing=False)
    else:
        for kind in ("ema", "sma"):
            c[f"use_{kind}_filter"] = trial.suggest_categorical(f"use_{kind}_filter", [False, True])
            if c[f"use_{kind}_filter"]:
                c[f"{kind}_period"] = trial.suggest_int(f"{kind}_period", 20, 300, step=10)
    c["use_kernel_filter"] = trial.suggest_categorical("use_kernel_filter", [False, True])
    if c["use_kernel_filter"] or c["use_dynamic_exits"]:
        c["kernel_h"] = trial.suggest_int("kernel_h", 3, 30)
        c["kernel_r"] = trial.suggest_float("kernel_r", .25, 16., step=.25)
        c["kernel_x"] = trial.suggest_int("kernel_x", 2, 40)
    c["kernel_smoothing"] = False
    c["kernel_lag"] = 2
    if c["use_kernel_filter"] and not c["use_dynamic_exits"]:
        c["kernel_smoothing"] = trial.suggest_categorical("kernel_smoothing", [False, True])
        if c["kernel_smoothing"]:
            c["kernel_lag"] = trial.suggest_int("kernel_lag", 1, 2)
    return c


class DefaultsTrial:
    def __init__(self, defaults):
        self.defaults = defaults
        self.params = {}

    def value(self, name):
        if name == "exit_mode":
            return "dynamic" if self.defaults["use_dynamic_exits"] else "fixed"
        if name.startswith("feature_") and name != "feature_count":
            _, slot, *parts = name.split("_")
            return self.defaults["features"][int(slot) - 1][parts[-1]]
        return self.defaults[name]

    def suggest_categorical(self, name, choices):
        value = self.value(name)
        if value not in choices:
            raise ValueError(f"default {name} outside search space")
        self.params[name] = value
        return value

    def suggest_int(self, name, low, high, step=1):
        value = self.value(name)
        if not low <= value <= high or (value - low) % step:
            raise ValueError(f"default {name} outside search space")
        self.params[name] = value
        return value

    def suggest_float(self, name, low, high, step=None):
        value = self.value(name)
        if not low - 1e-9 <= value <= high + 1e-9:
            raise ValueError(f"default {name} outside search space")
        self.params[name] = value
        return value


def default_parameters(defaults):
    trial = DefaultsTrial(defaults)
    sample_config(trial, defaults)
    return trial.params


def thresholds(plan):
    return int(plan["min_trades"]), int(plan["min_positive_folds"])


def constraint_values(result, baseline, plan):
    a, b = result["aggregate"], baseline["aggregate"]
    trades, folds = thresholds(plan)
    # Strict comparisons are independently enforced when exporting candidates.
    return {"min_trades": trades - a["trades"], "min_positive_folds": folds - a["positive_folds"],
            "positive_net_return": 1e-12 - a["net_return_pct"],
            "baseline_net_return": b["net_return_pct"] + 1e-12 - a["net_return_pct"],
            "baseline_win_rate": b["win_rate_pct"] + 1e-12 - a["win_rate_pct"]}


def eligible(result, baseline, plan):
    a, b = result["aggregate"], baseline["aggregate"]
    trades, folds = thresholds(plan)
    return (a["trades"] >= trades and a["positive_folds"] >= folds and
            a["net_return_pct"] > 0 and a["net_return_pct"] > b["net_return_pct"] and
            a["win_rate_pct"] > b["win_rate_pct"])


def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def make_contract(args, ready):
    return {"directory_schema_version": 1, "protocol_version": 1, "space_version": SPACE_VERSION, "sampler": args.sampler,
            "seed": args.seed, "seed_policy": SEED_POLICY, "startup_trials": args.startup_trials, "script_sha256": digest(__file__),
            "binary_sha256": digest(args.binary), "data_identity": ready["data_identity"],
            "plan": ready["plan"], "objectives": OBJECTIVES, "eval_timeout": args.eval_timeout}


def verify_contract(existing, proposed):
    if existing != proposed:
        raise RuntimeError("run contract differs: use a new output directory; changed data, code, plan or sampler cannot silently resume")


def remaining_budget(trials, total):
    return max(0, total - sum(getattr(t.state, "name", t.state) != "WAITING" for t in trials))


def atomic_json(path, value):
    temp = path.with_name(path.name + ".tmp")
    with open(temp, "w", encoding="utf-8") as output:
        json.dump(value, output, ensure_ascii=False, indent=2, allow_nan=False)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temp, path)
    sync_directory(path.parent)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def bind_directory(out, contract):
    """Check identity before opening a backend that can append journal records."""
    path = out / CONTRACT_FILE
    if path.exists():
        verify_contract(json.loads(path.read_text()), contract)
        return
    if ((out / "study.journal").exists() or (out / "summary.json").exists() or
            any(out.glob("candidate-trial-*.json"))):
        raise RuntimeError("existing output has no directory contract; use a new output directory")
    atomic_json(path, contract)


def prepare_journal(path):
    """Called under run.lock: recover only an uncommitted final record.

    A complete malformed record is corruption and remains untouched. Preserve
    and fsync the entire original before truncating a tail without a newline.
    The serial controller owns all backend writes, so a backend lock left after
    acquiring run.lock has no live controller owner.
    """
    lock = path.with_name(path.name + ".lock")
    if os.path.lexists(lock):
        if not lock.is_symlink() or Path(os.readlink(lock)).name != path.name:
            raise RuntimeError("unexpected journal lock; left untouched")
    if path.exists():
        committed = 0
        tail = False
        with path.open("rb") as source:
            for line in source:
                if not line.endswith(b"\n"):
                    tail = True
                    break
                try:
                    record = json.loads(line)
                    if not isinstance(record, dict) or type(record.get("op_code")) is not int:
                        raise ValueError("not an Optuna journal record")
                except (ValueError, UnicodeError) as exc:
                    raise RuntimeError("corrupt complete journal record; original left untouched") from exc
                committed += len(line)
        if tail:
            backup = path.with_name(path.name + ".recovery-" + uuid.uuid4().hex + ".bak")
            temporary = backup.with_name(backup.name + ".tmp")
            with path.open("rb") as source, temporary.open("xb") as dest:
                shutil.copyfileobj(source, dest)
                dest.flush()
                os.fsync(dest.fileno())
            os.replace(temporary, backup)
            sync_directory(path.parent)
            with path.open("r+b") as journal:
                journal.truncate(committed)
                journal.flush()
                os.fsync(journal.fileno())
            print(f"Recovered uncommitted journal tail; original preserved at {backup}", file=sys.stderr)
    if os.path.lexists(lock):
        lock.unlink()
        sync_directory(path.parent)


class GoServer:
    def __init__(self, binary, plan, timeout=600, termination=None, await_ready=True):
        self.timeout = timeout
        self.termination = termination
        self.buffer = b""
        self.stderr = collections.deque(maxlen=100)
        self.process = None
        self.selector = None
        self.thread = None
        self.next_id = 0
        try:
            # Defer TERM until ownership is recorded; child signal masks remain
            # unchanged, so terminate() also works while waiting for ready.
            with termination.defer() if termination else nullcontext():
                self.process = subprocess.Popen([str(binary), "optimize-eval", "-plan", str(plan)],
                                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.selector = selectors.DefaultSelector()
                self.selector.register(self.process.stdout, selectors.EVENT_READ)
                self.thread = threading.Thread(target=self._drain_stderr, daemon=True)
                self.thread.start()
            if await_ready:
                self.read_ready()
        except BaseException:
            self.close()
            raise

    def read_ready(self):
        self.ready = self._read()
        if self.ready.get("type") != "ready" or self.ready.get("protocol_version") != 1:
            raise ProtocolError(f"invalid ready response: {self.ready}")

    def _drain_stderr(self):
        for line in iter(self.process.stderr.readline, b""):
            self.stderr.append(line.decode("utf-8", errors="replace").rstrip())

    def _read(self):
        deadline = time.monotonic() + self.timeout
        while b"\n" not in self.buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProtocolError("Go response timeout: " + "\n".join(self.stderr))
            if not self.selector.select(remaining):
                continue
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise ProtocolError("Go exited before response: " + "\n".join(self.stderr))
            self.buffer += chunk
            if len(self.buffer) > 16 * 1024 * 1024:
                raise ProtocolError("oversized Go response")
        line, self.buffer = self.buffer.split(b"\n", 1)
        try:
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("response must be an object")
            return value
        except (ValueError, UnicodeError) as exc:
            raise ProtocolError("invalid Go JSON response") from exc

    def evaluate(self, config):
        self.next_id += 1
        request = {"id": self.next_id, "config": config}
        try:
            self.process.stdin.write((json.dumps(request, allow_nan=False) + "\n").encode())
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise ProtocolError("Go request pipe failed") from exc
        result = self._read()
        if result.get("id") != self.next_id:
            raise ProtocolError("Go response ID mismatch")
        if result.get("type") == "error":
            if result.get("error_kind") == "evaluation":
                raise EvaluationError(result.get("error", "evaluation failed"))
            raise ProtocolError(str(result))
        if result.get("type") != "result":
            raise ProtocolError("unexpected Go response type")
        if result.get("config") != config:
            raise ProtocolError("Go response config mismatch")
        return result

    def close(self):
        with self.termination.defer() if self.termination else nullcontext():
            self._close()

    def _close(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        if self.thread is not None and self.thread.ident is not None:
            self.thread.join(timeout=2)
        if self.selector is not None:
            self.selector.close()
        if self.process is not None:
            for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
                if pipe is not None:
                    pipe.close()



def make_sampler(seed, startup_trials, mode):
    """Seed each serial trial independently of controller process lifetime.

    Each TPE delegate reconstructs its search space from persisted study history.
    This makes an uninterrupted run and a resumed run use the same per-trial RNG.
    A failed interrupted trial still consumes its number and its random seed.
    """
    import optuna

    class TrialNumberSampler(optuna.samplers.BaseSampler):
        def __init__(self):
            self.delegates = {}

        def delegate(self, trial):
            if trial.number not in self.delegates:
                self.delegates[trial.number] = optuna.samplers.TPESampler(
                    seed=(seed + trial.number) % (2 ** 32), n_startup_trials=startup_trials,
                    multivariate=mode == "grouped", group=mode == "grouped")
            return self.delegates[trial.number]

        def before_trial(self, study, trial):
            self.delegate(trial).before_trial(study, trial)

        def infer_relative_search_space(self, study, trial):
            return self.delegate(trial).infer_relative_search_space(study, trial)

        def sample_relative(self, study, trial, search_space):
            return self.delegate(trial).sample_relative(study, trial, search_space)

        def sample_independent(self, study, trial, param_name, param_distribution):
            return self.delegate(trial).sample_independent(study, trial, param_name, param_distribution)

        def after_trial(self, study, trial, state, values):
            try:
                self.delegate(trial).after_trial(study, trial, state, values)
            finally:
                self.delegates.pop(trial.number, None)

    return TrialNumberSampler()

def recover_running(study, optuna):
    for trial in study.get_trials(deepcopy=False):
        if trial.state == optuna.trial.TrialState.RUNNING:
            study.tell(trial.number, state=optuna.trial.TrialState.FAIL)


def save_outputs(out, study, baseline, ready, contract):
    trials = study.get_trials(deepcopy=False)
    candidates = [t for t in trials if t.state.name == "COMPLETE" and
                  eligible(t.user_attrs["response"], baseline, ready["plan"])]
    pareto = [t for t in candidates if not any(
        all(u.values[i] >= t.values[i] for i in range(2)) and any(u.values[i] > t.values[i] for i in range(2))
        for u in candidates)]
    pareto.sort(key=lambda t: (-t.values[0], -t.values[1], t.number))
    exports = []
    for trial in pareto:
        filename = f"candidate-trial-{trial.number}.json"
        atomic_json(out / filename, trial.user_attrs["response"]["config"])
        exports.append({"trial": trial.number, "config_file": filename, "response": trial.user_attrs["response"]})
    # Retain prior exports; only the atomically published summary selects candidates.
    states = collections.Counter(t.state.name for t in trials)
    atomic_json(out / "summary.json", {"method": "original-online bounded Optuna multiobjective research",
                "plan": ready["plan"], "data_metadata": ready.get("metadata", {}), "contract": contract, "objectives": OBJECTIVES,
                "trial_states": dict(states), "trial_count": len(trials), "baseline": baseline,
                "eligible_trial_count": len(candidates), "eligible_pareto_candidates": exports,
                "candidate_status": "eligible candidates found" if exports else "no eligible candidate; no upgrade established"})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--binary", default=Path("bin/lorentz"), type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--trials", default=100, type=int, help="TOTAL budget, including failed trials")
    parser.add_argument("--eval-timeout", default=600., type=float)
    parser.add_argument("--seed", default=42, type=int)
    parser.add_argument("--startup-trials", default=32, type=int)
    parser.add_argument("--sampler", choices=["independent", "grouped"], default="independent")
    args = parser.parse_args(argv)
    if not 0 <= args.seed < 2 ** 32:
        parser.error("seed must be a uint32 integer (0..4294967295)")
    if args.eval_timeout <= 0:
        parser.error("eval-timeout must be positive")
    if args.trials < 0 or args.startup_trials < 0:
        parser.error("trial counts must be nonnegative")
    import optuna
    import numpy
    with controlled_termination() as termination:
        return run_controller(args, optuna, numpy, termination)


def run_controller(args, optuna, numpy, termination):
    args.binary = args.binary.resolve()
    args.plan = args.plan.resolve()
    args.out = args.out.resolve()
    args.out.mkdir(parents=True, exist_ok=True)
    with open(args.out / "run.lock", "a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("output directory already has an active controller") from exc
        server = None
        try:
            with termination.defer() if termination else nullcontext():
                server = GoServer(args.binary, args.plan, timeout=args.eval_timeout, termination=termination,
                                  await_ready=False)
            server.read_ready()
            ready = server.ready
            contract = make_contract(args, ready)
            contract["optuna_version"] = optuna.__version__
            contract["numpy_version"] = numpy.__version__
            contract["python_version"] = sys.version
            if not hasattr(optuna.trial.Trial, "set_constraint"):
                raise RuntimeError("Optuna 5.x with Trial.set_constraint is required")
            bind_directory(args.out, contract)
            journal = args.out / "study.journal"
            prepare_journal(journal)
            sampler = make_sampler(args.seed, args.startup_trials, args.sampler)
            storage = optuna.storages.JournalStorage(optuna.storages.journal.JournalFileBackend(str(journal)))
            names = optuna.get_all_study_names(storage=storage)
            if names:
                if names != [STUDY_NAME]:
                    raise RuntimeError("output directory must contain exactly its bound study")
                study = optuna.load_study(study_name=STUDY_NAME, storage=storage, sampler=sampler)
            else:
                study = optuna.create_study(study_name=STUDY_NAME, storage=storage, sampler=sampler,
                                           directions=["maximize", "maximize"])
            existing = study.user_attrs.get("run_contract")
            if existing is None:
                if study.trials:
                    raise RuntimeError("existing study has trials without a run contract")
                study.set_user_attr("run_contract", contract)
            else:
                verify_contract(existing, contract)
            recover_running(study, optuna)
            baseline = study.user_attrs.get("baseline")
            if baseline is None:
                baseline = server.evaluate(ready["default_config"])
                study.set_user_attr("baseline", baseline)
            if not study.trials and args.trials:
                study.enqueue_trial(default_parameters(ready["default_config"]))
            def objective(trial):
                config = sample_config(trial, ready["default_config"])
                response = server.evaluate(config)
                trial.set_user_attr("response", response)
                constraints = constraint_values(response, baseline, ready["plan"])
                trial.set_user_attr("constraints", constraints)
                for name, value in constraints.items():
                    trial.set_constraint(name, float(value))
                return tuple(response["aggregate"][key] for key in OBJECTIVES)
            # WAITING trials have not consumed an evaluation attempt.
            budget = remaining_budget(study.trials, args.trials)
            try:
                if budget:
                    study.optimize(objective, n_trials=budget, catch=(EvaluationError,))
            finally:
                save_outputs(args.out, study, baseline, ready, contract)
            print(f"Completed {len(study.trials)} stored trials. See {args.out / 'summary.json'}")
        finally:
            if server is not None:
                server.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except TerminationRequested:
        raise SystemExit(128 + signal.SIGTERM)

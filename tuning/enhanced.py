#!/usr/bin/env python3
"""Finite, frozen matrix runner; no sampling or Optuna. Failures consume budget."""
from __future__ import annotations
import argparse
import collections
from contextlib import nullcontext
import fcntl
import json
import math
from pathlib import Path
import selectors
import subprocess
import threading
import optimize as legacy

class EnhancedServer(legacy.GoServer):
    def __init__(self, binary, plan, timeout=600, termination=None, await_ready=True):
        self.timeout, self.termination = timeout, termination
        self.buffer = b""
        self.stderr = collections.deque(maxlen=100)
        self.process = self.selector = self.thread = None
        self.next_id = 0
        try:
            with termination.defer() if termination else nullcontext():
                self.process = subprocess.Popen([str(binary), "enhanced-eval", "-plan", str(plan)],
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
        if self.ready.get("type") != "ready" or self.ready.get("protocol_version") != 2:
            raise legacy.ProtocolError("invalid enhanced ready")

def strict_loads(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key: " + key)
            result[key] = value
        return result
    def constant(value):
        raise ValueError("nonfinite JSON: " + value)
    def number(value):
        result=float(value)
        if not math.isfinite(result):
            raise ValueError("nonfinite JSON number")
        return result
    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant, parse_float=number)

def load_matrix(path, budget):
    matrix = strict_loads(path.read_text())
    if not isinstance(matrix, list) or not matrix or len(matrix) > budget:
        raise ValueError("matrix exceeds authorized configuration budget or is empty")
    names = set()
    for entry in matrix:
        if set(entry) != {"name", "plan", "config"} or not isinstance(entry["name"], str) or not entry["name"] or entry["name"] in names:
            raise ValueError("matrix entries need unique names, plan and full config")
        names.add(entry["name"])
    return matrix

def run(args):
    matrix = load_matrix(args.matrix, args.budget)
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    servers = {}
    with (out / "run.lock").open("a+") as lock, legacy.controlled_termination() as termination:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            for entry in matrix:
                plan = str(Path(entry["plan"]).resolve())
                if plan not in servers:
                    with termination.defer() if termination else nullcontext():
                        servers[plan] = EnhancedServer(args.binary.resolve(), plan, args.timeout, termination, await_ready=False)
                    servers[plan].read_ready()
            contract = {"directory_schema_version": 2, "protocol_version": 2, "mode": "frozen-matrix",
                "budget": args.budget, "timeout": args.timeout, "matrix": matrix,
                "script_sha256": legacy.digest(__file__), "helpers_sha256": legacy.digest(legacy.__file__),
                "binary_sha256": legacy.digest(args.binary),
                "datasets": {p: s.ready for p, s in servers.items()}}
            contract_path = out / "run-contract.json"
            state_path = out / "summary.json"
            if contract_path.exists():
                legacy.verify_contract(strict_loads(contract_path.read_text()), contract)
            else:
                if any(p.name != "run.lock" for p in out.iterdir()):
                    raise RuntimeError("nonempty output has no matching matrix contract")
                legacy.atomic_json(contract_path, contract)
            state = strict_loads(state_path.read_text()) if state_path.exists() else []
            if not isinstance(state, list) or len(state) > len(matrix):
                raise RuntimeError("invalid matrix state; preserve and inspect")
            for i, record in enumerate(state):
                if record.get("name") != matrix[i]["name"] or record.get("status") not in {"RUNNING", "COMPLETE", "FAIL"}:
                    raise RuntimeError("matrix state identity/status differs")
                if record["status"] == "RUNNING":
                    record.update(status="FAIL", error="interrupted evaluation consumed budget; no automatic retry")
            legacy.atomic_json(state_path, state)
            for entry in matrix[len(state):]:
                record = {"name": entry["name"], "status": "RUNNING"}
                state.append(record)
                legacy.atomic_json(state_path, state)  # Count before external evaluation.
                try:
                    server = servers[str(Path(entry["plan"]).resolve())]
                    record["result"] = server.evaluate(entry["config"])
                    record["status"] = "COMPLETE"
                except Exception as exc:
                    record.update(status="FAIL", error=str(exc))
                    legacy.atomic_json(state_path, state)
                    raise  # broken protocol cannot safely continue on this child
                legacy.atomic_json(state_path, state)
                print(entry["name"], record["status"], record["result"]["aggregate"], flush=True)
        finally:
            with termination.defer() if termination else nullcontext():
                for server in servers.values():
                    server.close()
    return state

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--matrix", type=Path, required=True)
    p.add_argument("--budget", type=int, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--binary", type=Path, default=Path("bin/lorentz-upgrade"))
    p.add_argument("--timeout", type=int, default=600)
    args = p.parse_args()
    if args.budget < 1 or args.timeout < 1:
        p.error("positive authorized budget and timeout required")
    run(args)
if __name__ == "__main__":
    main()

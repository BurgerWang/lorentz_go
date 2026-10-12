#!/usr/bin/env python3
"""One frozen walk-forward batch; every reserved configuration consumes budget."""
from __future__ import annotations

import argparse
from contextlib import nullcontext
import fcntl
from pathlib import Path
import subprocess
import time

from enhanced import strict_loads
import enhanced
import optimize as legacy


def positive_int(value):
    return type(value) is int and value > 0


def child_json(command, timeout, termination):
    """Own every spawned child before TERM can unwind; bound waits and cleanup."""
    process = None
    try:
        with termination.defer() if termination else nullcontext():
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise legacy.ProtocolError("walk-forward child timeout")
            try:
                stdout, stderr = process.communicate(timeout=min(remaining, 0.1))
                break
            except subprocess.TimeoutExpired:
                continue
        if process.returncode != 0:
            raise legacy.ProtocolError("walk-forward child failed: " +
                                       stderr.decode("utf-8", errors="replace")[-4000:])
        try:
            value = strict_loads(stdout.decode("utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise legacy.ProtocolError("invalid walk-forward JSON") from exc
        if not isinstance(value, dict):
            raise legacy.ProtocolError("walk-forward response must be an object")
        return value
    finally:
        with termination.defer() if termination else nullcontext():
            if process is not None:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate(timeout=5)
                for pipe in (process.stdout, process.stderr):
                    if pipe is not None:
                        pipe.close()


def validate_ready(ready, budget):
    required = {"type", "protocol_version", "plan", "required_evaluations",
                "data_identity", "daily_identity", "exposure_identity"}
    if (not required <= ready.keys() or ready["type"] != "ready" or
            type(ready["protocol_version"]) is not int or ready["protocol_version"] != 1 or
            not isinstance(ready["plan"], dict) or
            not positive_int(ready["required_evaluations"]) or
            not positive_int(ready["plan"].get("budget"))):
        raise legacy.ProtocolError("invalid walk-forward inspect response")
    if budget != ready["required_evaluations"] or budget != ready["plan"]["budget"]:
        raise ValueError("authorized budget must equal all required configuration evaluations")


def validate_state(state, budget, ready):
    if (not isinstance(state, dict) or state.get("status") not in {"RUNNING", "COMPLETE", "FAIL"} or
            type(state.get("reserved_evaluations")) is not int or
            state["reserved_evaluations"] != budget):
        raise RuntimeError("invalid walk-forward state; preserve and inspect")
    if state["status"] == "COMPLETE":
        validate_result(state.get("result"), budget, ready)


def validate_result(result, budget, ready):
    if (not isinstance(result, dict) or result.get("type") != "result" or
            type(result.get("evaluations_used")) is not int or result["evaluations_used"] != budget):
        raise legacy.ProtocolError("walk-forward result evaluation count/type mismatch")
    if (type(result.get("protocol_version")) is not int or result["protocol_version"] != 1 or
            any(key not in result or result[key] != ready[key]
                for key in ("plan", "data_identity", "daily_identity", "exposure_identity"))):
        raise legacy.ProtocolError("walk-forward result plan/protocol/data identity differs from inspect contract")


def run(args):
    if not positive_int(args.budget) or not positive_int(args.timeout):
        raise ValueError("positive authorized budget and timeout required")
    plan, binary, out = args.plan.resolve(), args.binary.resolve(), args.out.resolve()
    # Validate strict source JSON before any inspect/evaluation process.
    source_plan = strict_loads(plan.read_text(encoding="utf-8"))
    if not isinstance(source_plan, dict):
        raise ValueError("walk-forward plan must be an object")
    out.mkdir(parents=True, exist_ok=True)
    with (out / "run.lock").open("a+") as lock, legacy.controlled_termination() as termination:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        identities = {"plan_sha256": legacy.digest(plan), "binary_sha256": legacy.digest(binary),
                      "script_sha256": legacy.digest(__file__),
                      "strict_helpers_sha256": legacy.digest(enhanced.__file__),
                      "helpers_sha256": legacy.digest(legacy.__file__)}
        def unchanged():
            if legacy.digest(plan) != identities["plan_sha256"] or legacy.digest(binary) != identities["binary_sha256"]:
                raise RuntimeError("plan or binary changed during this run; preserve output and use a new directory")
        command = [str(binary), "walkforward-eval", "-plan", str(plan)]
        ready = child_json(command + ["-inspect"], args.timeout, termination)
        unchanged()
        validate_ready(ready, args.budget)
        contract = {"directory_schema_version": 1, "mode": "frozen-walkforward",
                    "budget": args.budget, "timeout": args.timeout,
                    "plan_path": str(plan), "binary_path": str(binary),
                    "inspect": ready, **identities}
        contract_path, state_path = out / "run-contract.json", out / "summary.json"
        fresh = not contract_path.exists()
        if contract_path.exists():
            legacy.verify_contract(strict_loads(contract_path.read_text()), contract)
            if not state_path.exists():
                raise RuntimeError("existing contract has no state; preserve and inspect, no automatic evaluation")
        else:
            if any(p.name != "run.lock" for p in out.iterdir()):
                raise RuntimeError("nonempty output has no matching walk-forward contract")
            # Persist contract and reservation without an interruptible gap. A
            # crash leaving contract-only is conservatively blocked on resume.
            with termination.defer() if termination else nullcontext():
                legacy.atomic_json(contract_path, contract)
                legacy.atomic_json(state_path, {"status": "RUNNING", "reserved_evaluations": args.budget})
        state = strict_loads(state_path.read_text())
        try:
            validate_state(state, args.budget, ready)
        except legacy.ProtocolError as exc:
            # A previously marked completion with a mismatching result is an
            # already consumed batch, never a reason to start another one.
            state.update(status="FAIL", error=str(exc))
            with termination.defer() if termination else nullcontext():
                legacy.atomic_json(state_path, state)
            raise
        # Distinguish a fresh reservation from a previously interrupted batch.
        if state["status"] != "RUNNING":
            print("walkforward", state["status"], "reserved_evaluations", args.budget, flush=True)
            return state
        # A new contract is the sole authorization to execute this batch.
        # Its reservation existed before spawning the evaluator.
        if not fresh:
            state.update(status="FAIL", error="interrupted batch consumed entire reserved budget; no automatic retry")
            legacy.atomic_json(state_path, state)
            print("walkforward FAIL reserved_evaluations", args.budget, flush=True)
            return state
        try:
            unchanged()
            result = child_json(command, args.timeout, termination)
            unchanged()
            validate_result(result, args.budget, ready)
            state.update(status="COMPLETE", result=result)
            legacy.atomic_json(state_path, state)
        except BaseException as exc:
            state.update(status="FAIL", error=str(exc) or type(exc).__name__)
            with termination.defer() if termination else nullcontext():
                legacy.atomic_json(state_path, state)
            raise
        print("walkforward COMPLETE reserved_evaluations", args.budget, flush=True)
        return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--budget", type=int, required=True)
    parser.add_argument("--binary", type=Path, default=Path("bin/lorentz-upgrade"))
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    if args.budget < 1 or args.timeout < 1:
        parser.error("positive authorized budget and timeout required")
    try:
        run(args)
    except Exception as exc:
        parser.exit(1, "walkforward FAIL: " + str(exc) + "\n")


if __name__ == "__main__":
    main()

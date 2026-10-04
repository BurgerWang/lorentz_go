"""Strict protocol2 client; the Classic client is unchanged."""
import math
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
import enhanced
import optimize as legacy
from enhanced_space import canonical

def utc(value):
    t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if t.tzinfo is None: t = t.replace(tzinfo=timezone.utc)
    if t.utcoffset().total_seconds() != 0 or t.microsecond:
        raise ValueError("whole-second UTC dates required")
    return t.isoformat(timespec="seconds").replace("+00:00", "Z")

def normalized_plan(source):
    p = enhanced.strict_loads(canonical(source))
    p["dataset_dir"] = str(Path(p["dataset_dir"]).resolve())
    p["history_start"] = utc(p["history_start"])
    for f in p["folds"]:
        f["start"], f["end"] = utc(f["start"]), utc(f["end"])
    return p

def finite_tree(value):
    if isinstance(value, float) and not math.isfinite(value): raise legacy.ProtocolError("nonfinite protocol number")
    if isinstance(value, dict):
        for x in value.values(): finite_tree(x)
    elif isinstance(value, list):
        for x in value: finite_tree(x)

def number(value):
    if type(value) not in (int, float): return False
    try: return math.isfinite(value)
    except OverflowError: return False

def validate_ready(ready, source):
    finite_tree(ready)
    if (ready.get("type") != "ready" or type(ready.get("protocol_version")) is not int or ready["protocol_version"] != 2
        or type(ready.get("strategy_schema_version")) is not int or ready["strategy_schema_version"] != 5
        or not isinstance(ready.get("supported_strategy_schemas"), list)
        or not any(type(v) is int and v == 5 for v in ready["supported_strategy_schemas"])
        or ready.get("plan") != normalized_plan(source)
        or not isinstance(ready.get("default_config"), dict) or ready["default_config"].get("schema_version") != 5):
        raise legacy.ProtocolError("enhanced ready protocol/schema/plan differs")
    for key in ("data_identity", "daily_identity"):
        if not isinstance(ready.get(key), str) or not re.fullmatch("[0-9a-f]{64}", ready[key]):
            raise legacy.ProtocolError("invalid enhanced " + key)

def _validate_result(result, config, ready, request_id=None):
    finite_tree(result)
    if not isinstance(result, dict) or result.get("type") != "result": raise legacy.ProtocolError("unexpected enhanced result")
    if request_id is not None and (type(result.get("id")) is not int or result["id"] != request_id): raise legacy.ProtocolError("enhanced result ID mismatch")
    if canonical(result.get("config")) != canonical(config): raise legacy.ProtocolError("enhanced result config mismatch")
    for key in ("plan", "data_identity", "daily_identity", "daily_aggregation"):
        if key not in result or result[key] != ready.get(key): raise legacy.ProtocolError("enhanced result " + key + " mismatch")
    folds = result.get("folds")
    if not isinstance(folds,list) or len(folds) != len(ready["plan"]["folds"]): raise legacy.ProtocolError("enhanced result fold count mismatch")
    for f, expected in zip(folds,ready["plan"]["folds"]):
        if any(f.get(k) != expected[k] for k in ("name", "start", "end")) or not isinstance(f.get("metrics"),dict): raise legacy.ProtocolError("enhanced fold identity mismatch")
        if any(not number(v) and not (k=="profit_factor" and v is None) for k,v in f["metrics"].items()): raise legacy.ProtocolError("invalid enhanced fold metrics")
        metrics=f["metrics"]
        count_keys={"trades","wins","losses","breakeven"}
        float_keys={"win_rate_pct","net_return_pct","max_drawdown_pct","expectancy_pct","gross_pnl","fees","funding","net_pnl"}
        if set(metrics)!=count_keys|float_keys|{"profit_factor"} or any(type(metrics[k]) is not int or metrics[k]<0 for k in count_keys) or any(not number(metrics[k]) for k in float_keys):
            raise legacy.ProtocolError("incomplete/invalid enhanced fold metric schema")
        if metrics["wins"]+metrics["losses"]+metrics["breakeven"]!=metrics["trades"]:raise legacy.ProtocolError("invalid fold trade accounting counts")
    a = result.get("aggregate",{})
    for k in ("net_return_pct", "win_rate_pct", "worst_fold_net_return_pct", "max_fold_drawdown_pct"):
        if not number(a.get(k)): raise legacy.ProtocolError("invalid enhanced aggregate " + k)
    for k in ("trades", "wins", "positive_folds"):
        if type(a.get(k)) is not int or a[k] < 0: raise legacy.ProtocolError("invalid enhanced count " + k)
    if a["wins"] > a["trades"] or a["positive_folds"] > len(folds) or not 0 <= a["win_rate_pct"] <= 100 or not 0 <= a["max_fold_drawdown_pct"] <= 100:
        raise legacy.ProtocolError("inconsistent enhanced aggregate")
    if not number(result.get("elapsed_seconds")) or result["elapsed_seconds"] < 0: raise legacy.ProtocolError("invalid enhanced timing")
    return result

def validate_result(result,config,ready,request_id=None):
    try:return _validate_result(result,config,ready,request_id)
    except legacy.ProtocolError:raise
    except (KeyError,TypeError,ValueError,AttributeError,RecursionError,OverflowError) as e:
        raise legacy.ProtocolError("invalid enhanced response structure") from e

class Server(enhanced.EnhancedServer):
    def __init__(self, binary, plan, timeout=600, termination=None, await_ready=True):
        self.source_plan = enhanced.strict_loads(Path(plan).read_text())
        super().__init__(binary, plan, timeout, termination, await_ready=await_ready)
    def _read(self):
        deadline = time.monotonic() + self.timeout
        while b"\n" not in self.buffer:
            left = deadline-time.monotonic()
            if left <= 0: raise legacy.ProtocolError("enhanced response timeout")
            if not self.selector.select(left): continue
            chunk = os.read(self.process.stdout.fileno(),65536)
            if not chunk: raise legacy.ProtocolError("enhanced child exited: " + "\n".join(self.stderr))
            self.buffer += chunk
            if len(self.buffer) > 16*1024*1024: raise legacy.ProtocolError("oversized enhanced response")
        line,self.buffer = self.buffer.split(b"\n",1)
        try:
            result = enhanced.strict_loads(line)
            if not isinstance(result,dict): raise ValueError("response object required")
            return result
        except (ValueError,UnicodeError,RecursionError) as e: raise legacy.ProtocolError("invalid enhanced JSON") from e
    def read_ready(self):
        self.ready = self._read()
        validate_ready(self.ready,self.source_plan)
    def evaluate(self, config):
        self.next_id += 1
        try:
            self.process.stdin.write((canonical({"id":self.next_id,"config":config})+"\n").encode())
            self.process.stdin.flush()
        except OSError as e: raise legacy.ProtocolError("enhanced request pipe failed") from e
        r = self._read()
        if type(r.get("id")) is not int or r["id"] != self.next_id: raise legacy.ProtocolError("enhanced ID mismatch")
        if r.get("type") == "error":
            if r.get("error_kind") == "evaluation": raise legacy.EvaluationError(r.get("error","evaluation failure"))
            raise legacy.ProtocolError("enhanced request/config error: " + str(r))
        return validate_result(r,config,self.ready,self.next_id)

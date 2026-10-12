"""Immutable cached-data contracts. No downloader and no strategy evaluation.

UTC candle ranges are left closed/right open. Funding timestamps retain their
original millisecond precision. Proxy stress is a scenario, never an error bound.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

VERSION = "robust-bundle-v1"
SOURCE = "https://fapi.binance.com (USD-M perpetual REST)"
PROXY = "binance-mark-kline-open-8h"
DAY = 86_400_000
BUCKET = 28_800_000
INTERVALS = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": DAY}
COLUMNS = ["open_time", "open", "high", "low", "close", "volume", "close_time"]


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for part in iter(lambda: stream.read(1 << 20), b""):
            digest.update(part)
    return digest.hexdigest()


def utc_ms(value):
    if type(value) is int:
        return value
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError("dates must be UTC")
    return int(parsed.timestamp() * 1000)


def iso(value):
    return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat(timespec="milliseconds").replace(".000+00:00", "Z").replace("+00:00", "Z")


def _number(value, positive=False):
    if isinstance(value, bool):
        raise ValueError("boolean numeric field")
    value = float(value)
    if not math.isfinite(value) or (positive and value <= 0):
        raise ValueError("nonfinite or nonpositive value")
    return value


def _integer(value):
    if type(value) is not int or value < 0:
        raise ValueError("timestamps must be nonnegative integer milliseconds")
    return value


def _json(path):
    return json.loads(Path(path).read_text(), parse_constant=lambda x: (_ for _ in ()).throw(ValueError("nonfinite JSON")))


def _dump(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n")


def read_candles(path, interval):
    step = INTERVALS[interval]
    with Path(path).open(newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != COLUMNS:
            raise ValueError("invalid candle header")
        rows = []
        for item in reader:
            if set(item) != set(COLUMNS) or any(v is None for v in item.values()):
                raise ValueError("invalid candle row")
            at, close_at = int(item["open_time"]), int(item["close_time"])
            if at < 0 or at % step or close_at != at + step - 1:
                raise ValueError("misaligned candle or close time")
            values = [_number(item[key], key != "volume") for key in COLUMNS[1:6]]
            o, h, low, c, volume = values
            if low > min(o, c) or h < max(o, c) or low > h or volume < 0:
                raise ValueError("invalid OHLC or volume")
            if rows and at != rows[-1][0] + step:
                raise ValueError("candle gap, duplicate or order violation")
            rows.append((at, *values, close_at))
    if not rows:
        raise ValueError("empty candle file")
    return rows


def _overlap(candles, daily, interval):
    step = INTERVALS[interval]
    first = (candles[0][0] + DAY - 1) // DAY * DAY
    end = (candles[-1][6] + 1) // DAY * DAY
    by_day = {row[0]: row for row in daily}
    by_time = {row[0]: row for row in candles}
    mismatches = []
    for at in range(first, end, DAY):
        if at not in by_day:
            raise ValueError("missing daily overlap context")
        part = [by_time[t] for t in range(at, at + DAY, step)]
        aggregated = (part[0][1], max(r[2] for r in part), min(r[3] for r in part), part[-1][4])
        row = by_day[at]
        if any(not math.isclose(a, b, rel_tol=1e-8, abs_tol=1e-8) for a, b in zip(aggregated, row[1:5])):
            raise ValueError("daily overlap OHLC disagreement")
        volume = sum(r[5] for r in part)
        if not math.isclose(volume, row[5], rel_tol=1e-8, abs_tol=1e-8):
            mismatches.append({"time": at, "target_aggregate_volume": volume, "daily_volume": row[5]})
    # Daily prehistory is valid; any daily value beyond the target's last closed
    # day is refused, so a bundle cannot smuggle next-day context.
    if daily[-1][6] + 1 > candles[-1][6] + 1:
        raise ValueError("future daily context beyond target history")
    return {"overlap_complete_days": max(0, (end - first) // DAY), "volume_discrepancies": mismatches,
            "volume_policy": "preserved original volumes; diagnostics only"}


def _funding(rows, mode):
    if mode not in {"exact", "proxy-stress-v1"} or not isinstance(rows, list) or not rows:
        raise ValueError("invalid funding mode or empty funding")
    prior = None
    proxy_count = 0
    for row in rows:
        at = _integer(row["time"])
        if type(row["rate"]) not in (int, float) or type(row["mark_price"]) not in (int, float):
            raise ValueError("funding requires numeric rates and prices")
        _number(row["rate"])
        _number(row["mark_price"], True)
        if prior is not None and (at <= prior or at - prior > BUCKET + 60_000):
            raise ValueError("funding gap, duplicate or order violation")
        source = row.get("mark_price_source", "")
        if source:
            if source != PROXY or mode == "exact":
                raise ValueError("proxy funding mislabeled exact or unknown source")
            bucket = _integer(row.get("mark_price_time"))
            if bucket % BUCKET or not 0 <= at - bucket < 60_000:
                raise ValueError("proxy timestamp not current bucket open")
            proxy_count += 1
        elif row.get("mark_price_time", 0):
            raise ValueError("exact funding has proxy timestamp")
        prior = at
    return len(rows) - proxy_count, proxy_count


def _mark_buckets(path):
    rows = _json(path)
    buckets = {}
    previous = None
    for row in rows:
        if not isinstance(row, list) or len(row) != 12:
            raise ValueError("invalid official mark kline")
        at = _integer(row[0])
        if at % BUCKET or row[6] != at + BUCKET - 1 or (previous is not None and at != previous + BUCKET):
            raise ValueError("mark buckets misaligned or not contiguous")
        o, h, low, c = [_number(x, True) for x in row[1:5]]
        if low > min(o, c) or h < max(o, c):
            raise ValueError("bad mark OHLC")
        buckets[at] = (o, h, low)
        previous = at
    return buckets


def _stress(funding, mark_path=None):
    buckets = _mark_buckets(mark_path) if mark_path else {}
    out = []
    for index, row in enumerate(funding):
        at, mark = row["time"], row["mark_price"]
        if row.get("mark_price_source"):
            opening = row["mark_price_time"]
            if opening not in buckets or not math.isclose(buckets[opening][0], mark, rel_tol=1e-12):
                raise ValueError("proxy does not match official mark opening")
            prior = opening - BUCKET
            if prior not in buckets:
                if index == 0:
                    continue  # preserved, explicitly outside stress scoring coverage
                raise ValueError("missing previous completed official mark bucket")
            low, high = min(buckets[prior][2], mark), max(buckets[prior][1], mark)
            available, start, end = opening, prior, opening
        else:
            low = high = mark
            available = start = end = at
        out.append({"time": at, "lower_mark": low, "upper_mark": high, "available_time": available,
                    "source_bucket_open": start, "source_bucket_close": end})
    return out


def _validate_stress(funding, stress):
    if not isinstance(stress, list):
        raise ValueError("stress must be an array")
    by_time = {}
    last = -1
    for row in stress:
        if set(row) != {"time", "lower_mark", "upper_mark", "available_time", "source_bucket_open", "source_bucket_close"}:
            raise ValueError("invalid stress row schema")
        at = _integer(row["time"])
        low, high = _number(row["lower_mark"], True), _number(row["upper_mark"], True)
        available, start, end = [_integer(row[k]) for k in ("available_time", "source_bucket_open", "source_bucket_close")]
        if at <= last or low > high or not start <= end <= available <= at:
            raise ValueError("future, unordered or invalid stress inputs")
        by_time[at] = row
        last = at
    for i, row in enumerate(funding):
        bounds = by_time.pop(row["time"], None)
        if bounds is None:
            if i == 0 and row.get("mark_price_source"):
                continue
            raise ValueError("missing funding stress row")
        mark = row["mark_price"]
        if not bounds["lower_mark"] <= mark <= bounds["upper_mark"]:
            raise ValueError("stress interval excludes central mark")
        if row.get("mark_price_source"):
            opening = row["mark_price_time"]
            if bounds["source_bucket_open"] != opening - BUCKET or bounds["source_bucket_close"] != opening or bounds["available_time"] != opening:
                raise ValueError("proxy stress must use previous completed bucket")
        elif bounds["lower_mark"] != mark or bounds["upper_mark"] != mark:
            raise ValueError("exact funding stress must preserve mark")
    if by_time:
        raise ValueError("stress row without funding event")
    if not stress:
        raise ValueError("no usable funding stress coverage")
    return stress[0]["time"]


def compile_profile(interval, available_bars, daily_bars, history_start=None, history_end=None):
    if interval not in INTERVALS or type(available_bars) is not int or available_bars <= 204 or daily_bars < 9:
        raise ValueError("unsupported interval or insufficient initialization history")
    mature = available_bars - 200 - 4
    domains = [v for v in (500, 1000, 2000, 4000) if v <= mature]
    return {"version": "native-bars-v1", "interval": interval, "bar_ms": INTERVALS[interval],
            "label_horizon_bars": 4, "label_horizon_ms": 4 * INTERVALS[interval],
            "warmup_bars": 200, "available_bars": available_bars, "mature_samples_upper_bound": mature,
            "daily_bars": daily_bars, "d1_context": "completed UTC daily bars; current just-closed day allowed at 1d close",
            "d1_slope_z_unit": "target-bars after daily alignment", "parameter_length_unit": "native target bars",
            "max_bars_back_domain": domains, "neighbors_domain": {"low": 1, "high": min(100, mature), "step": 1},
            "evidence_sufficient": bool(domains), "clipping_policy": "original legal categories bounded by mature sample count; never reduce outcome gates",
            "history_start": history_start, "history_end": history_end,
            "longer_config_warmup": "Go validates chosen feature/kernel/daily dependencies"}


def _data(root, manifest):
    result = {}
    for key in ("candles", "daily", "funding", "stress"):
        item = manifest["files"][key]
        path = Path(item["path"])
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise ValueError("bundle file path must remain inside bundle")
        full = root / path
        if full.is_symlink() or root.resolve() not in full.resolve().parents or sha256(full) != item["sha256"]:
            raise ValueError("bundle file identity changed")
        result[key] = full
    return result


def validate_bundle(path):
    root = Path(path)
    if root.is_file():
        root = root.parent
    manifest = _json(root / "bundle.json")
    if manifest.get("version") != VERSION or manifest.get("schema_version") != 1 or manifest.get("source") != SOURCE or manifest.get("symbol") != "ETHUSDT" or manifest.get("interval") not in INTERVALS:
        raise ValueError("unsupported bundle identity")
    paths = _data(root, manifest)
    # The immutable source identity survives local file re-hashing. It prevents
    # stripping proxy labels to misrepresent settlement provenance.
    for key in ("candles", "daily", "funding"):
        if manifest["sources"][key]["sha256"] != sha256(paths[key]):
            raise ValueError(key + " differs from frozen source snapshot")
    if "marks" in manifest["sources"]:
        source = manifest["sources"]["marks"]
        snapshot = root / source["snapshot"]
        if snapshot.name != source["snapshot"] or snapshot.is_symlink() or sha256(snapshot) != source["sha256"]:
            raise ValueError("mark source snapshot changed")
        if _stress(_json(paths["funding"]), snapshot) != _json(paths["stress"]):
            raise ValueError("stress differs from causal frozen mark source")
    candles = read_candles(paths["candles"], manifest["interval"])
    daily = read_candles(paths["daily"], "1d")
    if [utc_ms(manifest[k]) for k in ("history_start", "history_end", "daily_history_start")] != [candles[0][0], candles[-1][6] + 1, daily[0][0]]:
        raise ValueError("bundle history declaration mismatch")
    quality = _overlap(candles, daily, manifest["interval"])
    funding, stress = _json(paths["funding"]), _json(paths["stress"])
    exact, proxy = _funding(funding, manifest["funding_mode"])
    stress_start = _validate_stress(funding, stress)
    expected = _quality(candles, daily, funding, exact, proxy, stress_start, quality)
    if manifest["quality"] != expected:
        raise ValueError("bundle quality declaration mismatch")
    if manifest["profile"] != compile_profile(manifest["interval"], len(candles), len(daily), manifest["history_start"], manifest["history_end"]):
        raise ValueError("profile declaration mismatch")
    return manifest


def _quality(candles, daily, funding, exact, proxy, stress_start, overlap):
    scoring_start = max(candles[0][0] + 200 * (candles[0][6] + 1 - candles[0][0]), funding[0]["time"], stress_start)
    scoring_start = (scoring_start + DAY - 1) // DAY * DAY
    scoring_end = min(candles[-1][6] + 1, funding[-1]["time"] + BUCKET) // DAY * DAY
    return {**overlap, "candles_rows": len(candles), "daily_rows": len(daily), "funding_rows": len(funding),
            "exact_rows": exact, "proxy_rows": proxy, "funding_start": iso(funding[0]["time"]),
            "funding_end": iso(funding[-1]["time"] + BUCKET), "stress_coverage_start": iso(stress_start),
            "scoring_start": iso(scoring_start), "scoring_end": iso(scoring_end),
            "score_coverage_available": scoring_start < scoring_end,
            "proxy_disclosure": "historical rates; scenario marks from previous completed official8h bucket and current open, not proven settlement bounds"}


def prepare_bundle(source_dir, interval, out, funding_mode="exact", stress_source=None, *, candle_dir=None, funding_dir=None):
    source_dir, out = Path(source_dir).resolve(), Path(out).absolute()
    candle_dir = Path(candle_dir or source_dir).resolve()
    funding_dir = Path(funding_dir or source_dir).resolve()
    if interval not in INTERVALS:
        raise ValueError("unsupported interval")
    for directory in (source_dir, candle_dir, funding_dir):
        if out.resolve() == directory or directory in out.resolve().parents:
            raise ValueError("bundle output cannot modify source directory")
    if out.exists():
        raise FileExistsError("immutable bundle already exists")
    inputs = {"candles": candle_dir / f"{interval}.csv", "daily": source_dir / "1d.csv", "funding": funding_dir / "funding.json"}
    for directory in set((source_dir, candle_dir, funding_dir)):
        meta = _json(directory / "metadata.json")
        if meta.get("source") != SOURCE or meta.get("symbol") != "ETHUSDT":
            raise ValueError("unknown source or symbol")
    if funding_mode == "proxy-stress-v1":
        inputs["marks"] = Path(stress_source or funding_dir / "raw/mark-8h-2020-2024.json").resolve()
    for i, directory in enumerate(sorted(set((source_dir, candle_dir, funding_dir)))):
        inputs[f"metadata_{i}"] = directory / "metadata.json"
        if (directory / "fixture.json").exists():
            inputs[f"fixture_{i}"] = directory / "fixture.json"
    snapshots = {key: sha256(path) for key, path in inputs.items()}
    out.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".robust-bundle-", dir=out.parent))
    published = False
    try:
        for key, name in (("candles", "candles.csv"), ("daily", "daily.csv"), ("funding", "funding.json")):
            shutil.copyfile(inputs[key], staging / name)
            if sha256(staging / name) != snapshots[key]:
                raise ValueError("source changed during bundle preparation")
        candles, daily = read_candles(staging / "candles.csv", interval), read_candles(staging / "daily.csv", "1d")
        overlap = _overlap(candles, daily, interval)
        funding = _json(staging / "funding.json")
        exact, proxy = _funding(funding, funding_mode)
        # Validate proxy sources from a snapshot too, not a live read after hashing.
        marks = None
        if "marks" in inputs:
            marks = staging / "source-mark-8h.json"
            shutil.copyfile(inputs["marks"], marks)
            if sha256(marks) != snapshots["marks"]:
                raise ValueError("source changed during bundle preparation")
        stress = _stress(funding, marks)
        stress_start = _validate_stress(funding, stress)
        _dump(staging / "funding-stress.json", stress)
        manifest = {"version": VERSION, "schema_version": 1, "source": SOURCE, "symbol": "ETHUSDT", "interval": interval,
                    "history_start": iso(candles[0][0]), "history_end": iso(candles[-1][6] + 1), "daily_history_start": iso(daily[0][0]),
                    "funding_mode": funding_mode, "files": {key: {"path": name, "sha256": sha256(staging / name)} for key, name in
                    (("candles", "candles.csv"), ("daily", "daily.csv"), ("funding", "funding.json"), ("stress", "funding-stress.json"))},
                    "sources": {key: {"path": str(path), "sha256": snapshots[key]} for key, path in inputs.items()},
                    "quality": _quality(candles, daily, funding, exact, proxy, stress_start, overlap),
                    "exposure": {"use": "exposed_history", "known_exposed_before": "2026-10-02T00:00:00Z", "future_use_requires_freeze": True},
                    "synthetic": any(key.startswith("fixture_") and _json(path).get("synthetic") is True for key, path in inputs.items())}
        if marks:
            manifest["sources"]["marks"]["snapshot"] = "source-mark-8h.json"
        manifest["profile"] = compile_profile(interval, len(candles), len(daily), manifest["history_start"], manifest["history_end"])
        _dump(staging / "bundle.json", manifest)
        validate_bundle(staging)
        for key, path in inputs.items():
            if sha256(path) != snapshots[key]:
                raise ValueError("source changed during bundle preparation")
        out.mkdir()  # exclusive claim, refuses even empty pre-existing directories
        published = True
        for file in staging.iterdir():
            file.rename(out / file.name)
        return manifest
    except BaseException:
        if published:
            shutil.rmtree(out)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _dependency(root, manifest, history_start, daily_start, cutoff):
    paths = _data(root, manifest)
    low = [row for row in read_candles(paths["candles"], manifest["interval"]) if history_start <= row[0] and row[6] + 1 <= cutoff]
    daily = [row for row in read_candles(paths["daily"], "1d") if daily_start <= row[0] and row[6] + 1 <= cutoff]
    funding = [row for row in _json(paths["funding"]) if history_start <= row["time"] < cutoff]
    stress = [row for row in _json(paths["stress"]) if history_start <= row["time"] < cutoff]
    if not low or low[0][0] != history_start or not daily or daily[0][0] != daily_start:
        raise ValueError("missing explicit dependency initialization prefix")
    return {"candles": low, "daily": daily, "funding": funding, "stress": stress}


def verify_successor(old, new, history_start, daily_history_start, cutoff):
    old, new = Path(old), Path(new)
    first, second = validate_bundle(old), validate_bundle(new)
    for key in ("source", "symbol", "interval", "funding_mode"):
        if first[key] != second[key]:
            raise ValueError("successor identity changed")
    start, daily_start, end = map(utc_ms, (history_start, daily_history_start, cutoff))
    if not start < end or not daily_start < end or end > utc_ms(first["history_end"]):
        raise ValueError("dependency cutoff outside old history")
    # Successorship requires the entire old data prefix, including funding/stress
    # beyond one candidate's cutoff. Candidate dependencies are checked separately.
    all_old = _dependency(old, first, utc_ms(first["history_start"]), utc_ms(first["daily_history_start"]), utc_ms(first["history_end"]))
    all_new = _dependency(new, second, utc_ms(first["history_start"]), utc_ms(first["daily_history_start"]), utc_ms(first["history_end"]))
    if all_old != all_new or utc_ms(second["history_end"]) < utc_ms(first["history_end"]):
        raise ValueError("successor modifies historical prefix")
    dependency = _dependency(old, first, start, daily_start, end)
    if dependency != _dependency(new, second, start, daily_start, end):
        raise ValueError("candidate dependency prefix changed")
    return {"version": "robust-successor-v1", "old_bundle_sha256": sha256(old / "bundle.json"), "new_bundle_sha256": sha256(new / "bundle.json"),
            "history_start": iso(start), "daily_history_start": iso(daily_start), "origin_cutoff": iso(end),
            "dependency_rows": {key: len(value) for key, value in dependency.items()}, "prefix_verified": True,
            "old_scores_inherited": False}


def candidate_migration_record(old, new, history_start, daily_history_start, origin_cutoff,
                               old_engine, new_engine, candidate_identity, *, interval=None,
                               funding_start=None):
    """Verify actual legacy dependencies, retaining the original frozen cutoff.

    Candle/D1 initialization and funding scoring start are separate explicit
    dependencies. This permits additional unused funding warmup without calling
    a revision of actual scored cash flows equivalent. Engine compatibility is
    deliberately pending until a separately budgeted runtime comparison.
    """
    old, new = Path(old), Path(new)
    target = validate_bundle(new)
    interval = interval or target["interval"]
    if interval != target["interval"]:
        raise ValueError("candidate interval changed")
    start, daily_start, cutoff = map(utc_ms, (history_start, daily_history_start, origin_cutoff))
    funding_start = utc_ms(funding_start) if funding_start is not None else start
    if not start < cutoff or not daily_start < cutoff or not start <= funding_start < cutoff:
        raise ValueError("invalid candidate dependency dates")
    target_paths = _data(new, target)
    if (old / "bundle.json").exists():
        source = validate_bundle(old)
        if any(source[key] != target[key] for key in ("source", "symbol", "interval")):
            raise ValueError("candidate source identity changed")
        source_paths = _data(old, source)
        old_identity = {"bundle_sha256": sha256(old / "bundle.json")}
    else:
        meta = _json(old / "metadata.json")
        if meta.get("source") != SOURCE or meta.get("symbol") != "ETHUSDT":
            raise ValueError("unknown legacy source")
        source_paths = {"candles": old / (interval + ".csv"), "daily": old / "1d.csv", "funding": old / "funding.json"}
        old_identity = {"files": {key: sha256(path) for key, path in source_paths.items()},
                        "metadata_sha256": sha256(old / "metadata.json")}
    counts = {}
    for key, lower, period in (("candles", start, interval), ("daily", daily_start, "1d")):
        original = [row for row in read_candles(source_paths[key], period) if lower <= row[0] and row[6] + 1 <= cutoff]
        successor = [row for row in read_candles(target_paths[key], period) if lower <= row[0] and row[6] + 1 <= cutoff]
        if not original or original[0][0] != lower or original != successor or original[-1][6] + 1 < cutoff:
            raise ValueError("candidate " + key + " dependency prefix changed or missing")
        counts[key] = len(original)
    original = [row for row in _json(source_paths["funding"]) if funding_start <= row["time"] < cutoff]
    successor = [row for row in _json(target_paths["funding"]) if funding_start <= row["time"] < cutoff]
    if not original or original != successor:
        raise ValueError("candidate funding dependency prefix changed or missing")
    _funding(original, "proxy-stress-v1")
    if original[0]["time"] - funding_start > BUCKET + 60_000 or cutoff - original[-1]["time"] > BUCKET + 60_000:
        raise ValueError("candidate funding dependency coverage missing")
    counts["funding"] = len(original)
    return {"version": "robust-candidate-migration-v1", "candidate_identity": candidate_identity,
            "old_source": old_identity, "new_bundle_sha256": sha256(new / "bundle.json"),
            "history_start": iso(start), "daily_history_start": iso(daily_start),
            "funding_start": iso(funding_start), "origin_cutoff": iso(cutoff),
            "dependency_rows": counts, "prefix_verified": True, "old_scores_inherited": False,
            "old_engine_sha256": sha256(old_engine), "new_engine_sha256": sha256(new_engine),
            "engine_compatibility": "pending-runtime-comparison", "evaluation_authorized": False}

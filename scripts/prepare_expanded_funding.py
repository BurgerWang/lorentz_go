#!/usr/bin/env python3
"""Prepare an explicitly labelled historical funding-price estimate from cached public data.

Historical funding rates are never replaced. Associated settlement marks take
priority; missing marks use the official eight-hour mark-kline opening price.
The proxy timestamp identifies the kline bucket, not an exact observed tick.
"""

import argparse
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
import tempfile

BUCKET_MS = 28_800_000
SOURCE = "binance-mark-kline-open-8h"
DATA_SOURCE = "https://fapi.binance.com (USD-M perpetual REST)"


def utc_ms(value):
    return int(datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


def read_json(path):
    return json.loads(path.read_text())


def positive(value):
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("mark prices must be finite and positive")
    return value


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".funding-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as target:
            json.dump(value, target, indent=2, allow_nan=False)
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def prepare(dataset, exact_dataset, start):
    metadata = read_json(dataset / "metadata.json")
    exact_meta = read_json(exact_dataset / "metadata.json")
    for meta in (metadata, exact_meta):
        if meta["source"] != DATA_SOURCE or meta["symbol"] != "ETHUSDT":
            raise ValueError("requires cached Binance ETHUSDT USD-M perpetual data")
    end = metadata["End"]
    if end != exact_meta["End"] or not metadata["Start"] <= start < end:
        raise ValueError("dataset ranges are incompatible")

    prices = {}
    previous = None
    for row in read_json(dataset / "raw/mark-8h-2020-2024.json"):
        if len(row) != 12:
            raise ValueError("invalid mark kline shape")
        at = row[0]
        if (not isinstance(at, int) or at < 0 or at % BUCKET_MS
                or row[6] != at + BUCKET_MS - 1
                or (previous is not None and at != previous + BUCKET_MS)):
            raise ValueError("mark klines must be aligned, contiguous and ordered")
        opening, high, low, closing = map(positive, row[1:5])
        if low > min(opening, closing) or high < max(opening, closing) or low > high:
            raise ValueError("invalid mark kline OHLC")
        prices[at] = opening
        previous = at

    exact = read_json(exact_dataset / "funding.json")
    if len(exact) != exact_meta["funding_rows"]:
        raise ValueError("exact funding row count differs from metadata")
    raw = read_json(dataset / "raw/funding-rate-2019-2023.json")
    rows = []
    previous = -1
    for item in raw:
        at = item["fundingTime"]
        if (item["symbol"] != "ETHUSDT" or item.get("rateType", "Regular") != "Regular"
                or not isinstance(at, int) or at <= previous):
            raise ValueError("invalid raw funding identity, type or order")
        previous = at
        rate = float(item["fundingRate"])
        if not math.isfinite(rate):
            raise ValueError("nonfinite historical funding rate")
        if not start <= at < end:
            continue
        record = {"time": at, "rate": rate}
        if item.get("markPrice"):
            record["mark_price"] = positive(item["markPrice"])
        else:
            bucket = at // BUCKET_MS * BUCKET_MS
            if at - bucket >= 60_000 or bucket not in prices:
                raise ValueError(f"no matching opening mark near settlement {at}")
            record.update(mark_price=prices[bucket], mark_price_source=SOURCE, mark_price_time=bucket)
        rows.append(record)

    for item in exact:
        if item.get("mark_price_source") or item.get("mark_price_time", 0):
            raise ValueError("exact source unexpectedly contains proxy records")
        positive(item["mark_price"])
        if not math.isfinite(item["rate"]):
            raise ValueError("nonfinite exact funding rate")
        if start <= item["time"] < end:
            rows.append(dict(item))
    if not rows:
        raise ValueError("empty prepared funding series")
    for first, second in zip(rows, rows[1:]):
        if second["time"] <= first["time"] or second["time"] - first["time"] > BUCKET_MS + 36_000:
            raise ValueError("prepared funding records overlap or contain a coverage gap")
    if rows[0]["time"] - start > BUCKET_MS + 36_000 or end - rows[-1]["time"] > BUCKET_MS + 36_000:
        raise ValueError("prepared funding does not cover the requested interval")

    estimated = [row for row in rows if row.get("mark_price_source") == SOURCE]
    summary = {
        "source": DATA_SOURCE, "symbol": "ETHUSDT", "start_inclusive": start, "end_exclusive": end,
        "funding_rows": len(rows), "estimated_mark_rows": len(estimated),
        "associated_settlement_mark_rows": len(rows) - len(estimated),
        "price_source_for_estimates": SOURCE,
        "first_estimated_time": estimated[0]["time"] if estimated else None,
        "last_estimated_time": estimated[-1]["time"] if estimated else None,
        "max_estimated_bucket_offset_ms": max((r["time"] - r["mark_price_time"] for r in estimated), default=0),
        "rate_policy": "all rates are historical Binance records; associated settlement marks take priority",
        "price_time_policy": "mark_price_time is the eight-hour bucket opening timestamp, not an exact tick or settlement observation",
        "inputs": [str(dataset / "raw/funding-rate-2019-2023.json"),
                   str(dataset / "raw/mark-8h-2020-2024.json"), str(exact_dataset / "funding.json")],
    }
    metadata.update(funding_requested_start=start, funding_rows=len(rows),
                    funding_estimated_rows=len(estimated), FundingStart=rows[0]["time"], FundingEnd=rows[-1]["time"],
                    max_funding_gap_hours=max((b["time"] - a["time"]) / 3_600_000 for a, b in zip(rows, rows[1:])))
    metadata["downloaded_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    # No evaluator may run against this staging dataset during these two writes.
    # Existing study/cache files live elsewhere and are never touched.
    atomic_json(dataset / "funding.json", rows)
    atomic_json(dataset / "metadata.json", metadata)
    atomic_json(dataset / "raw/funding-estimation-summary.json", summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=Path("data/ETHUSDT-expanded"))
    parser.add_argument("--exact-dataset", type=Path, default=Path("data/ETHUSDT"))
    parser.add_argument("--start", default="2020-09-22")
    args = parser.parse_args()
    if args.dataset.resolve() == args.exact_dataset.resolve():
        parser.error("expanded and original dataset directories must differ")
    print(json.dumps(prepare(args.dataset, args.exact_dataset, utc_ms(args.start)), indent=2))


if __name__ == "__main__":
    main()

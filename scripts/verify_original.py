"""Independent source-state recurrence; not a TradingView runtime equivalence test."""
import json
import math
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
env = os.environ.copy()
env.setdefault("GOCACHE", "/tmp/lorentz-go-cache")
result = subprocess.check_output(
    ["go", "run", "-buildvcs=false", "scripts/reference_fixture.go"], cwd=ROOT, env=env
)
results = json.loads(result)
sources = [100 + (i * 7) % 13 for i in range(31)]
features = [(i % 7 / 10, i % 3 / 10) for i in range(31)]
rq = [(i * 7) % 13 for i in range(31)]
def sign(a):
    return (a > 0) - (a < 0)

for algo in ("original-chart", "original-online"):
    for full in (False, True):
        key = algo + ("-full" if full else "")
        labels, distances, votes, indices = [], [], [], []
        signal = held = prediction = 0
        long_entry = short_entry = bullish_alert = bearish_alert = -1
        valid_long = valid_short = False
        for t in range(31):
            labels.append(-sign(sources[t] - sources[t - 4]) if t >= 4 else 0)
            gate = max(0, (30 if algo == "original-chart" else t) - 8)
            start = end = -1
            if t >= gate:
                start = 0 if full else gate
                end = min(7, t)
                last = -1
                step = 1 if end >= start else -1
                for j in range(start, end + step, step):
                    d = sum(math.log(1 + abs(a - b)) for a, b in zip(features[t], features[j]))
                    if d >= last and j % 4 != 0:
                        last = d
                        distances.append(d); votes.append(labels[j]); indices.append(j)
                        if len(votes) > 3:
                            last = distances[math.floor(3 * 3 / 4 + 0.5)]
                            distances.pop(0); votes.pop(0); indices.pop(0)
                prediction = sum(votes)
            previous = signal
            if prediction:
                signal = sign(prediction)
            change = signal != previous
            held = 0 if change else held + 1
            bull = t >= 1 and rq[t] > rq[t - 1]
            bear = t >= 1 and rq[t] < rq[t - 1]
            bc = t >= 2 and bull and rq[t - 2] > rq[t - 1]
            rc = t >= 2 and bear and rq[t - 2] < rq[t - 1]
            sl = change and signal == 1 and bull
            ss = change and signal == -1 and bear
            if sl: long_entry = t
            if ss: short_entry = t
            if bc: bullish_alert = t
            if rc: bearish_alert = t
            el, es = rc and valid_long, bc and valid_short
            valid_long = long_entry >= 0 and bearish_alert >= 0 and bearish_alert < long_entry
            valid_short = short_entry >= 0 and bullish_alert >= 0 and bullish_alert < short_entry
            expected = dict(prediction=prediction, signal=signal, bars_held=held,
                            start_long=sl, start_short=ss, end_long=el, end_short=es,
                            neighbors=len(votes), max_neighbor_index=max(indices, default=-1),
                            search_start=start, search_end=end)
            for field, want in expected.items():
                got = results[key][t][field]
                assert got == want, (key, t, field, got, want)
        print(key, "31 independent reference states match")
for key in ("aligned-knn", "aligned-knn-full"):
    for t, point in enumerate(results[key]):
        assert point["max_neighbor_index"] == -1 or point["max_neighbor_index"] <= t - 4
print("aligned-knn: selected neighbor maturity bounds match")

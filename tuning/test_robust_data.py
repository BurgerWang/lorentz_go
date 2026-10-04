"""Synthetic data-contract counterexamples; never strategy/market tests."""
import csv
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import robust_data as rd

START = 1704067200000


def fixture(root, interval="1h", days=800, proxy=False, daily_prefix=3):
    root = Path(root)
    root.mkdir()
    step = rd.INTERVALS[interval]
    rows = []
    for i in range(days * rd.DAY // step):
        at = START + i * step
        # Flat OHLC with daily-varying level keeps all aggregate intervals equal.
        value = 100 + i * step // rd.DAY % 17
        rows.append((at, value, value + 2, value - 2, value + 1, 1, at + step - 1))
    daily = []
    for day in range(-daily_prefix, days):
        at = START + day * rd.DAY
        value = 100 + day % 17
        daily.append((at, value, value + 2, value - 2, value + 1, rd.DAY // step, at + rd.DAY - 1))
    for name, content in ((interval + ".csv", rows), ("1d.csv", daily)):
        with (root / name).open("w", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(rd.COLUMNS)
            writer.writerows(content)
    funding = [{"time": START + i * rd.BUCKET, "rate": .0001 if i % 2 else -.0001, "mark_price": 100} for i in range(days * 3)]
    if proxy:
        for row in funding:
            row.update(mark_price_source=rd.PROXY, mark_price_time=row["time"])
        marks = [[START + i * rd.BUCKET, "100", "103", "97", "101", "0", START + (i + 1) * rd.BUCKET - 1, "0", 0, "0", "0", "0"] for i in range(days * 3)]
        (root / "raw").mkdir()
        (root / "raw/mark-8h-2020-2024.json").write_text(json.dumps(marks))
    (root / "funding.json").write_text(json.dumps(funding))
    (root / "metadata.json").write_text(json.dumps({"source": rd.SOURCE, "symbol": "ETHUSDT"}))
    (root / "fixture.json").write_text(json.dumps({"synthetic": True}))
    return root


def rewrite_manifest(bundle, key):
    manifest = json.loads((bundle / "bundle.json").read_text())
    manifest["files"][key]["sha256"] = rd.sha256(bundle / manifest["files"][key]["path"])
    (bundle / "bundle.json").write_text(json.dumps(manifest))


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_all_intervals_immutable_and_longer_daily(self):
        for interval in rd.INTERVALS:
            with self.subTest(interval=interval):
                source = fixture(self.root / (interval + "-source"), interval)
                bundle = self.root / (interval + "-bundle")
                result = rd.prepare_bundle(source, interval, bundle)
                self.assertEqual(rd.validate_bundle(bundle), result)
                self.assertTrue(result["synthetic"])
                self.assertEqual(result["profile"]["label_horizon_ms"], 4 * rd.INTERVALS[interval])
                self.assertEqual(result["quality"]["overlap_complete_days"], 803 if interval == "1d" else 800)
                self.assertLessEqual(rd.utc_ms(result["daily_history_start"]), rd.utc_ms(result["history_start"]))
                self.assertEqual(result["profile"]["d1_slope_z_unit"], "target-bars after daily alignment")
                with self.assertRaises(FileExistsError):
                    rd.prepare_bundle(source, interval, bundle)
                with self.assertRaises(ValueError):
                    rd.prepare_bundle(source, interval, source / "bad")

    def test_proxy_causal_previous_bucket_and_disclosure(self):
        source = fixture(self.root / "source", "4h", proxy=True)
        bundle = self.root / "bundle"
        result = rd.prepare_bundle(source, "4h", bundle, "proxy-stress-v1")
        stress = json.loads((bundle / "funding-stress.json").read_text())
        self.assertEqual(stress[0]["time"], START + rd.BUCKET)
        self.assertEqual((stress[0]["lower_mark"], stress[0]["upper_mark"]), (97, 103))
        self.assertEqual(stress[0]["source_bucket_close"], START + rd.BUCKET)
        self.assertEqual(len(stress) + 1, result["quality"]["funding_rows"])
        self.assertEqual(len(json.loads((bundle / "funding.json").read_text())), 2400)
        stress[0]["available_time"] += rd.BUCKET
        (bundle / "funding-stress.json").write_text(json.dumps(stress))
        rewrite_manifest(bundle, "stress")
        with self.assertRaisesRegex(ValueError, "causal"):
            rd.validate_bundle(bundle)
        with self.assertRaisesRegex(ValueError, "mislabeled"):
            rd.prepare_bundle(source, "4h", self.root / "badexact", "exact")

    def test_source_changed_during_snapshot_refused(self):
        source = fixture(self.root / "source", "1d")
        original = rd.shutil.copyfile
        def mutate(src, target):
            result = original(src, target)
            if Path(src).name == "funding.json":
                with Path(src).open("a") as stream:
                    stream.write(" ")
            return result
        with patch.object(rd.shutil, "copyfile", mutate):
            with self.assertRaisesRegex(ValueError, "source changed"):
                rd.prepare_bundle(source, "1d", self.root / "bundle")
        self.assertFalse((self.root / "bundle").exists())

    def test_future_daily_refused(self):
        source = fixture(self.root / "source", "1d")
        with (source / "1d.csv").open("a") as stream:
            at = START + 800 * rd.DAY
            stream.write(f"{at},100,102,98,101,1,{at + rd.DAY - 1}\n")
        with self.assertRaisesRegex(ValueError, "future"):
            # For1d the candle file is the daily source, so separate target source.
            target = fixture(self.root / "target", "4h")
            rd.prepare_bundle(source, "4h", self.root / "bundle", candle_dir=target)

    def test_gaps_and_ohlc_and_funding_gaps(self):
        for variant in ("gap", "price", "funding"):
            source = fixture(self.root / variant, "1d")
            if variant == "funding":
                path = source / "funding.json"
                rows = json.loads(path.read_text())
                del rows[10]
                path.write_text(json.dumps(rows))
            else:
                path = source / "1d.csv"
                lines = path.read_text().splitlines()
                if variant == "gap":
                    del lines[10]
                else:
                    fields = lines[10].split(",")
                    fields[2] = "1"
                    lines[10] = ",".join(fields)
                path.write_text("\n".join(lines) + "\n")
            with self.assertRaises(ValueError):
                rd.prepare_bundle(source, "1d", self.root / (variant + "-bundle"))

    def test_insufficient_data_and_profile_clipping(self):
        with self.assertRaisesRegex(ValueError, "insufficient"):
            rd.compile_profile("1d", 204, 500)
        profile = rd.compile_profile("1h", 800, 50)
        self.assertEqual(profile["max_bars_back_domain"], [500])
        self.assertFalse(rd.compile_profile("1d", 300, 300)["evidence_sufficient"])
        with self.assertRaises(ValueError):
            rd.compile_profile("1w", 1000, 1000)

    def test_append_only_successor_semantic_prefix_and_origin(self):
        oldsrc = fixture(self.root / "old-source", "1d", days=800)
        newsrc = fixture(self.root / "new-source", "1d", days=810)
        old, new = self.root / "old", self.root / "new"
        rd.prepare_bundle(oldsrc, "1d", old)
        rd.prepare_bundle(newsrc, "1d", new)
        cutoff = START + 790 * rd.DAY
        record = rd.verify_successor(old, new, START, START - 3 * rd.DAY, cutoff)
        self.assertEqual(record["origin_cutoff"], rd.iso(cutoff))
        self.assertFalse(record["old_scores_inherited"])
        # Byte formatting changes alone preserve semantic prefix.
        funding = json.loads((new / "funding.json").read_text())
        (new / "funding.json").write_text(json.dumps(funding, indent=1))
        rewrite_manifest(new, "funding")
        manifest = json.loads((new / "bundle.json").read_text())
        manifest["sources"]["funding"]["sha256"] = rd.sha256(new / "funding.json")
        (new / "bundle.json").write_text(json.dumps(manifest))
        rd.verify_successor(old, new, START, START - 3 * rd.DAY, cutoff)
        # Price/rate revisions are new research even if identity is re-hashed.
        funding[20]["rate"] += .001
        (new / "funding.json").write_text(json.dumps(funding))
        rewrite_manifest(new, "funding")
        with self.assertRaisesRegex(ValueError, "frozen source snapshot"):
            rd.verify_successor(old, new, START, START - 3 * rd.DAY, cutoff)

    def test_proxy_label_stripping_and_current_bucket_high_refused(self):
        source = fixture(self.root / "source", "4h", proxy=True)
        bundle = self.root / "bundle"
        rd.prepare_bundle(source, "4h", bundle, "proxy-stress-v1")
        funding = json.loads((bundle / "funding.json").read_text())
        funding[1].pop("mark_price_source")
        funding[1].pop("mark_price_time")
        (bundle / "funding.json").write_text(json.dumps(funding))
        rewrite_manifest(bundle, "funding")
        with self.assertRaisesRegex(ValueError, "frozen source snapshot"):
            rd.validate_bundle(bundle)
        # Even re-hashed stresses cannot use post-settlement current-bucket high.
        original = source / "funding.json"
        (bundle / "funding.json").write_bytes(original.read_bytes())
        rewrite_manifest(bundle, "funding")
        stress = json.loads((bundle / "funding-stress.json").read_text())
        stress[0]["upper_mark"] = 123456
        (bundle / "funding-stress.json").write_text(json.dumps(stress))
        rewrite_manifest(bundle, "stress")
        with self.assertRaisesRegex(ValueError, "causal"):
            rd.validate_bundle(bundle)

    def test_legacy_candidate_migration_preserves_cutoff_requires_runtime(self):
        old = fixture(self.root / "old", "4h", days=800)
        source = fixture(self.root / "source", "4h", days=810, daily_prefix=5)
        bundle = self.root / "bundle"
        rd.prepare_bundle(source, "4h", bundle)
        old_engine, new_engine = self.root / "engine-old", self.root / "engine-new"
        old_engine.write_bytes(b"old-engine")
        new_engine.write_bytes(b"new-engine-with-observation")
        cutoff = START + 790 * rd.DAY
        record = rd.candidate_migration_record(old, bundle, START, START - 3 * rd.DAY,
                    cutoff, old_engine, new_engine, "frozen-configuration", funding_start=START + 30 * rd.DAY)
        self.assertEqual(record["origin_cutoff"], rd.iso(cutoff))
        self.assertEqual(record["dependency_rows"]["candles"], 790 * 6)
        self.assertEqual(record["dependency_rows"]["daily"], 793)
        self.assertEqual(record["engine_compatibility"], "pending-runtime-comparison")
        self.assertFalse(record["old_scores_inherited"])
        self.assertFalse(record["evaluation_authorized"])
        # Revision at initialization, before scoring, still changes classifier inputs.
        path = old / "4h.csv"
        rows = path.read_text().splitlines()
        item = rows[20].split(",")
        item[1] = str(float(item[1]) + .25)
        rows[20] = ",".join(item)
        path.write_text("\n".join(rows) + "\n")
        with self.assertRaisesRegex(ValueError, "candles dependency prefix"):
            rd.candidate_migration_record(old, bundle, START, START - 3 * rd.DAY,
                    cutoff, old_engine, new_engine, "frozen-configuration", funding_start=START + 30 * rd.DAY)

    def test_volume_difference_diagnostic_preserved(self):
        source = fixture(self.root / "source", "4h")
        path = source / "1d.csv"
        lines = path.read_text().splitlines()
        fields = lines[10].split(",")
        fields[5] = "999"
        lines[10] = ",".join(fields)
        path.write_text("\n".join(lines) + "\n")
        expected = path.read_bytes()
        manifest = rd.prepare_bundle(source, "4h", self.root / "bundle")
        self.assertEqual(len(manifest["quality"]["volume_discrepancies"]), 1)
        self.assertEqual(path.read_bytes(), expected)


if __name__ == "__main__":
    unittest.main()

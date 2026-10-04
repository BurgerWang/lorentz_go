import copy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).parent))
import optimize as runner


def defaults():
    return dict(algorithm="original-online", source="close", neighbors=8, max_bars_back=2000,
                feature_count=5, features=[dict(name=n, a=a, b=b) for n, a, b in
                [("RSI",14,1),("WT",10,11),("CCI",20,1),("ADX",20,2),("RSI",9,1)]],
                include_full_history=False, use_volatility_filter=True, use_regime_filter=True,
                use_adx_filter=False, regime_threshold=-.1, adx_threshold=20,
                use_ema_filter=False, ema_period=200, use_sma_filter=False, sma_period=200,
                use_kernel_filter=True, kernel_smoothing=False, kernel_h=8, kernel_r=8.,
                kernel_x=25, kernel_lag=2, use_dynamic_exits=False, min_vote_fraction=0., sample_stride=4)


class FakeTrial:
    def __init__(self, values):
        self.values = values
        self.names = []

    def pick(self, name, fallback):
        self.names.append(name)
        return self.values.get(name, fallback)

    def suggest_categorical(self, name, choices):
        return self.pick(name, choices[0])

    def suggest_int(self, name, low, high, step=1):
        return self.pick(name, low)

    def suggest_float(self, name, low, high, step=None):
        return self.pick(name, low)


class SpaceTests(unittest.TestCase):
    def test_full_history_is_searchable_and_defaults_are_enqueued(self):
        t = FakeTrial({"include_full_history": True})
        c = runner.sample_config(t, defaults())
        self.assertTrue(c["include_full_history"])
        self.assertIn("include_full_history", t.names)
        self.assertFalse(runner.default_parameters(defaults())["include_full_history"])

    def test_dynamic_excludes_incompatible_filters_and_smoothing(self):
        trial = FakeTrial({"exit_mode": "dynamic", "use_kernel_filter": False})
        c = runner.sample_config(trial, defaults())
        self.assertFalse(c["use_ema_filter"])
        self.assertFalse(c["use_sma_filter"])
        self.assertFalse(c["kernel_smoothing"])
        for name in ("use_ema_filter", "ema_period", "use_sma_filter", "sma_period", "kernel_smoothing", "kernel_lag"):
            self.assertNotIn(name, trial.names)
        self.assertIn("kernel_h", trial.names)

    def test_inactive_fields_and_slots_preserve_defaults(self):
        d = defaults()
        trial = FakeTrial({"feature_count": 2})
        c = runner.sample_config(trial, d)
        self.assertEqual(c["features"][2:], d["features"][2:])
        for key in ("regime_threshold", "adx_threshold", "ema_period", "sma_period", "kernel_h", "kernel_r", "kernel_x"):
            self.assertEqual(c[key], d[key])
            self.assertNotIn(key, trial.names)
        self.assertFalse(c["kernel_smoothing"])
        self.assertEqual(c["kernel_lag"], 2)
        self.assertEqual(d, defaults())

    def test_adx_b_fixed_and_domains_distinct(self):
        t = FakeTrial({"feature_1_name": "ADX", "feature_2_name": "WT"})
        c = runner.sample_config(t, defaults())
        self.assertEqual(c["features"][0]["b"], 1)
        self.assertNotIn("feature_1_ADX_b", t.names)
        self.assertIn("feature_2_WT_b", t.names)
        self.assertNotIn("feature_2_RSI_b", t.names)

    def test_default_mapping_is_same_suggestion_names(self):
        d = defaults()
        params = runner.default_parameters(d)
        c = runner.sample_config(FakeTrial(params), d)
        expected = copy.deepcopy(d)
        expected["features"][3]["b"] = 1
        self.assertEqual(c, expected)
        self.assertNotIn("feature_4_ADX_b", params)
        self.assertEqual(params["feature_4_name"], "ADX")

    def test_fixed_kernel_smoothing_only_when_kernel_enabled(self):
        t = FakeTrial({"use_kernel_filter": True, "kernel_smoothing": True})
        c = runner.sample_config(t, defaults())
        self.assertEqual(c["kernel_lag"], 1)
        self.assertIn("kernel_lag", t.names)


class PolicyTests(unittest.TestCase):
    def test_strict_eligibility(self):
        baseline = {"aggregate": dict(net_return_pct=2., win_rate_pct=50.)}
        plan = dict(min_trades=30, min_positive_folds=2)
        good = {"aggregate": dict(net_return_pct=3., win_rate_pct=51., trades=30, positive_folds=2)}
        self.assertTrue(runner.eligible(good, baseline, plan))
        for key, value in [("net_return_pct",2.),("net_return_pct",0.),("win_rate_pct",50.),("trades",29),("positive_folds",1)]:
            bad = copy.deepcopy(good)
            bad["aggregate"][key] = value
            self.assertFalse(runner.eligible(bad, baseline, plan))
        self.assertTrue(all(v <= 0 for v in runner.constraint_values(good, baseline, plan).values()))

    def test_contract_mismatch_refused(self):
        runner.verify_contract({"seed": 42}, {"seed": 42})
        with self.assertRaisesRegex(RuntimeError, "run contract differs"):
            runner.verify_contract({"seed": 42}, {"seed": 43})

    def test_total_budget_failed_and_running_count_waiting_does_not(self):
        states = ["COMPLETE", "FAIL", "RUNNING", "WAITING"]
        trials = [SimpleNamespace(state=SimpleNamespace(name=s)) for s in states]
        self.assertEqual(runner.remaining_budget(trials, 5), 2)
        self.assertEqual(runner.remaining_budget(trials, 2), 0)
        self.assertEqual(runner.remaining_budget(trials, 3), 0)

    def test_atomic_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "summary.json"
            runner.atomic_json(path, {"a": 1})
            self.assertEqual(json.loads(path.read_text()), {"a": 1})
            self.assertFalse(path.with_name(path.name + ".tmp").exists())


class OptunaTests(unittest.TestCase):
    def test_actual_resume_and_recovery_budget(self):
        try:
            import optuna
        except ImportError:
            self.skipTest("Optuna not installed")
        study = optuna.create_study(directions=["maximize", "maximize"])
        study.enqueue_trial({"neighbors": 8})
        self.assertEqual(runner.remaining_budget(study.trials, 3), 3)
        study.optimize(lambda t: (1., 2.), n_trials=1)
        running = study.ask()
        runner.recover_running(study, optuna)
        self.assertEqual(study.trials[running.number].state.name, "FAIL")
        remaining = runner.remaining_budget(study.trials, 3)
        self.assertEqual(remaining, 1)
        study.optimize(lambda t: (1., 2.), n_trials=remaining)
        self.assertEqual(len(study.trials), 3)
        self.assertEqual(runner.remaining_budget(study.trials, 3), 0)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        try:
            import optuna
        except ImportError:
            self.skipTest("Optuna not installed")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.plan = self.root / "plan.json"
        self.plan.write_text("{}")
        self.binary = self.root / "fake-evaluator"
        ready = dict(type="ready", protocol_version=1, default_config=defaults(),
                     plan=dict(interval="4h", min_trades=30, min_positive_folds=2), data_identity="test",
                     metadata=dict(funding_rows=100, funding_estimated_rows=25))
        self.binary.write_text(f'''#!{sys.executable}
import json, sys
print({json.dumps(json.dumps(ready))}, flush=True)
for line in sys.stdin:
    request = json.loads(line)
    c = request['config']
    improved = c['neighbors'] != 8
    response = dict(type='result', id=request['id'], config=c, folds=[], elapsed_seconds=0.01,
        aggregate=dict(net_return_pct=2. if improved else 1., win_rate_pct=51. if improved else 50.,
        trades=30, wins=15, positive_folds=2, worst_fold_net_return_pct=0.1, max_fold_drawdown_pct=1.))
    print(json.dumps(response), flush=True)
''')
        self.binary.chmod(0o755)
        self.out = self.root / "out"
        self.args = ["--plan", str(self.plan), "--binary", str(self.binary), "--out", str(self.out), "--trials", "3"]

    def test_controller_resume_and_contract_and_complete_config_exports(self):
        self.assertEqual(runner.main(self.args), 0)
        summary = json.loads((self.out / "summary.json").read_text())
        self.assertEqual(summary["trial_states"], {"COMPLETE": 3})
        self.assertEqual(summary["baseline"]["config"], defaults())
        self.assertEqual(summary["data_metadata"], {"funding_rows": 100, "funding_estimated_rows": 25})
        self.assertTrue(summary["eligible_pareto_candidates"])
        for candidate in summary["eligible_pareto_candidates"]:
            self.assertEqual(candidate["config_file"], f"candidate-trial-{candidate['trial']}.json")
            exported = json.loads((self.out / candidate["config_file"]).read_text())
            self.assertEqual(set(exported), set(defaults()))
        self.assertEqual(runner.main(self.args), 0)
        self.assertEqual(json.loads((self.out / "summary.json").read_text())["trial_count"], 3)
        with self.assertRaisesRegex(RuntimeError, "run contract differs"):
            runner.main(self.args + ["--seed", "43"])

    def test_real_named_constraints(self):
        import optuna
        study = optuna.create_study(directions=["maximize", "maximize"])
        def obj(trial):
            trial.set_constraint("min_trades", 1.)
            return 1., 2.
        study.optimize(obj, n_trials=1)
        self.assertEqual(study.trials[0].constraints, {"min_trades": 1.})


    def test_evaluation_errors_count_towards_total(self):
        source = self.binary.read_text()
        source = source.replace("    print(json.dumps(response), flush=True)",
            "    if request['id'] > 1:\n        response = dict(type='error', id=request['id'], error_kind='evaluation', error='deliberate failure')\n    print(json.dumps(response), flush=True)")
        self.binary.write_text(source)
        self.assertEqual(runner.main(self.args), 0)
        summary = json.loads((self.out / "summary.json").read_text())
        self.assertEqual(summary["trial_states"], {"FAIL": 3})
        self.assertEqual(summary["eligible_pareto_candidates"], [])
        self.assertIn("no eligible candidate", summary["candidate_status"])
        self.assertEqual(runner.main(self.args), 0)
        self.assertEqual(json.loads((self.out / "summary.json").read_text())["trial_count"], 3)

    def test_config_errors_abort_and_close_child(self):
        source = self.binary.read_text().replace("    print(json.dumps(response), flush=True)",
            "    response = dict(type='error', id=request['id'], error_kind='config', error='bug')\n    print(json.dumps(response), flush=True)")
        self.binary.write_text(source)
        server = runner.GoServer(self.binary, self.plan)
        try:
            with self.assertRaises(runner.ProtocolError):
                server.evaluate(defaults())
        finally:
            server.close()
        self.assertIsNotNone(server.process.poll())


class SeedResumeTests(unittest.TestCase):
    def setUp(self):
        try:
            import optuna
        except ImportError:
            self.skipTest("Optuna not installed")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def open_study(self, path, mode, seed=42, startup=3):
        import optuna
        storage = optuna.storages.JournalStorage(optuna.storages.journal.JournalFileBackend(str(path)))
        return optuna.create_study(study_name="seed-test", storage=storage,
            sampler=runner.make_sampler(seed, startup, mode), directions=["maximize", "maximize"], load_if_exists=True)

    @staticmethod
    def objective(trial):
        c = runner.sample_config(trial, defaults())
        trial.set_constraint("min_trades", -1.)
        # Deterministic fake objectives exercise startup and history-dependent TPE.
        return float(c["neighbors"] + c["features"][0]["a"]), float(c["feature_count"])

    def test_journal_continuous_equals_split_resume_for_both_modes(self):
        for mode in ("independent", "grouped"):
            with self.subTest(mode=mode):
                continuous = self.open_study(self.root / (mode + "-continuous.journal"), mode)
                continuous.enqueue_trial(runner.default_parameters(defaults()))
                continuous.optimize(self.objective, n_trials=12)
                split_path = self.root / (mode + "-split.journal")
                split = self.open_study(split_path, mode)
                split.enqueue_trial(runner.default_parameters(defaults()))
                split.optimize(self.objective, n_trials=5)
                resumed = self.open_study(split_path, mode)
                resumed.optimize(self.objective, n_trials=runner.remaining_budget(resumed.trials, 12))
                expected = [(t.params, t.values, t.constraints) for t in continuous.trials]
                actual = [(t.params, t.values, t.constraints) for t in resumed.trials]
                self.assertEqual(actual, expected)
                self.assertEqual(len(actual), 12)

    def test_interrupted_trial_consumes_seed_and_next_number(self):
        import optuna
        for mode in ("independent", "grouped"):
            with self.subTest(mode=mode):
                path = self.root / (mode + "-failure.journal")
                study = self.open_study(path, mode, startup=100)
                interrupted = study.ask()
                runner.sample_config(interrupted, defaults())
                first_params = dict(interrupted.params)
                resumed = self.open_study(path, mode, startup=100)
                runner.recover_running(resumed, optuna)
                resumed.optimize(self.objective, n_trials=1)
                next_trial = resumed.trials[1]
                self.assertEqual(resumed.trials[0].state.name, "FAIL")
                reference = optuna.create_study(sampler=runner.make_sampler(43, 100, mode),
                    directions=["maximize", "maximize"])
                reference.optimize(self.objective, n_trials=1)
                self.assertEqual(next_trial.params, reference.trials[0].params)
                self.assertNotEqual(first_params, next_trial.params)

    def test_seed_bounds_rejected_before_launch(self):
        for seed in ("-1", "4294967296"):
            with self.assertRaises(SystemExit):
                runner.main(["--plan", "missing", "--out", "missing", "--seed", seed])


if __name__ == "__main__":
    unittest.main()

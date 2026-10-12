"""Production-gate tests on disposable synthetic responses/configurations only."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import os
from pathlib import Path
import tempfile
import unittest

import enhanced_space as api
from optimize import EvaluationError, ProtocolError
from robust_budget import Budget, BudgetExhausted, BudgetHalted, cache_identity
import robust_search as rs
from test_enhanced_space import ParameterTrial


def plan():
    return {'version': 'robust-eval-v1', 'bundle_dir': '/tmp/isolated-synthetic-bundle',
            'interval': '1h', 'history_start': '2020-01-01T00:00:00Z',
            'windows': rs.windows('2024-01-01T00:00:00Z'), 'account_mode': 'fold-reset-v1',
            'funding_mode': 'proxy-stress-v1', 'funding_scenarios': ['central', 'proxy-adverse'],
            'cost_multipliers': [1.5]}


def response(p=None, config=None, returns=None):
    p = deepcopy(p or plan())
    returns = returns or [3] * len(p['windows'])
    return {'type': 'result', 'protocol_version': 3, 'plan': p, 'config': deepcopy(config),
            'accounts': [{'window': deepcopy(w), 'funding_scenario': s, 'cost_multiplier': cost,
                          'metrics': {'net_return_pct': returns[i], 'max_drawdown_pct': 5, 'trades': 20}}
                         for i, w in enumerate(p['windows']) for s in p['funding_scenarios'] for cost in p['cost_multipliers']],
            'ledger_evaluations': len(p['windows']) * len(p['funding_scenarios']) * len(p['cost_multipliers'])}


def contract():
    return {'data_use': 'synthetic', 'binary_identity': 'synthetic-only', 'bundle_identity': 'disposable'}


def budget(folder, global_trials=4608):
    return Budget(Path(folder) / 'budget.json', contract(),
                  {'trial_attempts': global_trials, 'go_requests': 4608, 'ledger_evaluations': 55296},
                  rs.search_limits(('W1',)))


class RobustMathTests(unittest.TestCase):
    def test_calendar_objective_and_both_scenario_gates(self):
        w = rs.windows('2024-01-01T00:00:00Z')
        self.assertEqual(w[0]['start'], '2021-01-01T00:00:00Z')
        self.assertEqual(w[-1]['end'], '2024-01-01T00:00:00Z')
        self.assertEqual(sum((rs.utc(x['end']) - rs.utc(x['start'])).days for x in w), 1095)
        p = rs.windows('2024-07-01T00:00:00Z')
        self.assertEqual(sum((rs.utc(x['end']) - rs.utc(x['start'])).days for x in p), 1096)
        self.assertEqual(len(rs.origin_schedule()), 5)
        with self.assertRaises(ValueError):
            rs.windows('2024-01-02T00:00:00Z')
        r = response()
        self.assertTrue(rs.objective(r)['feasible'])
        # Higher central return does not exempt its trades/DD from the gates.
        r['accounts'][0]['metrics'].update(net_return_pct=4, trades=9, max_drawdown_pct=26)
        score = rs.objective(r)
        self.assertFalse(score['feasible'])
        self.assertIn('fold_trades:central', score['violations'])
        self.assertIn('drawdown:central', score['violations'])
        r = response(returns=[-10, 3, 3, 3, 3, 3])
        self.assertTrue(rs.objective(r)['feasible'])
        r = response(returns=[-10.01, 3, 3, 3, 3, 3])
        self.assertIn('worst_fold', rs.objective(r)['violations'])
        r = response(returns=[0, 0, 0, 3, 3, 3])
        self.assertIn('positive_folds', rs.objective(r)['violations'])
        r = response()
        r['accounts'][1]['metrics']['trades'] = 0
        self.assertFalse(rs.objective(r)['feasible'])

    def test_protocol_and_nonpositive_equity_are_distinct(self):
        r = response()
        r['accounts'].append(deepcopy(r['accounts'][0]))
        with self.assertRaises(ProtocolError):
            rs.objective(r)
        r = response()
        r['accounts'][1]['metrics']['net_return_pct'] = -100
        with self.assertRaises(EvaluationError):
            rs.objective(r)
        r = response()
        r['accounts'][0]['metrics']['max_drawdown_pct'] = float('nan')
        with self.assertRaises(ProtocolError):
            rs.objective(r)

    def test_journal_uncommitted_tail_recovery_preserves_prefix(self):
        # Exercise nativeOptuna resume, not just static JSON truncation.
        import optuna
        binary=Path(os.environ.get('LORENTZ_TEST_BINARY',Path(__file__).resolve().parents[1]/'bin/lorentz-robust-v1'))
        defaults=api.go_defaults(binary)
        with tempfile.TemporaryDirectory(prefix='lorentz-journal-tail-') as tmp:
            root=Path(tmp);b=budget(root);called=[]
            def evaluator(p,c):called.append(c);return response(p,c)
            rs.run_origin(root/'search',plan(),defaults,evaluator,b,contract(),until_attempts=1)
            journal=next((root/'search').rglob('study.journal'));prefix=journal.read_bytes()
            with journal.open('ab') as f:f.write(b'{"op_code":')
            rs.run_origin(root/'search',plan(),defaults,evaluator,b,contract(),until_attempts=2)
            self.assertTrue(journal.read_bytes().startswith(prefix));self.assertEqual(len(called),2)
            self.assertEqual(b.counts()['counts']['trial_attempts'],2)
            # A committed malformed line must be rejected, never trimmed away.
            with journal.open('ab') as f:f.write(b'not-json\n')
            before=journal.read_bytes()
            with self.assertRaises(ValueError):rs.run_origin(root/'search',plan(),defaults,evaluator,b,contract(),until_attempts=3)
            self.assertEqual(journal.read_bytes(),before);self.assertEqual(len(called),2)
    def test_recursive_cutoff(self):
        source = {'study': '/tmp/isolated-synthetic', 'trial': 0, 'stage': 'S1', 'family': rs.FAMILIES[0], 'score_windows': plan()['windows'],
                  'parents': [], 'data_use': 'synthetic', 'config_identity': 'x'}
        rs.check_cutoff(source, '2024-01-01T00:00:00Z')
        child = deepcopy(source)
        child['parents'] = [deepcopy(source)]
        child['parents'][0]['score_windows'][-1]['end'] = '2025-01-01T00:00:00Z'
        with self.assertRaises(ValueError):
            rs.check_cutoff(child, '2024-01-01T00:00:00Z')

    def test_continuous_contract(self):
        p = plan()
        p['account_mode'] = 'fixed-continuous-v1'
        p['windows'] = [{'name': 'training-continuous', 'start': p['windows'][0]['start'], 'end': p['windows'][-1]['end']}]
        p['cost_multipliers'] = [1, 1.5, 2]
        r = response(p)
        self.assertTrue(rs.continuous_check(r)['passed'])
        r['accounts'][5]['metrics']['net_return_pct'] = 0
        self.assertTrue(rs.continuous_check(r)['passed'])
        r['accounts'][4]['metrics']['net_return_pct'] = 0
        self.assertFalse(rs.continuous_check(r)['passed'])


class RobustBudgetTests(unittest.TestCase):
    def test_atomic_global_substudy_cache_and_no_unknown_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            limits = {'trial_attempts': 10, 'go_requests': 2, 'ledger_evaluations': 24}
            b = Budget(Path(temp) / 'budget.json', contract(), limits, {'A': limits, 'B': limits})
            with ThreadPoolExecutor(max_workers=8) as pool:
                def charge(i):
                    try:
                        b.reserve_attempt('A' if i % 2 else 'B', str(i))
                        return True
                    except BudgetExhausted:
                        return False
                self.assertEqual(sum(pool.map(charge, range(20))), 10)
            self.assertEqual(b.snapshot()['counts']['trial_attempts'], 10)
            first = next(iter(b.snapshot()['records']))
            study = b.record(first)['study']
            key = cache_identity(plan(), {'synthetic': 1})
            self.assertTrue(b.reserve_evaluation(study, first, key, 12)['send'])
            b.finish(first, response=response(config={'synthetic': 1}))
            second = next(x for x in b.snapshot()['records'] if x != first)
            hit = b.reserve_evaluation(b.record(second)['study'], second, key, 12)
            self.assertFalse(hit['send'])
            self.assertEqual(hit['response']['config'], {'synthetic': 1})
            third = next(x for x in b.snapshot()['records'] if x not in (first, second))
            third_study = b.record(third)['study']
            b.reserve_evaluation(third_study, third, 'different', 12)
            b.recover(third_study)
            with self.assertRaises(ValueError):
                b.reserve_evaluation(third_study, third, 'different', 12)
            s = b.snapshot()
            self.assertEqual(s['counts']['go_requests'], 2)
            self.assertEqual(s['counts']['ledger_evaluations'], 24)
            self.assertEqual(b.record(third)['status'], 'interrupted')
            with self.assertRaises(ValueError):
                Budget(Path(temp) / 'budget.json', {'changed': True}, limits, {'A': limits, 'B': limits})

    def test_fixed_requests_do_not_charge_trials_or_cache_failures(self):
        with tempfile.TemporaryDirectory() as temp:
            limits = {'trial_attempts': 0, 'go_requests': 2, 'ledger_evaluations': 6}
            b = Budget(Path(temp) / 'budget.json', contract(), limits, {'fixed': limits})
            b.reserve_evaluation('fixed', 'x', 'key', 3, fixed=True)
            b.finish('x', error='timeout')
            self.assertTrue(b.reserve_evaluation('fixed', 'y', 'key', 3, fixed=True)['send'])
            self.assertEqual(b.snapshot()['counts']['trial_attempts'], 0)
            b.halt('immutable data changed')
            with self.assertRaises(BudgetHalted):
                b.reserve_evaluation('fixed', 'z', 'key', 3, fixed=True)


class RobustConfigurationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.binary = Path(os.environ.get('LORENTZ_TEST_BINARY', str(Path(__file__).resolve().parents[1] / 'bin/lorentz-enhanced-optuna')))
        cls.defaults = api.go_defaults(cls.binary)

    def config(self, seed=0, forced=None, family=rs.FAMILIES[1], stage='S3'):
        spec = rs.wide_space(family, self.defaults['daily']['history_start'])
        spec['stage'] = stage
        return api.sample_config(ParameterTrial(seed, forced), self.defaults, spec)

    def candidate(self, config, trial=0, stage='C1', values=(.1, .08), seed=17, group='classic'):
        return {'config': config, 'config_identity': api.identity(config),
                'source': {'study': '/tmp/isolated-synthetic', 'origin': 'W1', 'family': config['classifier']['family'], 'seed': seed, 'stage': stage,
                           'group': group if stage == 'C1' else None, 'trial': trial, 'score_windows': plan()['windows'],
                           'parents': [], 'data_use': 'synthetic', 'config_identity': api.identity(config)},
                'complexity': rs.complexity(config, self.defaults),
                'score': {'feasible': True, 'values': list(values), 'max_drawdown_pct': 5}}

    def test_semantic_shrink_and_no_c2_fallback(self):
        cs = []
        for i, (kind, value) in enumerate((('RSI', 10), ('RSI', 12), ('RSI', 14), ('ADX', 30), ('ADX', 35), ('ADX', 40))):
            c = self.config(i, {'feature_count': 2, 'f0_type': kind, 'f0_' + kind + '_a': value,
                                'f1_type': 'WT', 'feature_group': 'classic'})
            cs.append(self.candidate(c, i, 'C2' if i == 5 else 'C1'))
        frozen = rs.shrink_domains(cs, self.defaults)
        self.assertEqual(frozen['domains']['f0_RSI_a'], {'low': 9, 'high': 15, 'step': 1})
        self.assertEqual(frozen['domains']['f0_ADX_a'], {'low': 29, 'high': 40, 'step': 1})
        self.assertNotIn('f0_type', frozen['domains'])
        self.assertNotIn('feature_count', frozen['domains'])
        self.assertIsNone(rs.shrink_domains(cs[:-1], self.defaults))
        self.assertEqual(frozen, rs.shrink_domains(list(reversed(cs)), self.defaults))

    def test_profile_clipping_and_c2_structure_seed_compiler(self):
        p = plan()
        p['interval'] = '1d'
        profile = rs.compile_search_profile(p, '2019-11-27')
        self.assertEqual(profile['max_bars_back_domain'], [500, 1000])
        self.assertEqual(profile['available_bars'], 1461)
        self.assertEqual(profile['history_end'], '2024-01-01T00:00:00Z')
        domain = rs._intersect_domains({}, profile)
        a = self.config(2, {'feature_group': 'classic-d1-slope'}, stage='S1')
        b = self.config(3, {'feature_group': 'classic-kernel-deviation'}, stage='S1')
        chosen = [self.candidate(a), self.candidate(b, 1)]
        spec = {'version': api.VERSION, 'family': a['classifier']['family'], 'stage': 'S2',
                'daily_history_start': a['daily']['history_start'], 'domains': {'anchor_control': [False]},
                'anchors': [{k: c[k] for k in ('config', 'config_identity', 'source')} for c in chosen]}
        configs = []
        for anchor in spec['anchors']:
            for entry in ('main', 'pullback', 'both'):
                for risk in ('off', 'atr', 'trail', 'breakeven', 'trail-breakeven'):
                    t = ParameterTrial(len(configs), {'anchor': anchor['config_identity'], 'entry_mode': entry, 'risk_mode': risk})
                    c = api.sample_config(t, self.defaults, spec)
                    configs.append(c)
                    self.assertEqual(c['entry_mode'], entry)
                    self.assertEqual(c['risk']['enabled'], risk != 'off')
                    self.assertEqual(c['classifier'], anchor['config']['classifier'])
        self.assertEqual(len(configs), 30)
        api.go_validate(self.binary, configs)
        spec = rs.wide_space(rs.FAMILIES[0], self.defaults['daily']['history_start'])
        spec['domains'] = domain
        for seed in range(10):
            c = api.sample_config(ParameterTrial(seed), self.defaults, spec)
            self.assertIn(c['classic']['max_bars_back'], [500, 1000])
        # C3 may shrink values but never expand the legal pre-cutoff grid.
        self.assertEqual(rs._intersect_domains({'max_bars_back': [1000, 2000]}, profile)['max_bars_back'], [1000])
        with self.assertRaises(ValueError):
            rs._intersect_domains({'max_bars_back': [4000]}, profile)

    def test_anchor_main_seed_and_source_ties(self):
        c = self.config()
        a = self.candidate(c, trial=10, group='classic-rvol')
        b = deepcopy(a)
        b['source']['trial'] = 10
        b['source']['group'] = 'classic'
        distinct = self.candidate(self.config(1), trial=2, values=(.3, .07))
        chosen = rs.anchors([a, b, distinct])
        self.assertEqual(chosen[0]['source']['group'], 'classic')
        self.assertEqual(chosen[1]['config_identity'], distinct['config_identity'])
        self.assertEqual(len(rs.deduplicate([a, b])[0]['sources']), 2)
        self.assertIsNone(rs.central_candidate([]))
        for item in chosen:
            item.update(local_passed=True, continuous_passed=True)
        selection = rs.select_flows(chosen)
        self.assertEqual(selection['main']['config_identity'], chosen[0]['config_identity'])
        self.assertIsNone(selection['seeds']['42'])

    def test_deterministic_wide_neighbors_and_full_risk_contract(self):
        c = self.config(13, {'feature_count': 3, 'f0_type': 'RSI', 'f1_type': 'WT', 'f2_type': 'CCI',
                            'feature_group': 'classic-rvol-atr-price', 'entry_mode': 'both', 'risk_mode': 'trail-breakeven',
                            'daily_filter': True, 'exit_policy': 'four-bars', 'age_mode': 'rank', 'kernel_filter': True})
        frozen = rs.neighbors(c, self.defaults, 'W1')
        self.assertEqual(frozen, rs.neighbors(c, self.defaults, 'W1'))
        self.assertEqual(len(frozen['points']), 24)
        self.assertEqual(frozen['selected_keys'][:6], ['neighbors', 'f0_RSI_a', 'rvol_period', 'kernel_h', 'envelope_h', 'risk_atr_multiplier'])
        self.assertTrue(frozen['uncovered_keys'])
        api.go_validate(self.binary, [p['config'] for p in frozen['points']])
        for point in frozen['points']:
            x = point['config']
            self.assertEqual(x['entry_mode'], c['entry_mode'])
            self.assertEqual(x['risk']['enabled'], c['risk']['enabled'])
            self.assertEqual(x['risk']['trail'], c['risk']['trail'])
            self.assertEqual(x['exit']['policy'], c['exit']['policy'])
            self.assertEqual(x['classifier']['feature_group']['name'], c['classifier']['feature_group']['name'])
            self.assertNotEqual(point['config_identity'], api.identity(c))
        results = [response(config=p['config']) for p in frozen['points']]
        center = response(config=c)
        self.assertTrue(rs.neighborhood_check(center, frozen, results)['passed'])
        for i in range(5):
            results[i] = None
        result = rs.neighborhood_check(center, frozen, results)
        self.assertFalse(result['passed'])
        self.assertEqual(result['N'], 24)
        self.assertEqual(result['required_points'], 20)
        for i in range(13):
            results[i] = None
        self.assertIsNone(rs.neighborhood_check(center, frozen, results)['median_neighbor_growth'])
        self.assertFalse(rs.neighborhood_check(center, {**frozen, 'points': frozen['points'][:11]}, results[:11])['passed'])

    def test_native_optuna_pause_resume_unknown_and_budget_boundary(self):
        import optuna
        optuna.logging.set_verbosity(optuna.logging.ERROR)
        with tempfile.TemporaryDirectory() as temp:
            b = budget(temp, global_trials=3)
            calls = []
            def evaluate(p, c):
                calls.append(api.identity(c))
                return response(p, c)
            args = (Path(temp) / 'search', plan(), self.defaults, evaluate, b, contract())
            first = rs.run_origin(*args, until_attempts=1)
            self.assertEqual(first['budget']['counts']['trial_attempts'], 1)
            self.assertEqual(len(calls), 1)
            rs.run_origin(*args, until_attempts=1)
            self.assertEqual(len(calls), 1)
            final = rs.run_origin(*args)
            self.assertEqual(final['budget']['counts']['trial_attempts'], 3)
            self.assertEqual(len(calls), 3)
            self.assertEqual(len(final['eligible_candidates']), 3)
            self.assertEqual(final['status'], 'paused')
            folder = Path(temp) / 'search' / rs.FAMILIES[0] / '17' / 'C1-classic'
            storage = optuna.storages.JournalStorage(optuna.storages.journal.JournalFileBackend(str(folder / 'study.journal')))
            st = optuna.load_study(study_name=rs.study_name('W1', rs.FAMILIES[0], 17, 'C1', 'classic'), storage=storage)
            self.assertEqual(len(st.trials), 3)  # no uncharged ask at the global budget boundary
        with tempfile.TemporaryDirectory() as temp:
            b = budget(temp)
            calls = []
            def interrupted(p, c):
                calls.append(c)
                raise KeyboardInterrupt()
            with self.assertRaises(KeyboardInterrupt):
                rs.run_origin(Path(temp) / 'search', plan(), self.defaults, interrupted, b, contract(), until_attempts=1)
            self.assertEqual(b.snapshot()['counts']['ledger_evaluations'], 12)
            def resumed(p, c):
                calls.append(c)
                return response(p, c)
            result = rs.run_origin(Path(temp) / 'search', plan(), self.defaults, resumed, b, contract(), until_attempts=2)
            self.assertEqual(len(calls), 2)
            self.assertEqual(result['budget']['counts']['go_requests'], 2)
            self.assertEqual(len(result['eligible_candidates']), 1)
            self.assertTrue(any(r['status'] == 'interrupted' for r in b.snapshot()['records'].values()))
        with tempfile.TemporaryDirectory() as temp:
            b = budget(temp)
            name = rs.study_name('W1', rs.FAMILIES[0], 17, 'C1', 'classic')
            b.reserve_attempt(name, name + '/0')  # interrupted before native ask
            calls = []
            def resumed(p, c):
                calls.append(c)
                return response(p, c)
            result = rs.run_origin(Path(temp) / 'search', plan(), self.defaults, resumed, b, contract(), until_attempts=2)
            self.assertEqual(len(calls), 1)
            self.assertEqual(result['budget']['counts']['trial_attempts'], 2)

    def test_lost_budget_with_failed_journal_is_rejected(self):
        import optuna
        optuna.logging.set_verbosity(optuna.logging.ERROR)
        with tempfile.TemporaryDirectory() as temp:
            b = budget(temp)
            def ordinary(p, c):
                raise EvaluationError('synthetic insufficient initialization')
            output = Path(temp) / 'search'
            rs.run_origin(output, plan(), self.defaults, ordinary, b, contract(), until_attempts=2)
            # Delete only the disposable synthetic budget to emulate recovery
            # state loss; journals retain two already-consumed failed attempts.
            for suffix in ('', '-wal', '-shm'):
                path = Path(str(b.path) + suffix)
                if path.exists():
                    path.unlink()
            fresh = budget(temp)
            calls = []
            def must_not_run(p, c):
                calls.append(c)
                return response(p, c)
            with self.assertRaises(ProtocolError):
                rs.run_origin(output, plan(), self.defaults, must_not_run, fresh, contract(), until_attempts=3)
            self.assertFalse(calls)
            self.assertIn('persisted charged attempt', fresh.halted())

    def test_native_ordinary_failure_and_fatal_halt(self):
        import optuna
        optuna.logging.set_verbosity(optuna.logging.ERROR)
        with tempfile.TemporaryDirectory() as temp:
            b = budget(temp)
            def ordinary(p, c):
                raise EvaluationError('synthetic equity protection')
            result = rs.run_origin(Path(temp) / 'search', plan(), self.defaults, ordinary, b, contract(), until_attempts=2)
            self.assertIsNone(rs.central_candidate(result['eligible_candidates']))
            self.assertEqual(result['budget']['counts']['trial_attempts'], 2)
            self.assertFalse(result['budget']['halted'])
        with tempfile.TemporaryDirectory() as temp:
            b = budget(temp)
            def fatal(p, c):
                raise ProtocolError('synthetic source changed')
            result = rs.run_origin(Path(temp) / 'search', plan(), self.defaults, fatal, b, contract(), until_attempts=2)
            self.assertEqual(result['budget']['counts']['trial_attempts'], 1)
            self.assertEqual(result['status'], 'halted')
            self.assertIn('source changed', result['budget']['halted'])


class RobustRealGoControllerTests(unittest.TestCase):
    """Full production plan shape, bounded native trials on invented OHLC only."""
    @classmethod
    def setUpClass(cls):
        import robust_fixture
        import robust_data
        cls.binary = Path(os.environ.get('LORENTZ_ROBUST_TEST_BINARY', str(Path(__file__).resolve().parents[1] / 'bin/lorentz-robust-v1')))
        if not cls.binary.exists():
            raise unittest.SkipTest('new robust Go binary not built')
        cls.tmp = tempfile.TemporaryDirectory(prefix='lorentz-robust-search-go-')
        cls.root = Path(cls.tmp.name)
        source = robust_fixture.make_source(cls.root / 'source', days=1462, proxy=True)
        cls.bundle = cls.root / 'bundle'
        cls.manifest = robust_data.prepare_bundle(source, '4h', cls.bundle, 'proxy-stress-v1', source / 'mark.json')
        cls.defaults = api.go_defaults(cls.binary)
        cls.p = plan()
        cls.p.update(bundle_dir=str(cls.bundle), interval='4h')
        from robust_client import inspect
        cls.ready = inspect(cls.binary, cls.p)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_real_go_optuna_checkpoint_and_unknown_killed_subprocess(self):
        import optuna
        import robust_client
        optuna.logging.set_verbosity(optuna.logging.ERROR)
        self.assertEqual(self.ready['ledger_evaluations'], 12)
        self.assertTrue(self.manifest['synthetic'])
        c = {'data_use': 'synthetic', 'binary_identity': __import__('hashlib').sha256(self.binary.read_bytes()).hexdigest(),
             'bundle_identity': self.ready['bundle_identity']}
        limits = {'trial_attempts': 4608, 'go_requests': 4608, 'ledger_evaluations': 55296}
        b = Budget(self.root / 'actual-budget.json', c, limits, rs.search_limits(('W1',)))
        calls = []
        def actual(p, config):
            calls.append(api.identity(config))
            return robust_client.evaluate(self.binary, p, config, timeout=600)
        output = self.root / 'actual-search'
        first = rs.run_origin(output, self.p, self.defaults, actual, b, c, until_attempts=2)
        self.assertEqual(first['budget']['counts']['trial_attempts'], 2)
        self.assertEqual(len(calls), 2)
        second = rs.run_origin(output, self.p, self.defaults, actual, b, c, until_attempts=3)
        self.assertEqual(second['budget']['counts']['go_requests'], 3)
        self.assertEqual(second['budget']['counts']['ledger_evaluations'], 36)
        self.assertEqual(len(calls), 3)
        binding = __import__('json').loads((output / 'run-contract.json').read_text())
        self.assertEqual(binding['quotas'], rs.QUOTAS)
        self.assertEqual(binding['gates'], rs.GATES)
        self.assertEqual(binding['startup'], rs.STARTUP)
        # Force an actual evaluator process death after stdin send. Its full
        # reservation is consumed and the interrupted native trial is never retried.
        killed_budget = Budget(self.root / 'killed-budget.json', c, limits, rs.search_limits(('W1',)))
        killed_out = self.root / 'killed-search'
        def killed(p, config):
            pp = self.root / 'killed-plan.json'
            pp.write_text(api.canonical(p))
            server = robust_client.Server(self.binary, pp)
            try:
                server.process.stdin.write((api.canonical({'id': 1, 'config': config, 'trace_dir': ''}) + '\n').encode())
                server.process.stdin.flush()
                server.process.kill()
                server.process.wait(timeout=10)
                raise KeyboardInterrupt('synthetic subprocess interrupted after send')
            finally:
                server.close()
        with self.assertRaises(KeyboardInterrupt):
            rs.run_origin(killed_out, self.p, self.defaults, killed, killed_budget, c, until_attempts=1)
        before = killed_budget.snapshot()
        self.assertEqual(before['counts']['ledger_evaluations'], 12)
        after = rs.run_origin(killed_out, self.p, self.defaults, actual, killed_budget, c, until_attempts=2)
        self.assertEqual(after['budget']['counts']['trial_attempts'], 2)
        self.assertEqual(after['budget']['counts']['go_requests'], 2)
        self.assertEqual(after['budget']['counts']['ledger_evaluations'], 24)
        records = after['budget']['records']
        self.assertEqual(records[next(iter(records))]['status'], 'interrupted')
        self.assertEqual(len(calls), 4)



if __name__ == '__main__':
    unittest.main()

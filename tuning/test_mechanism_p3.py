"""P3 contract/approval/recovery tests and isolated synthetic Go integration."""
from copy import deepcopy
from pathlib import Path
import hashlib
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import enhanced_space as space
from feature_groups import classifier_config
import mechanism_v2 as v2
import mechanism_p3 as p3
import mechanism_p3_client as client
from robust_budget import Budget, BudgetHalted, atomic_json
from robust_data import prepare_bundle
from robust_fixture import make_source, plan
from robust_trace import verify_account

BINARY = v2.ROOT/'bin/lorentz-mechanism-p3'
OLD_BINARY = v2.ROOT/'bin/lorentz-robust-v1'
SOURCE = v2.ROOT/'results/mechanism-v2/ETHUSDT/1h/p1-prepared'


class Contract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.defaults = space.go_defaults(OLD_BINARY)

    def test_audited_source_matrix_and_complete_variant_identity(self):
        source = p3.read_source(SOURCE)
        jobs = p3.matrix(source)
        self.assertEqual(len(jobs), 10)
        self.assertEqual(len({j['config_identity'] for j in jobs}), 8)
        client.go_validate(BINARY, [j['config'] for j in jobs])
        for skeleton in ('A', 'B'):
            group = [j for j in jobs[:8] if j['skeleton'] == skeleton]
            self.assertEqual([j['config']['variant'] for j in group], list(client.VARIANTS))
            for j in group:
                self.assertEqual(j['config']['strategy'], source['originals'][skeleton]['config'])
        self.assertEqual(jobs[-2]['replay_of'], 'A-classic-original')
        self.assertEqual(jobs[-1]['replay_of'], 'B-classic-original')
        self.assertTrue(source['w1_rejection_unchanged'])
        bad = client.wrapper(self.defaults, 'classic-original'); bad['extra'] = 0
        with self.assertRaises(client.ProtocolError): client.go_validate(BINARY, [bad])
        bad = client.wrapper(self.defaults, 'classic-recent'); bad['strategy']['classic']['sample_stride'] = 8
        with self.assertRaises(client.ProtocolError): client.go_validate(BINARY, [bad])

    def test_separate_approval_and_engineering_audit_barrier(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); atomic_json(root/'manifest.json', {})
            approval = {'version': 'mechanism-p3-approval-v1', 'manifest_sha256': p3.sha256(root/'manifest.json'),
                        'approved': False, 'authorized_by': 'user', 'authorization_reference': 'isolated synthetic test',
                        'batch': 'P3', 'budget': p3.LIMITS, 'funding_modes': ['proxy-stress-v1']}
            atomic_json(root/'approval.json', approval)
            with patch.object(p3, 'load_run', return_value={'contract': {}}), patch.object(client, 'Evaluator', side_effect=AssertionError('must not start')):
                with self.assertRaises(ValueError): p3.run(root, root/'approval.json')
                approval['approved'] = True; approval['batch'] = 'P1'; atomic_json(root/'approval.json', approval)
                with self.assertRaises(ValueError): p3.run(root, root/'approval.json')
                approval['batch'] = 'P3'; approval['budget'] = {**p3.LIMITS, 'go_requests': 11}; atomic_json(root/'approval.json', approval)
                with self.assertRaises(ValueError): p3.run(root, root/'approval.json')
                approval['budget'] = p3.LIMITS; atomic_json(root/'approval.json', approval)
                atomic_json(root/'engineering-audit.json', {'audit_status': 'pending', 'findings': []})
                with self.assertRaises(ValueError): p3.run(root, root/'approval.json')
            self.assertFalse((root/'execution').exists())

    def test_persistent_halt_and_unknown_no_retry(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); execution = root/'execution'; execution.mkdir()
            c = {'engine': {'path': str(BINARY)}, 'jobs': []}
            b = Budget(execution/'budget.sqlite', {'manifest_contract_sha256': space.identity(c)}, p3.LIMITS, {'P3': p3.LIMITS})
            b.reserve_evaluation('P3', 'P3/unknown', 'unknown-key', 6, fixed=True)
            self.assertEqual(b.recover('P3'), ['P3/unknown'])
            with patch.object(client, 'evaluate', side_effect=AssertionError('no retry')):
                self.assertIsNone(p3.request(root, c, b, {'name': 'unknown'}, None))
            self.assertEqual(b.counts()['counts']['go_requests'], 1)
            b.reserve_evaluation('P3', 'P3/complete', 'complete-key', 6, fixed=True)
            b.finish('P3/complete', response={'synthetic': True})
            b.halt('synthetic final protocol fault')
            for name in ('unknown', 'complete'):
                with self.assertRaises(BudgetHalted): p3.request(root, c, b, {'name': name}, None)
            with patch.object(p3, 'authorize', return_value={'contract': c}), patch.object(client, 'Evaluator', side_effect=AssertionError('halted')):
                with self.assertRaises(BudgetHalted): p3.run(root, root/'synthetic-approval.json')
            self.assertEqual(b.counts()['counts']['go_requests'], 2)

    def test_variant_cache_budget_and_fault_does_not_get_promoted(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            c = {'plan': {'synthetic': True}, 'engine': {'path': str(BINARY)}}
            b = Budget(root/'budget.sqlite', c, p3.LIMITS, {'P3': p3.LIMITS})
            jobs = [{'name': v, 'role': 'fixed-control', 'config': client.wrapper(self.defaults, v)}
                    for v in ('classic-original', 'classic-recent')]
            jobs[0]['skeleton'] = 'A'
            atomic_json(root/'lineage.json', {'originals': {'A': {'config': self.defaults, 'accounts': []}}})
            calls = []
            def evaluate(_plan, config, trace): calls.append(config['variant']); return {'config': config, 'accounts': []}
            with patch.object(p3, 'verified_result', side_effect=lambda c, r, config: r), patch.object(v2, 'resources_ready'):
                p3.request(root, c, b, jobs[0], evaluate)
                p3.request(root, c, b, jobs[0], evaluate)
                p3.request(root, c, b, jobs[1], evaluate)
            self.assertEqual(calls, ['classic-original', 'classic-recent'])
            self.assertEqual(b.counts()['counts'], {'trial_attempts': 0, 'go_requests': 2, 'ledger_evaluations': 12})
            fault = {'name': 'bad-state', 'role': 'fixed-control', 'config': client.wrapper(self.defaults, 'rq-direction')}
            with patch.object(v2, 'resources_ready'), patch.object(p3, 'verified_result', side_effect=client.ProtocolError('state mismatch')):
                with self.assertRaises(client.ProtocolError): p3.request(root, c, b, fault, evaluate)
            self.assertIsNotNone(b.halted())
            self.assertEqual(b.record('P3/bad-state')['status'], 'failed')
            self.assertEqual(b.counts()['counts']['go_requests'], 3)

    def test_terminal_and_new_cache_identity_fault_persistently_halt(self):
        for hit in ('existing-complete', 'new-cache-alias', 'corrupted-complete-json', 'corrupted-alias-json'):
            with self.subTest(hit=hit), tempfile.TemporaryDirectory() as td:
                root = Path(td); config = client.wrapper(self.defaults, 'rq-direction')
                c = {'plan': {'synthetic': True}}
                b = Budget(root/'budget.sqlite', c, p3.LIMITS, {'P3': p3.LIMITS})
                key = p3.cache_identity(c['plan'], config)
                b.reserve_evaluation('P3', 'P3/source', key, 6, fixed=True)
                b.finish('P3/source', response={'synthetic': True})
                corrupted = hit.startswith('corrupted')
                if corrupted:
                    with sqlite3.connect(root/'budget.sqlite') as db:
                        db.execute('UPDATE cache SET response=? WHERE key=?', ('{corrupt', key))
                name = 'source' if hit in ('existing-complete', 'corrupted-complete-json') else 'alias'
                job = {'name': name, 'role': 'fixed-control', 'config': config}
                with patch.object(p3, 'verified_result', side_effect=client.ProtocolError('cached state trace identity differs')):
                    with self.assertRaises((client.ProtocolError, ValueError)): p3.request(root, c, b, job, None)
                self.assertIsNotNone(b.halted())
                self.assertEqual(b.record('P3/source')['status'], 'complete')
                if hit != 'corrupted-alias-json': self.assertEqual(b.record('P3/'+name)['status'], 'complete')
                else: self.assertIsNone(b.record('P3/alias'))
                self.assertEqual(b.counts()['counts']['go_requests'], 1)

    def test_incompatible_first_original_stops_before_next_request_and_replay(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            originals = {'A': {'config': deepcopy(self.defaults), 'marker': 'preserved', 'accounts': []},
                         'B': {'config': deepcopy(self.defaults), 'marker': 'preserved', 'accounts': []}}
            originals['B']['config']['classic']['source'] = 'hlc3'
            source = {'originals': originals}
            atomic_json(root/'lineage.json', source)
            c = {'engine': {'path': str(BINARY)}, 'plan': {'synthetic': True}, 'jobs': p3.matrix(source)}
            calls = []
            def evaluate(_plan, config, trace):
                calls.append(config); return {'config': config, 'marker': 'incompatible', 'accounts': []}
            with patch.object(p3, 'authorize', return_value={'contract': c}), patch.object(p3, 'load_run', return_value={'contract': c}), patch.object(v2, 'resources_ready'), patch.object(p3, 'verified_result', side_effect=lambda c, r, cfg: r), patch.object(client, 'Evaluator') as factory:
                factory.return_value.__enter__.return_value = evaluate
                with self.assertRaisesRegex(client.ProtocolError, 'preserved P1'): p3.run(root, root/'synthetic-approval.json')
            b = Budget(root/'execution/budget.sqlite', {'manifest_contract_sha256': space.identity(c)}, p3.LIMITS, {'P3': p3.LIMITS})
            self.assertEqual(len(calls), 1)
            self.assertEqual(b.counts()['counts']['go_requests'], 1)
            self.assertEqual(b.record('P3/A-classic-original')['status'], 'failed')
            self.assertIsNotNone(b.halted())
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); cfg = client.wrapper(self.defaults, 'rq-direction'); c = {'plan': {'synthetic': True}, 'engine': {'path': str(BINARY)}}
            b = Budget(root/'budget.sqlite', c, p3.LIMITS, {'P3': p3.LIMITS})
            key = p3.cache_identity(c['plan'], cfg)
            b.reserve_evaluation('P3', 'P3/base', key, 6, fixed=True)
            b.finish('P3/base', response={'config': cfg, 'marker': 'original', 'accounts': []})
            replay = {'name': 'replay', 'role': 'independent-replay', 'replay_of': 'base', 'config': cfg}
            with patch.object(v2, 'resources_ready'), patch.object(p3, 'verified_result', side_effect=lambda c, r, cfg: r), patch.object(client, 'evaluate', return_value={'config': cfg, 'marker': 'different', 'accounts': []}):
                with self.assertRaisesRegex(client.ProtocolError, 'replay differs'): p3.request(root, c, b, replay, None)
            self.assertEqual(b.record('P3/replay')['status'], 'failed')
            self.assertIsNotNone(b.halted())

    def test_live_frozen_identity_failure_persists_halt_before_request(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); atomic_json(root/'lineage.json', {})
            c = {'engine': {'path': str(BINARY)}, 'jobs': [{'name': 'first'}]}
            with patch.object(p3, 'authorize', return_value={'contract': c}), patch.object(p3, 'load_run', side_effect=ValueError('live frozen identity differs')), patch.object(client, 'Evaluator') as factory:
                factory.return_value.__enter__.return_value = lambda *args: self.fail('must stop before request')
                with self.assertRaisesRegex(ValueError, 'live frozen identity'): p3.run(root, root/'synthetic-approval.json')
            b = Budget(root/'execution/budget.sqlite', {'manifest_contract_sha256': space.identity(c)}, p3.LIMITS, {'P3': p3.LIMITS})
            self.assertIsNotNone(b.halted())
            self.assertEqual(b.counts()['counts']['go_requests'], 0)

    def test_increment_gate_needs_both_baselines_scenarios_and_breadth(self):
        from test_mechanism_metrics import scoring_summaries
        configs = {'originals': {s: {'config': deepcopy(self.defaults)} for s in ('A', 'B')}, 'source_keys': {'A': ['synthetic', 'A'], 'B': ['synthetic', 'B']}}
        configs['originals']['B']['config']['classic']['source'] = 'hlc3'
        jobs = p3.matrix(configs)
        reports = []
        for j in jobs[:8]:
            rows = scoring_summaries()
            gain = 3 if j['config']['variant'] == 'classic-original' else 4 if j['config']['variant'] == 'classic-recent' else 1
            for a in rows:
                for h in a['half_years']: h['net_return_pct'] = gain
                a['metrics']['net_return_pct'] = 100*((1+gain/100)**6-1)
            reports.append({'name': j['name'], 'accounts': rows, 'v2_development_score': p3.continuous_score(rows)})
        result = p3.incremental_report(reports, jobs, configs)
        self.assertEqual([x['name'] for x in result['selected_research_objects']], ['A-classic-recent', 'B-classic-recent'])
        # Better total but only three halves not worse in one scenario rejects.
        a = next(r for r in reports if r['name'] == 'A-classic-recent')
        account = next(x for x in a['accounts'] if x['funding_scenario'] == 'proxy-adverse' and x['cost_multiplier'] == 1.5)
        for i, h in enumerate(account['half_years']): h['net_return_pct'] = 0.5 if i < 3 else 8
        account['metrics']['net_return_pct'] = 100*((1.005**3)*(1.08**3)-1)
        a['v2_development_score'] = p3.continuous_score(a['accounts'])
        result = p3.incremental_report(reports, jobs, configs)
        self.assertFalse(next(x for x in result['incremental_eligibility'] if x['name'] == 'A-classic-recent')['eligible'])
        self.assertEqual(result['selected_research_objects'][0]['name'], 'A-classic-original')
        partial = p3.incremental_report([r for r in reports if 'momentum-four' not in r['name']], jobs, configs)
        self.assertTrue(partial['stop_lorentz_local_search']); self.assertFalse(partial['P4_authorized'])


class SyntheticIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='lorentz-p3-synthetic-'); cls.root = Path(cls.tmp.name)
        source = make_source(cls.root/'source', days=740, proxy=True)
        cls.bundle = cls.root/'bundle'; prepare_bundle(source, '1h', cls.bundle, 'proxy-stress-v1', source/'mark.json')
        cls.plan = plan(cls.bundle, start_day=730, end_day=736, proxy=True)
        cls.cfg = space.go_defaults(OLD_BINARY)
        cls.cfg['classic'].update(max_bars_back=80, neighbors=4, use_volatility_filter=False, use_regime_filter=False, use_kernel_filter=False)
        cls.cfg['classifier'] = classifier_config(cls.cfg['classic'], 'classic', family='classic-extended')
        cls.cfg = space.normalize_config(cls.cfg, space.go_defaults(OLD_BINARY))

    @classmethod
    def tearDownClass(cls): cls.tmp.cleanup()

    def test_01_all_variants_inspect_and_protocol_ledger_old_original_replay(self):
        # 4 variants + old binary + independent original replay = 6 synthetic
        # Go requests / 36 synthetic accounts; no real source is evaluated.
        self.__class__.responses = {}
        with client.Evaluator(BINARY) as evaluator:
            for variant in client.VARIANTS:
                w = client.wrapper(self.cfg, variant)
                ready = client.inspect(BINARY, self.plan, w)
                self.assertEqual(ready['evaluation_calls'], 0)
                self.assertEqual(ready['ledger_evaluations'], 6)
                result = evaluator(self.plan, w, self.root/variant)
                self.assertTrue(client.verify_state_trace(result)['verified'])
                for account in result['accounts']:
                    self.assertTrue(verify_account(account, self.bundle)['verified'])
                self.responses[variant] = result
        old = client.base.evaluate(OLD_BINARY, self.plan, self.cfg, self.root/'old-original')
        self.assertEqual(space.canonical(p3.economic_response(self.responses['classic-original'], legacy=True)),
                         space.canonical(v2.economic_response(old)))
        replay = client.evaluate(BINARY, self.plan, client.wrapper(self.cfg, 'classic-original'), self.root/'replay-original')
        self.assertEqual(space.canonical(p3.economic_response(replay)), space.canonical(p3.economic_response(self.responses['classic-original'])))

    def test_02_state_missing_and_valid_hash_expired_index_rejected(self):
        r = deepcopy(self.responses['classic-recent'])
        r['mechanism_trace']['path'] = str(self.root/'absent.jsonl')
        with self.assertRaises(client.ProtocolError): client.verify_state_trace(r)
        original = Path(self.responses['classic-recent']['mechanism_trace']['path'])
        rows = [json.loads(line) for line in original.read_text().splitlines()]
        row = rows[100]
        row['queue_indices'] = [1]; row['neighbors'] = 1; row['max_neighbor_index'] = 1
        fake = self.root/'expired.jsonl'; fake.write_text(''.join(json.dumps(x)+'\n' for x in rows))
        r['mechanism_trace']['path'] = str(fake)
        r['mechanism_trace']['sha256'] = hashlib.sha256(fake.read_bytes()).hexdigest()
        with self.assertRaisesRegex(client.ProtocolError, 'expired'): client.verify_state_trace(r)
        r = deepcopy(self.responses['classic-original']); r['config']['variant'] = 'classic-recent'
        with self.assertRaises(client.ProtocolError): client.validate_result(r, self.plan, r['config'], {'plan': self.plan, 'bundle_identity': r['bundle_identity'], 'ledger_evaluations': 6})


if __name__ == '__main__': unittest.main()

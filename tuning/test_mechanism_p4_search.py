"""Isolated fake-evaluator durability tests: no real historical evaluation."""
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch

import optuna

import mechanism_p4_search as search
from enhanced_space import canonical, identity
from robust_budget import Budget, BudgetHalted
from robust_client import EvaluationError, normalize_plan

optuna.logging.set_verbosity(optuna.logging.ERROR)
NAMES = ['total_trades', 'half_trades', 'drawdown', 'positive_halves', 'worst_half', 'compound_return']


def analysis(response):
    c = response['config']['strategy']
    quality = sum(c['x'+str(i)] for i in range(4)) + .1 * sum(c['m'+str(i)] for i in range(4))
    vector = [1.7 if c['x0'] == 0 else 0., 0., 0., 0., 0., 0.]
    score = {'constraint_names': NAMES, 'constraint_vector': vector,
             'violations': dict(zip(NAMES, vector)), 'constraint_sum': sum(vector),
             'max_violation': max(vector), 'feasible': all(v == 0 for v in vector),
             'values': [quality, quality / 2], 'max_drawdown_pct': 5.}
    return {'accounts': [{'synthetic': True, 'scenario': x} for x in ('central', 'proxy-adverse')], 'score': score}


def fake_domain(single=False):
    d = ModuleType('mechanism_p4_domain')
    def validate(c, rules):
        expected = deepcopy(rules['anchor'])
        for block in ('classifier', 'management'):
            for key in rules['blocks'][block]['keys']:
                if c['strategy'][key] not in rules['blocks'][block]['domains'][key]:
                    raise ValueError('domain')
                expected['strategy'][key] = c['strategy'][key]
        if expected != c:
            raise ValueError('frozen field')
        return c
    def parameters(c, rules, block):
        validate(c, rules)
        return {key: c['strategy'][key] for key in rules['blocks'][block]['keys']}
    def compile_config(base, rules, block, params):
        c = deepcopy(base)
        c['strategy'].update(params)
        return validate(c, rules)
    d.validate_config, d.parameters, d.compile_config = validate, parameters, compile_config
    d.rank_key = lambda c: (-c['score']['values'][1], -c['score']['values'][0], c['score']['max_drawdown_pct'],
                           int(not c.get('anchor_preferred', False)), identity(c['config']))
    return d


def fixture(root, quota=3, single=False):
    cfg = {'version': 'mechanism-p3-config-v1', 'variant': 'classic-original',
           'strategy': {key: 1 for p in ('x', 'm') for key in [p+str(i) for i in range(4)]}}
    rules = {'anchor': deepcopy(cfg), 'blocks': {
        block: {'keys': [prefix+str(i) for i in range(4)],
                'domains': {prefix+str(i): ([1] if single else [0, 1, 2]) for i in range(4)}}
        for block, prefix in [('classifier', 'x'), ('management', 'm')]}}
    plan = {'version': 'robust-eval-v1', 'bundle_dir': '/tmp/p4-fake-bundle-no-market-access',
            'interval': '1h', 'history_start': '2020-01-01T00:00:00Z',
            'windows': [{'name': 'continuous', 'start': '2021-01-01T00:00:00Z', 'end': '2024-01-01T00:00:00Z'}],
            'account_mode': 'fixed-continuous-v1', 'funding_mode': 'proxy-stress-v1',
            'funding_scenarios': ['central', 'proxy-adverse'], 'cost_multipliers': [1.5]}
    response = {'plan': plan, 'config': cfg, 'synthetic': True}
    a = analysis(response)
    anchor = {'config': deepcopy(cfg), 'score': a['score'], 'analysis': a, 'response': response,
              'source': {'kind': 'closed-p3-synthetic-reference'}}
    contract = {'binding_identity': 'synthetic-p4-binding', 'plan': plan, 'domains': {'A': rules},
                'seeds': [17], 'anchors': {'A': anchor}, 'startup_trials': 16}
    limits = {'trial_attempts': quota, 'go_requests': quota, 'ledger_evaluations': quota * 2}
    substudy = {f'P4/A/17/{block}': deepcopy(limits) for block in ('classifier', 'management')}
    budget = Budget(root/'budget.sqlite', {'manifest_contract_sha256': contract['binding_identity']},
                    {k: 2*v for k, v in limits.items()}, substudy)
    calls = []
    def evaluator(plan, config, trace_dir):
        calls.append((deepcopy(plan), deepcopy(config), str(trace_dir)))
        return {'plan': normalize_plan(plan), 'config': config, 'synthetic': True, 'trace_dir': str(trace_dir)}
    return contract, budget, evaluator, calls


def study(root, block='classifier'):
    storage = optuna.storages.JournalStorage(optuna.storages.journal.JournalFileBackend(
        str(root/'execution/search/A/17'/block/'study.journal')))
    return optuna.load_study(study_name=f'P4/A/17/{block}', storage=storage)


def fingerprint(root):
    return {block: [(t.number, t.state.name, t.params, t.values, t.constraints,
                     t.user_attrs.get('execution', {}).get('charged')) for t in study(root, block).trials]
            for block in ('classifier', 'management')}


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='lorentz-p4-synthetic-')
        self.root = Path(self.tmp.name)
        self.domain = fake_domain()
        self.modules = patch.dict(sys.modules, {'mechanism_p4_domain': self.domain})
        self.modules.start()

    def tearDown(self):
        self.modules.stop()
        self.tmp.cleanup()

    def run_search(self, root, contract, budget, evaluator, quota, until=None, checkpoint=None):
        return search.run_search(root/'execution', contract, budget, evaluator, analysis,
                                 checkpoint=checkpoint, until_attempts=until, quota=quota)

    def test_uninterrupted_vs_segmented_per_trial_tpe(self):
        a, b = self.root/'whole', self.root/'segmented'
        ca, ba, ea, calls_a = fixture(a, quota=20)
        cb, bb, eb, calls_b = fixture(b, quota=20)
        whole = self.run_search(a, ca, ba, ea, 20)
        for cutoff in (3, 9, 17, 24, 40):
            segmented = self.run_search(b, cb, bb, eb, 20, cutoff)
        self.assertEqual(whole['status'], 'complete')
        self.assertEqual(segmented['status'], 'complete')
        self.assertEqual(fingerprint(a), fingerprint(b))
        self.assertEqual(whole['branches'][0]['selected']['config'], segmented['branches'][0]['selected']['config'])
        self.assertEqual(ba.counts(), bb.counts())
        self.assertEqual(len(calls_a), len(calls_b))
        # management freezes all classifier values of the selected first block.
        selected = whole['branches'][0]['classifier_selected']['config']['strategy']
        for t in study(a, 'management').trials:
            if t.state.name == 'COMPLETE':
                for i in range(4):
                    self.assertEqual(t.user_attrs['config']['strategy']['x'+str(i)], selected['x'+str(i)])

    def test_cached_duplicates_consume_attempts_and_never_refill(self):
        c, b, e, calls = fixture(self.root, single=True)
        result = self.run_search(self.root, c, b, e, 3)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(len(calls), 1)
        self.assertEqual(b.counts()['counts'], {'trial_attempts': 6, 'go_requests': 1, 'ledger_evaluations': 2})
        self.assertEqual(result['branches'][0]['selected']['source']['kind'], 'prior-anchor')
        for t in study(self.root, 'classifier').trials:
            self.assertEqual(set(t.constraints), set(NAMES))
        before = fingerprint(self.root)
        journals = {block: (self.root/'execution/search/A/17'/block/'study.journal').read_bytes() for block in search.BLOCKS}
        with patch.object(b, 'finish', side_effect=AssertionError('terminal budget row finished again')):
            self.run_search(self.root, c, b, e, 3)
        self.assertEqual(fingerprint(self.root), before)
        self.assertEqual(len(calls), 1)
        for block in search.BLOCKS:
            self.assertEqual((self.root/'execution/search/A/17'/block/'study.journal').read_bytes(), journals[block])

    def test_reservation_before_ask_crash_preserves_number_as_fail(self):
        c, b, e, calls = fixture(self.root)
        original = b.reserve_attempt
        def crash(*args):
            original(*args)
            raise KeyboardInterrupt()
        with patch.object(b, 'reserve_attempt', side_effect=crash):
            with self.assertRaises(KeyboardInterrupt):
                self.run_search(self.root, c, b, e, 3)
        self.assertEqual(study(self.root).trials[0].state.name, 'WAITING')
        result = self.run_search(self.root, c, b, e, 3, until=1)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(study(self.root).trials[0].state.name, 'FAIL')
        self.assertEqual(b.record('P4/A/17/classifier/0')['status'], 'interrupted')
        self.assertEqual(len(calls), 0)
        self.assertEqual(b.counts()['counts']['trial_attempts'], 1)

    def test_completed_go_before_tell_recovers_cache_without_send(self):
        c, b, e, calls = fixture(self.root)
        original = b.finish
        def crash(*args, **kwargs):
            original(*args, **kwargs)
            if kwargs.get('response') is not None:
                raise KeyboardInterrupt()
        with patch.object(b, 'finish', side_effect=crash):
            with self.assertRaises(KeyboardInterrupt):
                self.run_search(self.root, c, b, e, 3)
        self.assertEqual(study(self.root).trials[0].state.name, 'RUNNING')
        self.assertEqual(b.record('P4/A/17/classifier/0')['status'], 'complete')
        self.run_search(self.root, c, b, e, 3, until=1)
        self.assertEqual(len(calls), 1)
        native = study(self.root).trials[0]
        self.assertEqual(native.state.name, 'COMPLETE')
        self.assertEqual(native.constraints, dict(zip(NAMES, [0.]*6)))

    def test_cache_hit_complete_before_native_metadata_recovers_without_send(self):
        c, b, e, calls = fixture(self.root, single=True)
        self.run_search(self.root, c, b, e, 3, until=1)
        original = b.reserve_evaluation
        def crash(*args, **kwargs):
            result = original(*args, **kwargs)
            if not result['send']:
                raise KeyboardInterrupt()
            return result
        with patch.object(b, 'reserve_evaluation', side_effect=crash):
            with self.assertRaises(KeyboardInterrupt):
                self.run_search(self.root, c, b, e, 3, until=2)
        native = study(self.root).trials[1]
        self.assertEqual(native.state.name, 'RUNNING')
        self.assertNotIn('source', native.user_attrs)
        self.assertEqual(b.record('P4/A/17/classifier/1')['status'], 'complete')
        self.run_search(self.root, c, b, e, 3, until=2)
        native = study(self.root).trials[1]
        self.assertEqual(native.state.name, 'COMPLETE')
        self.assertEqual(native.user_attrs['source']['kind'], 'p4-cache')
        self.assertEqual(native.user_attrs['source']['cache_source_token'], 'P4/A/17/classifier/0')
        self.assertEqual(len(calls), 1)

    def test_unknown_request_and_identical_later_attempts_never_retry(self):
        c, b, e, calls = fixture(self.root, single=True)
        def crash(plan, config, trace):
            calls.append(config)
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.run_search(self.root, c, b, crash, 3)
        self.assertEqual(b.counts()['counts']['go_requests'], 1)
        self.run_search(self.root, c, b, e, 3)
        self.assertEqual(len(calls), 1)
        self.assertEqual(b.counts()['counts']['trial_attempts'], 6)
        self.assertTrue(all(t.state.name == 'FAIL' for block in search.BLOCKS for t in study(self.root, block).trials))
        self.assertEqual(b.record('P4/A/17/classifier/0')['status'], 'interrupted')

    def test_ordinary_evaluation_and_compile_failures_retain_prior_anchor(self):
        c, b, _, calls = fixture(self.root, single=True)
        def failed(*args):
            calls.append(args)
            raise EvaluationError('synthetic insufficient/equity failure')
        r = self.run_search(self.root, c, b, failed, 3)
        self.assertEqual(r['branches'][0]['selected']['source']['kind'], 'prior-anchor')
        self.assertEqual(len(calls), 1)
        self.assertIsNone(b.halted())
        other = self.root/'compile'
        c, b, e, calls = fixture(other)
        with patch.object(self.domain, 'compile_config', side_effect=ValueError('synthetic compile rejection')):
            r = self.run_search(other, c, b, e, 3)
        self.assertEqual(len(calls), 0)
        self.assertEqual(b.counts()['counts']['trial_attempts'], 6)
        self.assertIsNone(b.halted())
        self.assertEqual(r['branches'][0]['selected']['source']['kind'], 'prior-anchor')

    def test_protocol_halt_prechecks_before_resume_mutations(self):
        c, b, e, _ = fixture(self.root)
        def wrong(plan, config, trace):
            r = e(plan, config, trace)
            r['config'] = dict(config, variant='rq-direction')
            return r
        with self.assertRaises(search.SearchHalted):
            self.run_search(self.root, c, b, wrong, 3)
        self.assertIsNotNone(b.halted())
        path = self.root/'execution/search/A/17/classifier/study.journal'
        journal = path.read_bytes()
        with patch.object(b, 'recover', side_effect=AssertionError('halted recovery mutation')):
            with self.assertRaises(BudgetHalted):
                self.run_search(self.root, c, b, e, 3, checkpoint=lambda: self.fail('halted checkpoint invoked'))
        self.assertEqual(path.read_bytes(), journal)

    def test_completed_cache_tamper_durably_halts_without_request(self):
        c, b, e, calls = fixture(self.root)
        self.run_search(self.root, c, b, e, 3, until=1)
        key = b.record('P4/A/17/classifier/0')['cache_key']
        changed = b.cached_response(key)
        changed['config']['strategy']['x0'] = 2
        with closing(sqlite3.connect(b.path)) as db:
            db.execute('UPDATE cache SET response=? WHERE key=?', (canonical(changed), key))
            db.commit()
        with self.assertRaises(search.SearchHalted):
            self.run_search(self.root, c, b, e, 3)
        self.assertEqual(len(calls), 1)
        self.assertIsNotNone(b.halted())

    def test_lost_budget_and_truncated_journal_halt(self):
        c, b, e, calls = fixture(self.root)
        self.run_search(self.root, c, b, e, 3, until=1)
        with closing(sqlite3.connect(b.path)) as db:
            db.execute("DELETE FROM records WHERE token='P4/A/17/classifier/0'")
            db.commit()
        with self.assertRaises(search.SearchHalted):
            self.run_search(self.root, c, b, e, 3)
        self.assertEqual(len(calls), 1)
        other = self.root/'tail'
        c, b, e, calls = fixture(other)
        self.run_search(other, c, b, e, 3, until=1)
        path = other/'execution/search/A/17/classifier/study.journal'
        with path.open('ab') as f:
            f.write(b'{"unterminated":')
        before = path.read_bytes()
        with self.assertRaises(search.SearchHalted):
            self.run_search(other, c, b, e, 3)
        self.assertEqual(before, path.read_bytes())
        self.assertEqual(len(calls), 1)

    def test_wrong_binding_or_account_plan_halts_before_native_creation(self):
        for case in ('binding', 'plan'):
            root = self.root/case
            c, b, e, calls = fixture(root)
            if case == 'binding':
                c['binding_identity'] = 'wrong-manifest-binding'
            else:
                c['plan']['account_mode'] = 'fold-reset-v1'
            with self.assertRaises(search.SearchHalted):
                self.run_search(root, c, b, e, 3)
            self.assertEqual(len(calls), 0)
            self.assertEqual(b.counts()['counts']['trial_attempts'], 0)
            self.assertFalse((root/'execution/search').exists())
            self.assertIsNotNone(b.halted())

    def test_live_checkpoint_before_each_send_and_resume_verification(self):
        c, b, e, calls = fixture(self.root)
        checks = []
        def checkpoint():
            checks.append(len(calls))
        self.run_search(self.root, c, b, e, 3, until=1, checkpoint=checkpoint)
        self.assertTrue(checks and checks[-1] == 0)
        checks.clear()
        self.run_search(self.root, c, b, e, 3, until=1, checkpoint=checkpoint)
        self.assertTrue(checks and all(n == 1 for n in checks))
        def revoked():
            raise search.ProtocolError('synthetic live helper identity changed')
        with self.assertRaises(search.SearchHalted):
            self.run_search(self.root, c, b, e, 3, checkpoint=revoked)
        self.assertEqual(len(calls), 1)


if __name__ == '__main__':
    unittest.main()

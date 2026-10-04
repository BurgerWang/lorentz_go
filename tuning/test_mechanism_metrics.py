"""Disposable synthetic metrics only; no Go or market-data evaluations."""
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from optimize import EvaluationError, ProtocolError
from robust_search import windows, objective
from robust_data import utc_ms
import mechanism_metrics as mm


def old_result(returns=None):
    ps = windows('2024-01-01T00:00:00Z')
    returns = [3] * 6 if returns is None else returns
    return {'plan': {'windows': ps, 'account_mode': 'fold-reset-v1',
                     'funding_scenarios': ['central', 'proxy-adverse'],
                     'funding_mode': 'proxy-stress-v1', 'cost_multipliers': [1.5]},
            'accounts': [{'window': p, 'funding_scenario': s, 'cost_multiplier': 1.5,
                          'metrics': {'trades': 15, 'net_return_pct': returns[i], 'max_drawdown_pct': 25}}
                         for i, p in enumerate(ps) for s in ('central', 'proxy-adverse')],
            'ledger_evaluations': 12}


def synthetic_account(folder):
    """Sparse economic path for the pure cutter, with mocked source verifier.

    A crosses H1/H2; funding, exit, reentry at H2 start belong to H2. B
    crosses remaining halves and pays its forced endpoint exit. Peaks decay
    across boundaries so global DD exceeds every half's reset-peak DD.
    """
    ps = windows('2024-01-01T00:00:00Z')
    edges = [utc_ms(p['start']) for p in ps] + [utc_ms(ps[-1]['end'])]
    events, trades = [], []
    cash, side, entry, active, trade_id = 1000., 0, 0., None, 0

    def add(phase, t, price, fee=0., funding=0., reason=None):
        nonlocal cash, side, entry, active, trade_id
        gross = 0.
        payload = None
        if phase == 'entry':
            trade_id += 1
            side, entry = 1, price
            active = {'entry_time': t, 'entry_equity': cash, 'entry_price': price,
                      'side': 'long', 'entry_kind': 'main_entry' if trade_id == 1 else 'pullback',
                      'fees': fee, 'funding': 0.}
            cash -= fee
        elif phase == 'funding':
            cash += funding
            active['funding'] += funding
        elif phase == 'exit':
            gross = price - entry
            cash += gross - fee
            payload = {**active, 'exit_time': t, 'exit_price': price, 'gross_pnl': gross,
                       'fees': active['fees'] + fee, 'net_pnl': gross - active['fees'] - fee + active['funding'],
                       'reason': reason}
            payload['return_pct'] = 100 * payload['net_pnl'] / active['entry_equity']
            trades.append(payload)
            side, entry, active = 0, 0., None
        event = {'version': 'ledger-trace-v1', 'sequence': len(events) + 1,
                 'time': t, 'phase': phase, 'sampled': phase != 'funding',
                 'price': price, 'cash': cash, 'equity': cash + side * (price - entry),
                 'direction': side, 'trade_id': trade_id, 'fee': fee,
                 'gross_pnl': gross, 'funding_cash': funding}
        if payload is not None:
            event['trade'] = payload
        events.append(event)

    add('initial', edges[0], 0)
    add('open', edges[0], 100)
    add('entry', edges[0] + 3600000, 100, fee=2)
    add('close', edges[0] + 7200000 - 1, 600)
    add('close', edges[1] - 1, 500)
    add('funding', edges[1], 510, funding=-10)
    add('open', edges[1], 510)
    add('exit', edges[1], 510, fee=3, reason='signal_exit')
    add('entry', edges[1], 510, fee=2)
    for i, price in enumerate((410, 310, 310, 310), 2):
        add('close', edges[i] - 1, price)
    add('exit', edges[-1] - 1, 310, fee=3, reason='end_of_window')
    add('close', edges[-1] - 1, 310)
    peak, dd = 1000., 0.
    for e in events:
        if e['sampled']:
            peak = max(peak, e['equity'])
            dd = max(dd, 100 * (peak - e['equity']) / peak)
    metrics = {'trades': len(trades), 'net_return_pct': 100 * (cash / 1000 - 1),
               'max_drawdown_pct': dd,
               **{k: sum(t[k] for t in trades) for k in ('gross_pnl', 'fees', 'funding')}}
    path = Path(folder) / 'trace.jsonl'
    write_events(path, events)
    account = {'account_mode': 'fixed-continuous-v1',
               'window': {'name': 'continuous', 'start': ps[0]['start'], 'end': ps[-1]['end']},
               'funding_scenario': 'central', 'cost_multiplier': 1.5,
               'trace_path': str(path), 'trace_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
               'metrics': metrics}
    checked = {'verified': True, 'source_completeness_verified': True, 'events': len(events),
               'initial_equity': 1000., 'final_equity': cash, 'metrics': metrics, 'trades': trades}
    return account, checked, events


def write_events(path, events):
    path.write_text(''.join(json.dumps(e, allow_nan=False) + '\n' for e in events))


class OldScoringTests(unittest.TestCase):
    def test_all_thresholds_and_frozen_equivalence(self):
        for returns in ([3] * 6, [-10, 3, 3, 3, 3, 3], [-10.01, 3, 3, 3, 3, 3],
                        [0] * 6, [0, 0, 0, 3, 3, 3], [1e-200] * 6):
            for trades in (9, 10, 14, 15, 16):
                for dd in (24.99, 25, 25.01):
                    r = old_result(returns)
                    for a in r['accounts']:
                        a['metrics'].update(trades=trades, max_drawdown_pct=dd)
                    score = mm.old_fold_score(r)
                    self.assertEqual(score['feasible'], objective(r)['feasible'])
                    self.assertEqual(score['feasible'], all(v == 0 for v in score['constraint_vector']))
        score = mm.old_fold_score(old_result([0] * 6))
        self.assertEqual(score['constraint_vector'][-1], mm.ZERO_LOG_VIOLATION)
        self.assertEqual(mm.old_fold_score(old_result([1e-200] * 6))['constraint_vector'][-1], 0)

    def test_exact_vector_and_discrete_counts(self):
        r = old_result([-20] * 6)
        for a in r['accounts']:
            a['metrics'].update(trades=5, max_drawdown_pct=50)
        v = mm.old_fold_score(r)['constraint_vector']
        for got, want in zip(v[:5], (2 / 3, .5, 1, 1, 1)):
            self.assertAlmostEqual(got, want)
        self.assertAlmostEqual(v[5], -6 * math.log1p(-.2) / math.log(1.10))

    def test_invalid_inputs_fail_instead_of_scoring(self):
        for field, value in (('trades', True), ('trades', -1), ('trades', 1.5),
                             ('net_return_pct', math.nan), ('net_return_pct', math.inf),
                             ('net_return_pct', True), ('max_drawdown_pct', -1),
                             ('max_drawdown_pct', 101)):
            r = old_result()
            r['accounts'][0]['metrics'][field] = value
            with self.assertRaises(ProtocolError):
                mm.old_fold_score(r)
        r = old_result()
        r['accounts'][0]['metrics']['net_return_pct'] = -100
        with self.assertRaises(EvaluationError):
            mm.old_fold_score(r)
        for mutation in ('missing', 'duplicate', 'count', 'mode'):
            r = old_result()
            if mutation == 'missing': r['accounts'].pop()
            if mutation == 'duplicate': r['accounts'].append(deepcopy(r['accounts'][0]))
            if mutation == 'count': r['ledger_evaluations'] = 11
            if mutation == 'mode': r['plan']['account_mode'] = 'fixed-continuous-v1'
            with self.assertRaises(ProtocolError):
                mm.old_fold_score(r)


class ContinuousSummaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.account, self.checked, self.events = synthetic_account(self.tmp.name)

    def summary(self):
        return mm.summarize_continuous_account(self.account, checked=self.checked)

    def mutate_trace(self, events):
        write_events(Path(self.account['trace_path']), events)
        self.account['trace_sha256'] = hashlib.sha256(Path(self.account['trace_path']).read_bytes()).hexdigest()
        self.checked['events'] = len(events)

    def test_boundary_cash_and_trade_ownership_product_and_dd(self):
        report = self.summary()
        halves = report['half_years']
        self.assertEqual([h['trades'] for h in halves], [0, 1, 0, 0, 0, 1])
        self.assertEqual([h['entries'] for h in halves], [1, 1, 0, 0, 0, 0])
        self.assertEqual([h['carry_in'] for h in halves], [0, 1, 1, 1, 1, 1])
        self.assertEqual([h['carry_out'] for h in halves], [1, 1, 1, 1, 1, 0])
        self.assertEqual(halves[0]['fees'], 2)
        self.assertEqual(halves[1]['funding'], -10)
        self.assertEqual(halves[1]['fees'], 5)
        self.assertEqual(halves[5]['fees'], 3)
        self.assertEqual(halves[1]['start_equity'], 1398)
        self.assertEqual(halves[0]['carry_out_trade_ids'], [1])
        self.assertEqual(halves[1]['carry_out_trade_ids'], [2])
        product = math.prod(1 + h['net_return_pct'] / 100 for h in halves)
        self.assertAlmostEqual(product, 1 + report['metrics']['net_return_pct'] / 100)
        self.assertGreater(report['metrics']['max_drawdown_pct'], max(h['max_drawdown_pct'] for h in halves))
        held = sum((t['exit_time'] - t['entry_time']) / 3600000 for t in self.checked['trades'])
        self.assertAlmostEqual(sum(h['exposure_hours'] for h in halves), held)
        self.assertEqual(report['attribution']['by_trade']['entry_kind']['main_entry']['trades'], 1)
        self.assertEqual(report['attribution']['by_trade']['reason']['end_of_window']['trades'], 1)

    def test_calls_real_verifier_when_not_supplied(self):
        with patch.object(mm, 'verify_account', return_value=self.checked) as verify:
            mm.summarize_continuous_account(self.account, bundle='/tmp/synthetic-only')
            verify.assert_called_once_with(self.account, '/tmp/synthetic-only')
        with self.assertRaisesRegex(ValueError, 'requires frozen'):
            mm.summarize_continuous_account(self.account)

    def test_gap_overlap_missing_trace_boundary_and_final_flat_reject(self):
        for offset in (-3600000, 3600000):
            periods = windows('2024-01-01T00:00:00Z')
            periods[1]['start'] = '2021-06-30T23:00:00Z' if offset < 0 else '2021-07-01T01:00:00Z'
            with self.assertRaises(ProtocolError):
                mm.summarize_continuous_account(self.account, periods, checked=self.checked)
        a = deepcopy(self.account)
        a['trace_path'] += '.missing'
        with self.assertRaisesRegex(ProtocolError, 'missing continuous trace'):
            mm.summarize_continuous_account(a, checked=self.checked)
        boundary = utc_ms('2021-07-01T00:00:00Z') - 1
        self.mutate_trace([e for e in self.events if e['time'] != boundary])
        with self.assertRaisesRegex(ProtocolError, 'missing B-1ms'):
            self.summary()
        self.mutate_trace([e for e in self.events if e['time'] != self.events[-1]['time']])
        with self.assertRaises(ProtocolError):
            self.summary()

    def test_trace_identity_unverified_and_wrong_forced_endpoint_reject(self):
        self.account['trace_sha256'] = '0' * 64
        with self.assertRaisesRegex(ProtocolError, 'identity differs'):
            self.summary()
        self.account['trace_sha256'] = hashlib.sha256(Path(self.account['trace_path']).read_bytes()).hexdigest()
        self.checked['source_completeness_verified'] = False
        with self.assertRaises(ProtocolError):
            self.summary()
        self.checked['source_completeness_verified'] = True
        es = deepcopy(self.events)
        es[-2]['trade']['reason'] = 'signal_exit'
        self.mutate_trace(es)
        with self.assertRaisesRegex(ProtocolError, 'endpoint exit must be forced'):
            self.summary()


def scoring_summaries():
    ps = windows('2024-01-01T00:00:00Z')
    return [{'version': mm.VERSION, 'account_mode': 'fixed-continuous-v1',
             'window': {'name': 'continuous', 'start': ps[0]['start'], 'end': ps[-1]['end']},
             'funding_scenario': s, 'cost_multiplier': c,
             'half_years': [{**p, 'trades': 15, 'net_return_pct': 3} for p in ps],
             'metrics': {'trades': 90, 'net_return_pct': 100 * (1.03 ** 6 - 1), 'max_drawdown_pct': 20}}
            for s in ('central', 'proxy-adverse') for c in (1, 1.5, 2)]


class ContinuousScoringTests(unittest.TestCase):
    def test_new_gates_use_full_dd_and_cost_stress(self):
        rows = scoring_summaries()
        self.assertTrue(mm.continuous_score(rows)['feasible'])
        rows[1]['metrics']['max_drawdown_pct'] = 26
        self.assertGreater(mm.continuous_score(rows)['constraint_vector'][2], 0)
        rows[1]['metrics']['max_drawdown_pct'] = 25
        rows[0]['metrics']['max_drawdown_pct'] = 20.01
        score = mm.continuous_score(rows)
        self.assertFalse(score['feasible'])
        self.assertTrue(all(v == 0 for v in score['constraint_vector']))
        self.assertEqual(score['failed_real_accounts'], [{'funding_scenario': 'central', 'cost_multiplier': 1}])

    def test_two_times_flat_allowed_lower_cost_flat_strict_reject(self):
        rows = scoring_summaries()
        for i in (2, 5):
            rows[i]['metrics']['net_return_pct'] = 0
            for h in rows[i]['half_years']:
                h['net_return_pct'] = 0
        self.assertTrue(mm.continuous_score(rows)['feasible'])
        rows[0]['metrics']['net_return_pct'] = 0
        for h in rows[0]['half_years']:
            h['net_return_pct'] = 0
        self.assertFalse(mm.continuous_score(rows)['feasible'])

    def test_matrix_illegal_metrics_and_reconciliation_reject(self):
        rows = scoring_summaries()
        for changed in (rows[:-1], rows + [deepcopy(rows[0])]):
            with self.assertRaises(ProtocolError): mm.continuous_score(changed)
        for scope, field, value in (('metrics', 'trades', True), ('metrics', 'max_drawdown_pct', math.inf),
                                    ('metrics', 'net_return_pct', math.nan), ('half', 'trades', -1),
                                    ('half', 'net_return_pct', False)):
            bad = deepcopy(rows)
            target = bad[0]['half_years'][0] if scope == 'half' else bad[0]['metrics']
            target[field] = value
            with self.assertRaises(ProtocolError): mm.continuous_score(bad)
        bad = deepcopy(rows)
        bad[0]['half_years'][0]['trades'] += 1
        with self.assertRaisesRegex(ValueError, 'trade sum'): mm.continuous_score(bad)
        bad = deepcopy(rows)
        bad[0]['half_years'][0]['net_return_pct'] += 1
        with self.assertRaisesRegex(ValueError, 'return product'): mm.continuous_score(bad)


if __name__ == '__main__':
    unittest.main()

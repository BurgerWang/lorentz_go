"""Mechanism v2 pure scoring and one-account MTM reporting.

No strategy evaluation, fold concatenation, or writes occur here. Returns and
drawdowns in the constraint vector are fractions; public report values are pct.
"""
from bisect import bisect_right
import hashlib
import math
from pathlib import Path
import statistics

from enhanced import strict_loads
from optimize import EvaluationError, ProtocolError
from robust_data import utc_ms
from robust_search import objective, windows, utc
from robust_trace import near, verify_account

VERSION = 'mechanism-metrics-v2'
CONSTRAINT_NAMES = ('total_trades', 'half_trades', 'drawdown',
                    'positive_halves', 'worst_half', 'compound_return')
# Equality alone has a positive violation; positive L is never shifted by eps.
ZERO_LOG_VIOLATION = 1.0


def _finite(value, label):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ProtocolError('nonfinite/invalid ' + label)
    return value


def _count(value, label):
    if type(value) is not int or value < 0:
        raise ProtocolError('invalid count: ' + label)
    return value


def _vector(totals, counts, drawdowns, worse):
    log_return = sum(math.log1p(r) for r in worse)
    compound = (ZERO_LOG_VIOLATION if log_return == 0 else
                max(0., -log_return / math.log(1.10)))
    vector = [max(max(0., (90 - n) / 90) for n in totals),
              max(max(0., (10 - n) / 10) for n in counts),
              max(max(0., (d - .25) / .25) for d in drawdowns),
              max(0., (4 - sum(r > 0 for r in worse)) / 4),
              max(0., (-.10 - min(worse)) / .10), compound]
    return {'constraint_names': list(CONSTRAINT_NAMES), 'constraint_vector': vector,
            'constraint_sum': sum(vector), 'max_violation': max(vector),
            'feasible': all(v == 0 for v in vector), 'envelope_log_return': log_return}


def old_fold_score(result):
    """Validate the frozen v1 contract and add its six graded violations.

    The frozen objective validates the exact calendar/scenario/account matrix,
    metric types and equity protection. Its original fields remain available.
    """
    old = objective(result)
    scenes = result['plan']['funding_scenarios']
    rows = result['accounts']
    totals = [sum(a['metrics']['trades'] for a in rows if a['funding_scenario'] == s)
              for s in scenes]
    score = _vector(totals, [a['metrics']['trades'] for a in rows],
                    [a['metrics']['max_drawdown_pct'] / 100 for a in rows],
                    [r / 100 for r in old['worst_fold_returns_pct']])
    if score['feasible'] != old['feasible']:
        raise ProtocolError('graded score differs from frozen feasibility')
    return {**old, **score, 'score_contract': 'old-fold-six-vector-v2'}


def _periods(account, periods):
    window = account.get('window', {})
    try:
        expected = windows(window['end'])
        if expected[0]['start'] != window['start']:
            raise ProtocolError('continuous account must span 36 calendar months')
        periods = expected if periods is None else periods
        if periods != expected:
            raise ProtocolError('six periods must be contiguous calendar half-years of account window')
        edges = [utc_ms(p['start']) for p in periods] + [utc_ms(periods[-1]['end'])]
    except (KeyError, TypeError, ValueError) as e:
        raise ProtocolError('invalid continuous period contract') from e
    return periods, edges


def _attribution(trades):
    groups = {}
    for field in ('entry_kind', 'side', 'reason'):
        values = {}
        for t in trades:
            key = t[field]
            row = values.setdefault(key, {'trades': 0, 'gross_pnl': 0., 'fees': 0.,
                                         'funding': 0., 'net_pnl': 0., 'holding_hours': 0.})
            row['trades'] += 1
            for k in ('gross_pnl', 'fees', 'funding', 'net_pnl'):
                row[k] += t[k]
            row['holding_hours'] += (t['exit_time'] - t['entry_time']) / 3600000
        groups[field] = values
    wins = sorted((t['net_pnl'] for t in trades if t['net_pnl'] > 0), reverse=True)
    total = sum(wins)
    durations = [(t['exit_time'] - t['entry_time']) / 3600000 for t in trades]
    return {'by_trade': groups,
            'holding_hours': {'median': statistics.median(durations) if durations else None,
                              'max': max(durations) if durations else None,
                              'total': sum(durations)},
            'winning_net_pnl': total,
            'top_winner_concentration': {str(n): sum(wins[:n]) / total if total else None
                                         for n in (1, 3, 5)}}


def summarize_continuous_account(account, periods=None, bundle=None, checked=None):
    """Cut one verified continuous trace into six MTM half-years.

    Pass ``checked=robust_trace.verify_account(account,bundle)`` to reuse an
    existing verification; the caller must pass that exact account's result.
    Otherwise verification is performed here and needs the frozen input bundle.
    A single extra streaming pass derives boundaries and event contributions;
    no full trace is retained in memory. Hash identity is checked again.
    """
    if account.get('account_mode') != 'fixed-continuous-v1':
        raise ProtocolError('requires fixed-continuous-v1 account')
    periods, edges = _periods(account, periods)
    if checked is None:
        checked = verify_account(account, bundle)
    if (checked.get('verified') is not True or
            checked.get('source_completeness_verified') is not True):
        raise ProtocolError('requires source-complete verified trace')
    path = Path(account.get('trace_path', ''))
    if not path.is_file():
        raise ProtocolError('missing continuous trace')
    digest = hashlib.sha256()
    initial = _finite(checked['initial_equity'], 'initial equity')
    if initial <= 0:
        raise EvaluationError('nonpositive initial equity')
    segments = [{**p, 'start_equity': None, 'end_equity': None, 'max_drawdown_pct': 0.,
                 'trades': 0, 'entries': 0, 'carry_in': 0, 'carry_out': 0,
                 'carry_in_trade_ids': [], 'carry_out_trade_ids': [],
                 'exposure_hours': 0., 'exited_holding_hours': [],
                 'gross_pnl': 0., 'fees': 0., 'funding': 0.} for p in periods]
    segments[0]['start_equity'] = initial
    peaks = [initial] + [None] * 5
    full_peak = initial
    full_dd = 0.
    active = None
    last_time = -1
    count = 0
    exits = []
    final_close = None
    final_exit_reason = None
    with path.open('rb') as stream:
        for raw in stream:
            digest.update(raw)
            e = strict_loads(raw.decode('utf-8'))
            count += 1
            t, phase = e['time'], e['phase']
            if type(t) is not int or t < last_time or not edges[0] <= t < edges[-1]:
                raise ProtocolError('trace timestamp outside ordered account')
            last_time = t
            i = bisect_right(edges, t) - 1
            row = segments[i]
            if row['start_equity'] is None:
                raise ProtocolError('missing B-1ms boundary close')
            if count == 1 and (phase != 'initial' or t != edges[0]):
                raise ProtocolError('missing initial account event')
            for key, event_key in (('gross_pnl', 'gross_pnl'), ('fees', 'fee'), ('funding', 'funding_cash')):
                row[key] += _finite(e[event_key], event_key)
            if phase == 'entry':
                if active is not None:
                    raise ProtocolError('entry while holding')
                active = (e['trade_id'], t)
                row['entries'] += 1
            elif phase == 'exit':
                trade = e['trade']
                if active != (e['trade_id'], trade['entry_time']) or trade['exit_time'] != t:
                    raise ProtocolError('exit/entry ownership differs')
                if trade['reason'] == 'end_of_window' and t != edges[-1] - 1:
                    raise ProtocolError('forced exit before account endpoint')
                for j in range(6):
                    segments[j]['exposure_hours'] += max(0, min(t, edges[j + 1]) -
                                                         max(trade['entry_time'], edges[j])) / 3600000
                row['exited_holding_hours'].append((t - trade['entry_time']) / 3600000)
                row['trades'] += 1
                exits.append(trade)
                active = None
                if t == edges[-1] - 1:
                    final_exit_reason = trade['reason']
                    if final_exit_reason != 'end_of_window':
                        raise ProtocolError('endpoint exit must be forced end_of_window')
            equity = _finite(e['equity'], 'trace equity')
            if e['sampled'] is True:
                if equity <= 0:
                    raise EvaluationError('nonpositive sampled equity')
                peaks[i] = max(peaks[i], equity)
                row['max_drawdown_pct'] = max(row['max_drawdown_pct'], 100 * (peaks[i] - equity) / peaks[i])
                full_peak = max(full_peak, equity)
                full_dd = max(full_dd, 100 * (full_peak - equity) / full_peak)
            if t == edges[i + 1] - 1 and phase == 'close':
                if row['end_equity'] is not None or e['sampled'] is not True:
                    raise ProtocolError('duplicate/unsampled boundary close')
                row['end_equity'] = equity
                row['carry_out'] = int(active is not None)
                row['carry_out_trade_ids'] = [active[0]] if active else []
                if i < 5:
                    segments[i + 1]['start_equity'] = equity
                    segments[i + 1]['carry_in'] = row['carry_out']
                    segments[i + 1]['carry_in_trade_ids'] = list(row['carry_out_trade_ids'])
                    peaks[i + 1] = equity
                else:
                    final_close = e
    if digest.hexdigest() != account.get('trace_sha256'):
        raise ProtocolError('continuous trace identity differs')
    if not count or active is not None or final_close is None or final_close['direction'] != 0:
        raise ProtocolError('continuous account missing final flat close')
    if any(s['start_equity'] is None or s['end_equity'] is None for s in segments):
        raise ProtocolError('missing B-1ms boundary close')
    if count != checked['events'] or exits != checked['trades']:
        raise ProtocolError('summary differs from verified events/trades')
    for row in segments:
        row['net_return_pct'] = 100 * (row['end_equity'] / row['start_equity'] - 1)
    final = segments[-1]['end_equity']
    near(final, checked['final_equity'], 'continuous final equity')
    near(math.prod(1 + s['net_return_pct'] / 100 for s in segments), final / initial, 'six-half return product')
    metrics = checked['metrics']
    near(full_dd, metrics['max_drawdown_pct'], 'full-path drawdown')
    near(100 * (final / initial - 1), metrics['net_return_pct'], 'continuous return')
    near(sum(s['trades'] for s in segments), metrics['trades'], 'six-half exits')
    for key in ('gross_pnl', 'fees', 'funding'):
        near(sum(s[key] for s in segments), metrics[key], 'six-half ' + key)
    near(final - initial, metrics['gross_pnl'] - metrics['fees'] + metrics['funding'], 'continuous cash')
    return {'version': VERSION, 'account_mode': 'fixed-continuous-v1',
            'window': dict(account['window']), 'funding_scenario': account['funding_scenario'],
            'cost_multiplier': account['cost_multiplier'], 'trace_sha256': account['trace_sha256'],
            'initial_equity': initial, 'final_equity': final, 'metrics': dict(metrics),
            'half_years': segments, 'attribution': _attribution(exits),
            'boundary_policy': 'B-1ms close; boundary events in new half; final paid forced exit in H6',
            'drawdown_sampling': 'all sampled execution/close events; not tick risk'}


def continuous_score(summaries):
    """New v2 gates for 2 real accounts × 3 costs and six MTM halves.

    The six-vector uses 1.5x accounts, full-path DD and the worse-half scenario
    envelope. Stress failures are separately reported and also block feasibility.
    """
    rows = {}
    expected = {(s, c) for s in ('central', 'proxy-adverse') for c in (1, 1.5, 2)}
    common_periods = None
    for summary in summaries:
        key = (summary.get('funding_scenario'), summary.get('cost_multiplier'))
        if key not in expected or key in rows or type(key[1]) not in (int, float):
            raise ProtocolError('duplicate/unexpected continuous summary')
        if summary.get('version') != VERSION or summary.get('account_mode') != 'fixed-continuous-v1':
            raise ProtocolError('continuous summary contract differs')
        halves = summary.get('half_years', [])
        ps = [{k: h[k] for k in ('name', 'start', 'end')} for h in halves]
        _periods(summary, ps)
        if common_periods is not None and ps != common_periods:
            raise ProtocolError('continuous accounts have different periods')
        common_periods = ps
        m = summary['metrics']
        r, dd = _finite(m['net_return_pct'], 'account return'), _finite(m['max_drawdown_pct'], 'full drawdown')
        if r <= -100:
            raise EvaluationError('nonpositive continuous equity')
        if not 0 <= dd <= 100:
            raise ProtocolError('invalid full drawdown')
        n = _count(m['trades'], 'account trades')
        for h in halves:
            rr = _finite(h['net_return_pct'], 'half return')
            if rr <= -100:
                raise EvaluationError('nonpositive half equity')
            _count(h['trades'], 'half trades')
        near(sum(h['trades'] for h in halves), n, 'continuous score trade sum')
        near(math.prod(1 + h['net_return_pct'] / 100 for h in halves), 1 + r / 100, 'continuous score return product')
        rows[key] = summary
    if set(rows) != expected:
        raise ProtocolError('missing continuous scenario/cost summary')
    central = [rows[(s, 1.5)] for s in ('central', 'proxy-adverse')]
    worse = [min(a['half_years'][i]['net_return_pct'] / 100 for a in central) for i in range(6)]
    score = _vector([a['metrics']['trades'] for a in central],
                    [h['trades'] for a in central for h in a['half_years']],
                    [a['metrics']['max_drawdown_pct'] / 100 for a in central], worse)
    failed = []
    for (scene, cost), a in rows.items():
        r, dd = a['metrics']['net_return_pct'], a['metrics']['max_drawdown_pct']
        if (r < 0 if cost == 2 else r <= 0) or dd > (20 if cost == 1 else 25):
            failed.append({'funding_scenario': scene, 'cost_multiplier': cost})
    growth = [365.25 / ((utc(p['end']) - utc(p['start'])).total_seconds() / 86400) * math.log1p(r)
              for p, r in zip(common_periods, worse)]
    return {**score, 'score_contract': 'continuous-mtm-six-vector-v2',
            'feasible': score['feasible'] and not failed, 'failed_real_accounts': failed,
            'worst_half_returns_pct': [100 * r for r in worse], 'annual_log_growth': growth,
            'values': [statistics.median(growth), min(growth)],
            'envelope_compound_return_pct': 100 * math.expm1(score['envelope_log_return']),
            'max_drawdown_pct': max(a['metrics']['max_drawdown_pct'] for a in central),
            'envelope_policy': 'worse scenario each half; not one real funding account'}

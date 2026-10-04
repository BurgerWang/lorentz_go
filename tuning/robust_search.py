"""Frozen robust-v1 search protocol; strategy/ledger work remains in Go.

Search quotas and gates are production constants. ``until_attempts`` pauses a
synthetic or authorized real run; it never weakens the frozen search contract.
"""
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import hashlib
import math
from pathlib import Path
import random
import statistics
import sys

import enhanced_space as space_api
from enhanced import strict_loads
from feature_groups import EXTRAS
from optimize import EvaluationError, ProtocolError
from optimize_enhanced import sampler, strict_journal
from robust_budget import Budget, BudgetExhausted, BudgetHalted, atomic_json, cache_identity

VERSION = 'robust-search-v1'
SEEDS = (17, 42, 73)
FAMILIES = space_api.FAMILIES
GROUPS = tuple(EXTRAS)
STAGES = ('C1', 'C2', 'C3')
ORIGINS = ('W1', 'W2', 'W3', 'W4', 'F1')
QUOTAS = {'C1': 64, 'C2': 192, 'C3': 192}
STARTUP = {'C1': 16, 'C2': 32, 'C3': 32}
GATES = {'min_total_trades': 90, 'min_fold_trades': 10,
         'min_positive_folds': 4, 'min_worst_return_pct': -10,
         'max_drawdown_pct': 25, 'compound_strictly_positive': True}


def utc(value):
    if not isinstance(value, str):
        raise ValueError('UTC timestamp required')
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as e:
        raise ValueError('invalid UTC timestamp') from e
    if dt.tzinfo is None or dt.utcoffset().total_seconds() != 0:
        raise ValueError('explicit UTC timestamp required')
    return dt.astimezone(timezone.utc)


def stamp(dt):
    return dt.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def months(dt, offset):
    if dt.day != 1 or any((dt.hour, dt.minute, dt.second, dt.microsecond)):
        raise ValueError('calendar windows require UTC month boundaries')
    n = dt.year * 12 + dt.month - 1 + offset
    return dt.replace(year=n // 12, month=n % 12 + 1)


def windows(cutoff):
    """Six consecutive half-years in the preceding 36 calendar months."""
    end = utc(cutoff)
    return [{'name': 'H' + str(i + 1), 'start': stamp(months(end, -36 + 6 * i)),
             'end': stamp(months(end, -30 + 6 * i))} for i in range(6)]


def origin_schedule():
    cutoffs = ('2024-01-01T00:00:00Z', '2024-07-01T00:00:00Z',
               '2025-01-01T00:00:00Z', '2025-07-01T00:00:00Z', '2026-01-01T00:00:00Z')
    return [{'origin': name, 'cutoff': cutoff, 'training_windows': windows(cutoff),
             'outer_window': {'name': name, 'start': cutoff,
                              'end': '2026-10-01T00:00:00Z' if name == 'F1' else stamp(months(utc(cutoff), 6))}}
            for name, cutoff in zip(ORIGINS, cutoffs)]


def objective(result):
    """Strict six-fold, dual-funding, 1.5x objective and all-path gates."""
    plan = result.get('plan', {})
    folds = plan.get('windows', [])
    if (len(folds) != 6 or plan.get('account_mode') != 'fold-reset-v1' or
            plan.get('funding_scenarios') != ['central', 'proxy-adverse'] or
            plan.get('funding_mode') != 'proxy-stress-v1' or plan.get('cost_multipliers') != [1.5]):
        raise ProtocolError('robust training needs six folds, two funding scenarios, 1.5x cost')
    if folds != windows(folds[-1]['end']):
        raise ProtocolError('training windows differ from calendar-UTC-v1')
    expected = {(space_api.canonical(w), s, 1.5) for w in folds for s in plan['funding_scenarios']}
    rows = {}
    for account in result.get('accounts', []):
        key = (space_api.canonical(account.get('window')), account.get('funding_scenario'), account.get('cost_multiplier'))
        if key not in expected or key in rows:
            raise ProtocolError('unexpected/duplicate training account')
        m = account.get('metrics', {})
        for field in ('net_return_pct', 'max_drawdown_pct'):
            if type(m.get(field)) not in (int, float) or not math.isfinite(m[field]):
                raise ProtocolError('nonfinite training metric: ' + field)
        if type(m.get('trades')) is not int or m['trades'] < 0 or not 0 <= m['max_drawdown_pct'] <= 100:
            raise ProtocolError('invalid trade/drawdown metric')
        if m['net_return_pct'] <= -100:
            raise EvaluationError('nonpositive training equity')
        rows[key] = m
    if set(rows) != expected or result.get('ledger_evaluations') != 12:
        raise ProtocolError('missing training account or ledger count')
    worse, growth = [], []
    violations = []
    max_dd = max(m['max_drawdown_pct'] for m in rows.values())
    for scene in plan['funding_scenarios']:
        ms = [rows[(space_api.canonical(w), scene, 1.5)] for w in folds]
        if sum(m['trades'] for m in ms) < GATES['min_total_trades']:
            violations.append('total_trades:' + scene)
        if any(m['trades'] < GATES['min_fold_trades'] for m in ms):
            violations.append('fold_trades:' + scene)
        if any(m['max_drawdown_pct'] > GATES['max_drawdown_pct'] for m in ms):
            violations.append('drawdown:' + scene)
    for w in folds:
        value = min(rows[(space_api.canonical(w), s, 1.5)]['net_return_pct'] for s in plan['funding_scenarios'])
        days = (utc(w['end']) - utc(w['start'])).total_seconds() / 86400
        worse.append(value)
        growth.append(365.25 / days * math.log1p(value / 100))
    compound = math.expm1(sum(math.log1p(r / 100) for r in worse)) * 100
    if sum(r > 0 for r in worse) < GATES['min_positive_folds']:
        violations.append('positive_folds')
    if min(worse) < GATES['min_worst_return_pct']:
        violations.append('worst_fold')
    if compound <= 0:
        violations.append('compound_return')
    return {'values': [statistics.median(growth), min(growth)], 'feasible': not violations,
            'violations': violations, 'worst_fold_returns_pct': worse, 'annual_log_growth': growth,
            'compound_return_pct': compound, 'max_drawdown_pct': max_dd}


def check_cutoff(source, cutoff, depth=0):
    """Every recursive parent is time-bounded, including legacy S1/S2 inputs."""
    if depth > 32 or not isinstance(source, dict):
        raise ValueError('invalid/recursive source')
    required = {'study', 'trial', 'stage', 'family', 'score_windows', 'parents', 'data_use', 'config_identity'}
    if not required <= source.keys() or source['stage'] not in (*STAGES, *space_api.STAGES):
        raise ValueError('complete known candidate source required')
    if source['stage'] in STAGES and (source.get('origin') not in ORIGINS or source.get('seed') not in SEEDS or
            (source.get('group') not in GROUPS if source['stage'] == 'C1' else source.get('group') is not None)):
        raise ValueError('invalid robust provenance stage identity')
    if source['family'] not in FAMILIES or type(source['trial']) is not int or source['trial'] < 0:
        raise ValueError('invalid source family/trial')
    if source['data_use'] not in {'development', 'retrospective', 'synthetic'}:
        raise ValueError('invalid candidate data use')
    if not isinstance(source['score_windows'], list) or not source['score_windows'] or not isinstance(source['parents'], list):
        raise ValueError('source needs scoring windows and parent list')
    end = utc(cutoff)
    for w in source['score_windows']:
        if not isinstance(w, dict) or set(w) != {'name', 'start', 'end'} or utc(w['start']) >= utc(w['end']):
            raise ValueError('invalid provenance scoring window')
        if utc(w['end']) > end:
            raise ValueError('candidate or parent used scores after cutoff')
    for parent in source['parents']:
        check_cutoff(parent, cutoff, depth + 1)


def source_key(candidate):
    s = candidate['source']
    return (ORIGINS.index(s['origin']), FAMILIES.index(s['family']), SEEDS.index(s['seed']),
            STAGES.index(s['stage']), GROUPS.index(s['group']) if s['stage'] == 'C1' else 0,
            s['trial'], candidate['config_identity'])


def deduplicate(candidates):
    output = {}
    for c in sorted(candidates, key=source_key):
        key = c['config_identity']
        if key not in output:
            output[key] = deepcopy(c)
            output[key]['sources'] = []
        output[key]['sources'].append(deepcopy(c['source']))
    return list(output.values())


def pareto(candidates):
    eligible = deduplicate([c for c in candidates if c['score']['feasible']])
    return [c for c in eligible if not any(
        all(a >= b for a, b in zip(u['score']['values'], c['score']['values'])) and
        any(a > b for a, b in zip(u['score']['values'], c['score']['values'])) for u in eligible)]


def rank_key(c):
    med, worst = c['score']['values']
    return (-worst, -med, c['score']['max_drawdown_pct'], tuple(c['complexity']), source_key(c))


def anchors(candidates):
    pool = sorted(pareto(candidates), key=rank_key)
    if not pool:
        return []
    first = pool[0]
    rest = [c for c in pool if c['config_identity'] != first['config_identity']]
    rest.sort(key=lambda c: (-c['score']['values'][0], -c['score']['values'][1],
                             c['score']['max_drawdown_pct'], tuple(c['complexity']), source_key(c)))
    return [first] + rest[:1]


def central_candidate(candidates):
    pool = sorted(pareto(candidates), key=rank_key)
    return pool[0] if pool else None


def select_flows(candidates):
    """Only explicitly passed local/continuous candidates can enter selection."""
    passed = [c for c in candidates if c.get('local_passed') and c.get('continuous_passed') and c['score']['feasible']]
    seeds = {str(seed): (sorted([c for c in passed if c['source']['seed'] == seed], key=rank_key) or [None])[0]
             for seed in SEEDS}
    available = [c for c in seeds.values() if c is not None]
    return {'seeds': seeds, 'main': (sorted(available, key=rank_key) or [None])[0],
            'status': 'development_candidate' if available else 'no qualified candidate'}


def compile_search_profile(plan, daily_history_start):
    """Pre-cutoff temporal availability; Go inspect verifies gap-free bundle input.

    The bundle's full-file profile can include outer data and is deliberately
    ignored. Only completed target and daily bars before this origin's cutoff
    enter the frozen clipping declaration.
    """
    from robust_data import INTERVALS, compile_profile
    cutoff = utc(plan['windows'][-1]['end'])
    history = utc(plan['history_start'])
    daily_start = utc(daily_history_start + 'T00:00:00Z' if len(daily_history_start) == 10 else daily_history_start)
    duration_ms = int((cutoff - history).total_seconds() * 1000)
    if plan['interval'] not in INTERVALS or duration_ms <= 0 or duration_ms % INTERVALS[plan['interval']]:
        raise ValueError('invalid pre-cutoff target history')
    profile = compile_profile(plan['interval'], duration_ms // INTERVALS[plan['interval']],
                              int((cutoff - daily_start).total_seconds() // 86400),
                              plan['history_start'], stamp(cutoff))
    if not profile['evidence_sufficient']:
        raise ValueError('insufficient mature samples for frozen legal classifier domain')
    return profile


def _profile_domains(profile):
    if not isinstance(profile, dict) or profile.get('version') != 'native-bars-v1' or not profile.get('evidence_sufficient'):
        raise ValueError('complete sufficient native-bars profile required')
    result = {'max_bars_back': list(profile['max_bars_back_domain']), 'neighbors': deepcopy(profile['neighbors_domain'])}
    space_api.validate_domains(result)
    return result


def _intersect_domains(domains, profile):
    result = deepcopy(domains)
    caps = _profile_domains(profile)
    for key, cap in caps.items():
        old = result.get(key, cap)
        if isinstance(cap, list):
            value = [x for x in cap if x in old]
            if not value:
                raise ValueError('empty legal pre-cutoff domain: ' + key)
            result[key] = value
        else:
            lo, hi = max(old['low'], cap['low']), min(old['high'], cap['high'])
            if lo > hi:
                raise ValueError('empty legal pre-cutoff domain: ' + key)
            result[key] = {'low': lo, 'high': hi, 'step': old['step']}
    space_api.validate_domains(result)
    return result


def wide_space(family, daily_history_start):
    return {'version': space_api.VERSION, 'family': family, 'stage': 'S3',
            'domains': {}, 'daily_history_start': daily_history_start}


def active_parameters(config, defaults):
    spec = wide_space(config['classifier']['family'], config['daily']['history_start'])
    return space_api.seed_parameters(config, defaults, spec)


def complexity(config, defaults):
    return [len(active_parameters(config, defaults)), len(config['classifier']['feature_group']['features'])]


def numeric_domains():
    cats, nums = space_api.domain_specs()
    out = {k: list(v) for k, v in cats.items()
           if v and all(type(x) in (int, float) for x in v)}
    for k, (lo, hi, step, integer) in nums.items():
        if k == 'feature_count':
            continue  # feature dimension is structural
        n = round((hi - lo) / step)
        out[k] = [int(lo + step * i) if integer else round(lo + step * i, 12) for i in range(n + 1)]
    return out


def shrink_domains(candidates, defaults):
    """Semantic numeric keys are already f<slot>_<type>_a/b in the compiler."""
    feasible = [c for c in candidates if c['score']['feasible']]
    if not any(c['source']['stage'] == 'C2' for c in feasible):
        return None
    references = sorted(pareto(feasible), key=rank_key)[:12]
    domains = numeric_domains()
    observed = {k: [] for k in domains}
    for c in references:
        for k, v in active_parameters(c['config'], defaults).items():
            if k in observed:
                observed[k].append(v)
    cats, nums = space_api.domain_specs()
    result = {}
    for key, values in observed.items():
        if len(values) < 3 or len(set(values)) < 2:
            continue
        grid = domains[key]
        inds = [_grid_index(grid, v) for v in values]
        subset = grid[max(0, min(inds) - 1):min(len(grid), max(inds) + 2)]
        result[key] = subset if key in cats else {'low': subset[0], 'high': subset[-1], 'step': nums[key][2]}
    space_api.validate_domains(result)
    return {'reference_configs': [c['config'] for c in references],
            'references': [c['config_identity'] for c in references],
            'reference_sources': [c['source'] for c in references], 'domains': result}


def _grid_index(grid, value):
    for i, x in enumerate(grid):
        if math.isclose(x, value, rel_tol=1e-10, abs_tol=1e-10):
            return i
    raise ValueError('active numeric value outside frozen wide grid')


class FixedTrial:
    def __init__(self, parameters):
        self.parameters = parameters
        self.params = {}

    def suggest_categorical(self, key, choices):
        value = self.parameters[key]
        if value not in choices:
            raise ValueError('fixed value outside domain: ' + key)
        self.params[key] = value
        return value

    def suggest_int(self, key, lo, hi, step=1):
        value = self.parameters[key]
        if type(value) is not int or not lo <= value <= hi or (value - lo) % step:
            raise ValueError('fixed integer outside grid: ' + key)
        self.params[key] = value
        return value

    def suggest_float(self, key, lo, hi, step):
        value = self.parameters[key]
        if not lo <= value <= hi or not math.isclose((value - lo) / step, round((value - lo) / step), abs_tol=1e-9):
            raise ValueError('fixed number outside grid: ' + key)
        self.params[key] = value
        return value


def neighbor_blocks(parameters):
    """Frozen six blocks and within-block order, independent of dictionary order."""
    blocks = [[], [], [], [], [], []]
    orders = [
        ['neighbors', 'max_bars_back', 'min_vote_fraction', 'sample_stride', 'vote_half_life', 'rank_half_life'],
        [f'f{i}_{kind}_{suffix}' for i in range(5) for kind in ('RSI', 'WT', 'CCI', 'ADX') for suffix in ('a', 'b')],
        [f'{extra}_{suffix}' for extra in ('rvol', 'atr_price', 'kernel_deviation', 'd1_slope') for suffix in ('period', 'window')] +
            ['daily_h', 'daily_r', 'daily_x'],
        ['kernel_h', 'kernel_r', 'kernel_x', 'kernel_lag', 'ema_period', 'sma_period', 'regime_threshold', 'adx_threshold'],
        ['envelope_h', 'envelope_r', 'envelope_x', 'envelope_atr_length', 'near', 'far_gap', 'pullback_wait'],
        ['risk_atr_multiplier', 'trail_multiplier', 'breakeven_r', 'risk_atr_period', 'max_hold_bars']]
    numeric = numeric_domains()
    for i, order in enumerate(orders):
        blocks[i] = [k for k in order if k in parameters and k in numeric]
    return blocks


def neighbors(config, defaults, origin, validate_config=None, profile=None):
    """Freeze at most 24 legal points without changing mechanism/structure."""
    if origin not in ORIGINS:
        raise ValueError('unknown origin')
    central = space_api.normalize_config(config, defaults)
    params = active_parameters(central, defaults)
    blocks = neighbor_blocks(params)
    all_keys = [key for block in blocks for key in block]
    selected = []
    for i in range(max((len(b) for b in blocks), default=0)):
        for block in blocks:
            if i < len(block) and len(selected) < 12:
                selected.append(block[i])
    grids = numeric_domains()
    if profile is not None:
        caps = _profile_domains(profile)
        grids['max_bars_back'] = caps['max_bars_back']
        cap = caps['neighbors']
        grids['neighbors'] = list(range(cap['low'], cap['high'] + 1, cap['step']))
    seen = {space_api.identity(central)}
    points = []
    spec = wide_space(central['classifier']['family'], central['daily']['history_start'])
    if profile is not None:
        spec['domains'] = _profile_domains(profile)

    def accept(changes):
        trial_params = deepcopy(params)
        for key, direction in changes:
            grid = grids[key]
            index = _grid_index(grid, params[key]) + direction
            if not 0 <= index < len(grid):
                return
            trial_params[key] = grid[index]
        try:
            c = space_api.sample_config(FixedTrial(trial_params), defaults, spec)
            if validate_config:
                validate_config(c)
        except ValueError:
            return
        key = space_api.identity(c)
        if key not in seen:
            seen.add(key)
            points.append({'config': c, 'config_identity': key, 'changes': [[k, d] for k, d in changes]})
    for key in selected:
        for direction in (-1, 1):
            accept([(key, direction)])
    rng_seed = int(hashlib.sha256(('robust-v1-neighbor|' + origin + '|' + space_api.identity(central)).encode()).hexdigest()[:16], 16)
    rng = random.Random(rng_seed)
    generation_attempts = 0
    while len(points) < 24 and generation_attempts < 256 and all_keys:
        count = min(2 if generation_attempts % 2 == 0 else 3, len(all_keys))
        keys = rng.sample(all_keys, count)
        accept([(key, rng.choice((-1, 1))) for key in keys])
        generation_attempts += 1
    return {'version': 'robust-neighbor-v1', 'origin': origin,
            'central_identity': space_api.identity(central), 'points': points[:24],
            'selected_keys': selected, 'uncovered_keys': [k for k in all_keys if k not in selected],
            'active_numeric_keys': all_keys, 'joint_seed': rng_seed,
            'joint_generation_attempts': generation_attempts, 'python_version': sys.version}


def neighborhood_check(central, frozen, results):
    """None is ordinary failed evaluation; failures stay in the frozen denominator."""
    points = frozen['points']
    if frozen.get('central_identity') != space_api.identity(central['config']):
        raise ProtocolError('frozen neighborhood central identity differs')
    central_response = central.get('response', central)
    expected_plan = central_response.get('plan')
    if not isinstance(expected_plan, dict):
        raise ValueError('complete central training response required')
    recomputed = objective(central_response)
    if 'score' in central and central['score'] != recomputed:
        raise ProtocolError('central score differs from its training response')
    if len(results) != len(points):
        raise ValueError('all frozen neighbor outcomes including failures required')
    n = len(points)
    passed = 0
    successes = []
    failures = 0
    for point, result in zip(points, results):
        if result is None:
            failures += 1
            continue
        if space_api.canonical(result.get('plan')) != space_api.canonical(expected_plan):
            raise ProtocolError('neighbor used a different training plan')
        if space_api.identity(result['config']) != point['config_identity']:
            raise ProtocolError('neighbor config differs from frozen point')
        try:
            score = objective(result)
        except EvaluationError:
            failures += 1
            continue
        successes.append(score['values'][0])
        if score['compound_return_pct'] > 0 and score['max_drawdown_pct'] <= 25:
            passed += 1
    successes.sort()
    positions = [(n - 1) // 2, n // 2] if n else []
    # Failure values are represented by rank, never fictitious infinities.
    median = None if not positions or any(p < failures for p in positions) else statistics.mean(successes[p - failures] for p in positions)
    center_score = recomputed
    enough = n >= 12
    return {'passed': enough and passed >= math.ceil(.8 * n) and median is not None and median >= .7 * center_score['values'][0],
            'N': n, 'passed_points': passed, 'required_points': math.ceil(.8 * n), 'ordinary_failures': failures,
            'median_neighbor_growth': median, 'central_growth': center_score['values'][0],
            'status': 'stability evidence insufficient' if not enough else 'evaluated'}


def continuous_check(result):
    p = result.get('plan', {})
    if (p.get('account_mode') != 'fixed-continuous-v1' or len(p.get('windows', [])) != 1 or
            p.get('cost_multipliers') != [1, 1.5, 2] or p.get('funding_scenarios') != ['central', 'proxy-adverse'] or
            p.get('funding_mode') != 'proxy-stress-v1'):
        raise ProtocolError('continuous development contract differs')
    window = p['windows'][0]
    if utc(window['start']) != months(utc(window['end']), -36):
        raise ProtocolError('continuous development must score exactly36 calendar months')
    accounts = result.get('accounts', [])
    expected = {(s, cost) for s in p['funding_scenarios'] for cost in p['cost_multipliers']}
    seen = set()
    failed = []
    for a in accounts:
        k = (a.get('funding_scenario'), a.get('cost_multiplier'))
        if k not in expected or k in seen or a.get('window') != p['windows'][0]:
            raise ProtocolError('continuous account layout differs')
        seen.add(k)
        m = a['metrics']
        r, dd = m['net_return_pct'], m['max_drawdown_pct']
        if any(type(x) not in (int, float) or not math.isfinite(x) for x in (r, dd)) or not 0 <= dd <= 100:
            raise ProtocolError('continuous metric invalid')
        if (r <= 0 if k[1] in (1, 1.5) else r < 0) or dd > 25:
            failed.append([*k])
    if seen != expected or result.get('ledger_evaluations') != 6:
        raise ProtocolError('continuous account/ledger count differs')
    return {'passed': not failed, 'failed_accounts': failed}


def study_name(origin, family, seed, stage, group=None):
    return '/'.join((origin, family, str(seed), stage) + ((group,) if stage == 'C1' and group is not None else ()))


def search_limits(origins=ORIGINS):
    limits = {}
    for origin in origins:
        for family in FAMILIES:
            for seed in SEEDS:
                for stage in STAGES:
                    for group in (GROUPS if stage == 'C1' else (None,)):
                        q = QUOTAS[stage]
                        limits[study_name(origin, family, seed, stage, group)] = {
                            'trial_attempts': q, 'go_requests': q, 'ledger_evaluations': q * 12}
    return limits


def _candidate(trial, defaults):
    r = trial.user_attrs['response']
    c = r['config']
    return {'config': c, 'config_identity': space_api.identity(c), 'source': trial.user_attrs['source'],
            'score': trial.user_attrs['score'], 'complexity': complexity(c, defaults),
            'response': r, 'execution': trial.user_attrs['execution']}


def run_origin(out, plan, defaults, evaluator, budget, contract, origin='W1',
               daily_history_start=None, validate_config=None, until_attempts=None, profile=None):
    """Run/resume native independent TPE: 3 seeds x 2 families x 768 attempts.

    evaluator(plan, config) must validate the Go protocol and immutable engine/
    bundle identity against contract. Its ordinary EvaluationError consumes the
    request; ProtocolError or any unclassified evaluator error halts research.
    until_attempts is an optional total-attempt checkpoint for this origin.
    """
    import optuna,numpy
    if optuna.__version__ != '5.0.0' or not hasattr(optuna.trial.Trial, 'set_constraint'):
        raise RuntimeError('Optuna5.0.0 with native constraints required')
    if origin not in ORIGINS or not isinstance(budget, Budget):
        raise ValueError('known origin and persistent shared Budget required')
    if until_attempts is not None and (type(until_attempts) is not int or until_attempts < 0):
        raise ValueError('nonnegative checkpoint required')
    # Validate the plan independent of any market call using a shape-only check.
    if (plan.get('windows') != windows(plan['windows'][-1]['end']) or
            plan.get('cost_multipliers') != [1.5] or plan.get('funding_scenarios') != ['central', 'proxy-adverse'] or
            plan.get('funding_mode') != 'proxy-stress-v1' or plan.get('account_mode') != 'fold-reset-v1'):
        raise ValueError('frozen training plan differs')
    daily_history_start = daily_history_start or defaults['daily']['history_start']
    expected_profile = compile_search_profile(plan, daily_history_start)
    if profile is not None and space_api.canonical(profile) != space_api.canonical(expected_profile):
        raise ValueError('provided search profile differs from pre-cutoff availability')
    profile = expected_profile
    cutoff = plan['windows'][-1]['end']
    expected_cutoff = next(x['cutoff'] for x in origin_schedule() if x['origin'] == origin)
    if utc(cutoff) != utc(expected_cutoff) or utc(plan['history_start']) != utc('2020-01-01T00:00:00Z'):
        raise ValueError('origin cutoff/fixed causal history differs')
    for parent in contract.get('parents', []):
        check_cutoff(parent, cutoff)
    binding = {'version': VERSION, 'origin': origin, 'plan': plan, 'defaults': defaults, 'contract': contract,
               'seeds': list(SEEDS), 'families': list(FAMILIES), 'groups': list(GROUPS), 'quotas': QUOTAS,
               'startup': STARTUP, 'gates': GATES, 'daily_history_start': daily_history_start, 'profile': profile,
               'sampler': 'independent-per-trial-TPE', 'python_version': sys.version,
               'optuna_version': optuna.__version__,
               'numpy_version': numpy.__version__,
               'implementation_sha256': {str(Path(x).resolve()): hashlib.sha256(Path(x).read_bytes()).hexdigest()
                   for x in (__file__, space_api.__file__, str(Path(__file__).with_name('robust_budget.py')),
                             str(Path(__file__).with_name('optimize_enhanced.py')),
                             str(Path(__file__).with_name('feature_groups.py')),
                             str(Path(__file__).with_name('robust_data.py')))}}
    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    with (out / 'run.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        contract_path = out / 'run-contract.json'
        if contract_path.exists():
            if space_api.canonical(strict_loads(contract_path.read_text())) != space_api.canonical(binding):
                raise ValueError('search contract differs; cannot resume')
        else:
            if any(p.name != 'run.lock' for p in out.iterdir()):
                raise ValueError('nonempty search output has no contract')
            atomic_json(contract_path, binding)
        state_path = out / 'state.json'
        state = strict_loads(state_path.read_text()) if state_path.exists() else {'frozen': {}, 'completed': [], 'status': 'in-progress'}
        all_candidates = []
        abstentions = []
        origin_names = set(search_limits((origin,)))
        existing_budget = budget.snapshot(include_cache=False)
        if space_api.canonical(existing_budget['binding']['contract']) != space_api.canonical(contract):
            raise ValueError('search/research budget contracts differ')
        if not origin_names <= set(existing_budget['substudy_counts']):
            raise ValueError('all fixed search substudy budgets must be registered before run')
        for name in origin_names:
            if existing_budget['binding']['substudy_limits'][name] != search_limits((origin,))[name]:
                raise ValueError('search substudy quotas differ')

        def consumed():
            s = budget.counts()['substudy_counts']
            return sum(s[name]['trial_attempts'] for name in origin_names)

        def save_summary(complete=False):
            centers = []
            for family in FAMILIES:
                for seed in SEEDS:
                    pool = [c for c in all_candidates if c['source']['family'] == family and c['source']['seed'] == seed]
                    c = central_candidate(pool)
                    if c is not None:
                        centers.append(c)
            state['status'] = 'complete' if complete else ('halted' if budget.halted() else 'paused')
            atomic_json(state_path, state)
            summary = {'version': VERSION, 'origin': origin, 'status': state['status'],
                       'candidate_status': 'development central candidates; local checks pending' if centers else 'no feasible candidate',
                       'central_candidates': centers, 'abstentions': abstentions,
                       'eligible_candidates': deduplicate(all_candidates),
                       'budget': budget.snapshot(include_cache=False), 'frozen_stages': state['frozen']}
            atomic_json(out / 'summary.json', summary)
            return summary

        for family in FAMILIES:
            for seed in SEEDS:
                previous = []
                for stage in STAGES:
                    freeze_key = study_name(origin, family, seed, stage)
                    if stage == 'C2':
                        chosen = anchors(previous)
                        if not chosen:
                            abstentions.append({'family': family, 'seed': seed, 'stage': stage, 'reason': 'no feasible C1 anchor'})
                            break
                        frozen = {'anchors': [{k: c[k] for k in ('config', 'config_identity', 'source')} for c in chosen]}
                    elif stage == 'C3':
                        frozen = shrink_domains(previous, defaults)
                        if frozen is None:
                            abstentions.append({'family': family, 'seed': seed, 'stage': stage, 'reason': 'no feasible C2 result'})
                            break
                    else:
                        frozen = {}
                    if stage != 'C1':
                        if freeze_key in state['frozen']:
                            if space_api.canonical(state['frozen'][freeze_key]) != space_api.canonical(frozen):
                                raise ValueError('frozen anchor/domain reference differs')
                            frozen = state['frozen'][freeze_key]
                        else:
                            state['frozen'][freeze_key] = frozen
                            atomic_json(state_path, state)
                    for group in (GROUPS if stage == 'C1' else (None,)):
                        name = study_name(origin, family, seed, stage, group)
                        folder = out / family / str(seed) / (stage + ('-' + group if group else ''))
                        folder.mkdir(parents=True, exist_ok=True)
                        spec = {'version': space_api.VERSION, 'family': family,
                                'stage': {'C1': 'S1', 'C2': 'S2', 'C3': 'S3'}[stage],
                                'daily_history_start': daily_history_start, 'domains': {}}
                        if stage == 'C1':
                            spec['domains']['feature_group'] = [group]
                        elif stage == 'C2':
                            spec['anchors'] = frozen['anchors']
                            spec['domains']['anchor_control'] = [False]
                        else:
                            spec['domains'] = frozen['domains']
                        if stage != 'C2':
                            spec['domains'] = _intersect_domains(spec['domains'], profile)
                        strict_journal(folder / 'study.journal')
                        storage = optuna.storages.JournalStorage(optuna.storages.journal.JournalFileBackend(str(folder / 'study.journal')))
                        names = optuna.get_all_study_names(storage)
                        if names and names != [name]:
                            raise ValueError('unexpected persisted study')
                        st = optuna.load_study(study_name=name, storage=storage, sampler=sampler(seed, STARTUP[stage], 'independent')) if names else optuna.create_study(
                            study_name=name, storage=storage, sampler=sampler(seed, STARTUP[stage], 'independent'), directions=['maximize', 'maximize'])
                        stage_contract = {'binding_identity': space_api.identity(binding), 'space': spec, 'name': name}
                        stored_contract = st.user_attrs.get('contract')
                        if stored_contract is None:
                            if st.trials:
                                raise ValueError('persisted trials have no contract')
                            st.set_user_attr('contract', stage_contract)
                        elif space_api.canonical(stored_contract) != space_api.canonical(stage_contract):
                            raise ValueError('substudy contract differs')
                        # Lost budget state cannot reset failed/finished study usage.
                        # Every consumed native trial must already have its atomic charge.
                        for existing_trial in st.trials:
                            if existing_trial.state.name != 'WAITING':
                                existing_token = name + '/' + str(existing_trial.number)
                                charged = budget.record(existing_token)
                                if (not charged or charged['study'] != name or
                                        charged['charged']['trial_attempts'] != 1):
                                    budget.halt('journal trial has no persisted charged attempt: ' + existing_token)
                                    raise ProtocolError('lost/mismatched research budget state; no usage reset')
                        # A reservation precedes ask/compile as well as the Go send.
                        # Recover the narrow reservation-before-ask crash window by
                        # creating/consuming its original number, never sampling again.
                        pending = [(token, r) for token, r in budget.records_for(name).items()
                                   if r['study'] == name and r['status'] in {'attempt', 'reserved'}]
                        for token, r in pending:
                            number = int(token.rsplit('/', 1)[1])
                            trials = st.trials
                            if number >= len(trials):
                                recovery = st.ask()
                                if recovery.number != number:
                                    raise ValueError('budget/journal attempt numbers differ')
                                st.tell(recovery, state=optuna.trial.TrialState.FAIL)
                            elif trials[number].state.name in {'RUNNING', 'WAITING'}:
                                storage.set_trial_state_values(trials[number]._trial_id, optuna.trial.TrialState.FAIL)
                        for t in st.trials:
                            if t.state.name == 'RUNNING':
                                token = name + '/' + str(t.number)
                                if budget.record(token) is None:
                                    raise ValueError('running journal trial has no budget reservation')
                                st.tell(t.number, state=optuna.trial.TrialState.FAIL)
                        budget.recover(name)
                        if stage == 'C2' and not st.user_attrs.get('structure_queue_frozen'):
                            if st.trials:
                                raise ValueError('partial structure queue without frozen marker')
                            for a in frozen['anchors']:
                                check_cutoff(a['source'], cutoff)
                            # Persist marker before enqueue; recovery fills missing entries by seed_index.
                            st.set_user_attr('structure_queue_frozen', True)
                        if stage == 'C2':
                            already = {t.user_attrs.get('structure_index') for t in st.trials}
                            structure = [(a['config_identity'], entry, risk) for a in frozen['anchors']
                                         for entry in ('main', 'pullback', 'both')
                                         for risk in ('off', 'atr', 'trail', 'breakeven', 'trail-breakeven')]
                            for i, (a, entry, risk) in enumerate(structure):
                                if i not in already:
                                    st.enqueue_trial({'anchor': a, 'anchor_control': False, 'entry_mode': entry, 'risk_mode': risk},
                                                     user_attrs={'structure_index': i})
                        for t in st.trials:
                            if t.state.name == 'COMPLETE':
                                r = t.user_attrs['response']
                                key = cache_identity(plan, r['config'])
                                if (space_api.canonical(r.get('plan')) != space_api.canonical(plan) or
                                        t.user_attrs['execution'].get('cache_key') != key or
                                        space_api.canonical(budget.cached_response(key)) != space_api.canonical(r)):
                                    raise ProtocolError('persisted successful response/cache identity differs')
                                compiled = space_api.sample_config(FixedTrial(t.params), defaults, spec)
                                if space_api.canonical(compiled) != space_api.canonical(r['config']):
                                    raise ProtocolError('persisted trial params/config differ')
                                token = name + '/' + str(t.number)
                                record = budget.record(token)
                                if not record or record['status'] != 'complete' or record.get('cache_key') != key or record['study'] != name:
                                    raise ProtocolError('completed trial has no matching successful reservation')
                                score = objective(r)
                                if score != t.user_attrs['score'] or list(t.values) != score['values']:
                                    raise ProtocolError('persisted score differs')
                                c = _candidate(t, defaults)
                                expected_source = {'study': str(folder), 'origin': origin, 'family': family, 'seed': seed,
                                                   'stage': stage, 'group': group, 'trial': t.number,
                                                   'score_windows': plan['windows'], 'data_use': contract.get('data_use', 'retrospective'),
                                                   'config_identity': c['config_identity']}
                                if stage == 'C2':
                                    expected_source['parents'] = [next(a['source'] for a in frozen['anchors'] if a['config_identity'] == t.params['anchor'])]
                                elif stage == 'C3':
                                    expected_source['parents'] = frozen['reference_sources']
                                else:
                                    expected_source['parents'] = []
                                if any(c['source'].get(k) != v for k, v in expected_source.items()):
                                    raise ProtocolError('persisted candidate source differs')
                                check_cutoff(c['source'], cutoff)
                                if c['score']['feasible']:
                                    previous.append(c)
                                    all_candidates.append(c)
                        while sum(t.state.name != 'WAITING' for t in st.trials) < QUOTAS[stage]:
                            if budget.halted():
                                return save_summary()
                            if until_attempts is not None and consumed() >= until_attempts:
                                return save_summary()
                            trials = st.trials
                            waiting = [t for t in trials if t.state.name == 'WAITING']
                            next_number = waiting[0].number if waiting else len(trials)
                            token = name + '/' + str(next_number)
                            try:
                                reserved = budget.reserve_attempt(name, token)
                            except (BudgetExhausted, BudgetHalted):
                                return save_summary()
                            if reserved['status'] != 'attempt':
                                raise ValueError('attempt number was already consumed')
                            t = st.ask()
                            if t.number != next_number:
                                budget.halt('budget/journal next number differs')
                                return save_summary()
                            phase = 'compile'
                            try:
                                cfg = space_api.sample_config(t, defaults, spec)
                                if validate_config:
                                    validate_config(cfg)
                                source = {'study': str(folder), 'origin': origin, 'family': family, 'seed': seed,
                                          'stage': stage, 'group': group, 'trial': t.number,
                                          'score_windows': deepcopy(plan['windows']), 'parents': [],
                                          'data_use': contract.get('data_use', 'retrospective'), 'config_identity': space_api.identity(cfg)}
                                if stage == 'C2':
                                    a = next(a for a in frozen['anchors'] if a['config_identity'] == t.params['anchor'])
                                    source['parents'] = [deepcopy(a['source'])]
                                elif stage == 'C3':
                                    source['parents'] = deepcopy(frozen['reference_sources'])
                                check_cutoff(source, cutoff)
                                t.set_user_attr('source', source)
                                key = cache_identity(plan, cfg)
                                execution = budget.reserve_evaluation(name, token, key, 12)
                                t.set_user_attr('execution', execution['record'])
                                phase = 'evaluate'
                                r = evaluator(deepcopy(plan), deepcopy(cfg)) if execution['send'] else execution['response']
                                if space_api.canonical(r.get('config')) != space_api.canonical(cfg) or space_api.canonical(r.get('plan')) != space_api.canonical(plan):
                                    raise ProtocolError('evaluator changed configuration/plan')
                                score = objective(r)
                                if execution['send']:
                                    budget.finish(token, response=r)
                                t.set_user_attr('response', r)
                                t.set_user_attr('score', score)
                                t.set_constraint('robust_gates', 0.0 if score['feasible'] else 1.0)
                                st.tell(t, score['values'])
                                c = _candidate(st.trials[t.number], defaults)
                                if score['feasible']:
                                    previous.append(c)
                                    all_candidates.append(c)
                            except (BudgetExhausted, BudgetHalted) as e:
                                record = budget.record(token)
                                if record and record['status'] in {'attempt', 'reserved'}:
                                    budget.finish(token, error=e)
                                st.tell(t, state=optuna.trial.TrialState.FAIL)
                                return save_summary()
                            except EvaluationError as e:
                                budget.finish(token, error=e)
                                t.set_user_attr('error', str(e))
                                st.tell(t, state=optuna.trial.TrialState.FAIL)
                            except ValueError as e:
                                # Invalid sampled config is an ordinary counted attempt; evaluator ValueError is fatal.
                                record = budget.record(token)
                                if record and record['status'] in {'attempt', 'reserved'}:
                                    budget.finish(token, error=e)
                                t.set_user_attr('error', str(e))
                                st.tell(t, state=optuna.trial.TrialState.FAIL)
                                if phase == 'evaluate':
                                    budget.halt('data/protocol fault: ' + str(e))
                                    return save_summary()
                            except (KeyboardInterrupt, SystemExit):
                                # Reservation remains consumed; next resume marks unknown outcome FAIL.
                                save_summary()
                                raise
                            except Exception as e:
                                record = budget.record(token)
                                if record and record['status'] in {'attempt', 'reserved'}:
                                    budget.finish(token, error=e)
                                t.set_user_attr('error', str(e))
                                st.tell(t, state=optuna.trial.TrialState.FAIL)
                                budget.halt('data/protocol fault: ' + str(e))
                                return save_summary()
                        if name not in state['completed']:
                            state['completed'].append(name)
                            atomic_json(state_path, state)
        return save_summary(complete=True)

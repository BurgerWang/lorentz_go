"""Frozen P4 numeric domains and continuous-pair development scoring.

This module never evaluates a strategy or changes a prior research artifact.
"""
from copy import deepcopy
from itertools import combinations, product
import math
import statistics

import enhanced_space as space
import mechanism_metrics as metrics
import mechanism_p3_client as client
import mechanism_v2 as v2
import robust_search as search
from robust_trace import near

VERSION = 'mechanism-p4-domain-v1'
BLOCKS = ('classifier', 'management')
KEYS = {
    'A': {'classifier': ('neighbors', 'f0_RSI_a', 'f1_WT_a', 'f2_CCI_a'),
          'management': ('risk_atr_period', 'risk_atr_multiplier', 'breakeven_r', 'pullback_wait')},
    'B': {'classifier': ('neighbors', 'f0_WT_a', 'f1_RSI_a', 'f2_ADX_a'),
          'management': ('ema_period', 'regime_threshold', 'adx_threshold', 'max_hold_bars')},
}
PATHS = {
    'neighbors': ('classic', 'neighbors'),
    'risk_atr_period': ('risk', 'atr_period'),
    'risk_atr_multiplier': ('risk', 'atr_multiplier'),
    'breakeven_r': ('risk', 'breakeven_r'),
    'pullback_wait': ('pullback', 'max_wait_bars'),
    'ema_period': ('classic', 'ema_period'),
    'regime_threshold': ('classic', 'regime_threshold'),
    'adx_threshold': ('classic', 'adx_threshold'),
    'max_hold_bars': ('exit', 'max_hold_bars'),
}


def _get(config, key):
    c = config['strategy']
    if key.startswith('f') and key not in PATHS:
        slot, kind, suffix = key.split('_')
        i = int(slot[1:])
        a, b = c['classic']['features'][i], c['classifier']['feature_group']['features'][i]
        if a['name'] != kind or b['name'] != kind or a[suffix] != b[suffix]:
            raise ValueError('P4 mirrored feature contract differs: '+key)
        return a[suffix]
    p, q = PATHS[key]
    return c[p][q]


def _put(config, key, value):
    c = config['strategy']
    if key.startswith('f') and key not in PATHS:
        slot, _, suffix = key.split('_'); i = int(slot[1:])
        c['classic']['features'][i][suffix] = value
        c['classifier']['feature_group']['features'][i][suffix] = value
    else:
        p, q = PATHS[key]; c[p][q] = value


def freeze(anchor, skeleton):
    client.validate_wrapper(anchor)
    if skeleton not in KEYS or anchor['variant'] != 'classic-original':
        raise ValueError('P4 supports only retained original A/B')
    c = anchor['strategy']; v2.unique_features(c)
    if (skeleton == 'A' and (c['entry_mode'] != 'both' or not c['risk']['enabled']
        or c['exit']['policy'] != 'signals' or not c['pullback']['enabled'])):
        raise ValueError('P4 A management skeleton differs')
    if (skeleton == 'B' and (c['entry_mode'] != 'main' or c['risk']['enabled']
        or c['exit']['policy'] != 'four-bars' or c['pullback']['enabled']
        or not all(c['classic']['use_'+f+'_filter'] for f in ('ema', 'regime', 'adx')))):
        raise ValueError('P4 B management skeleton differs')
    grids = search.numeric_domains(); blocks = {}
    for block in BLOCKS:
        domains = {}
        for key in KEYS[skeleton][block]:
            value = _get(anchor, key); grid = grids[key]
            i = search._grid_index(grid, value)
            values = grid[max(0, i-1):i+2]
            if len(values) != 3 or value not in values:
                raise ValueError('P4 requires three legal adjacent grid values: '+key)
            domains[key] = values
        blocks[block] = {'keys': list(KEYS[skeleton][block]), 'domains': domains,
                         'cardinality': math.prod(len(v) for v in domains.values())}
    return {'version': VERSION, 'skeleton': skeleton, 'anchor': deepcopy(anchor),
            'anchor_identity': space.identity(anchor), 'blocks': blocks,
            'policy': 'original anchor +/- one established grid; domain never adapts or widens',
            'inactive_policy': 'all other fields and feature/D1 context remain byte-semantic fixed'}


def _validate_rules(rules):
    if rules != freeze(rules['anchor'], rules['skeleton']):
        raise ValueError('P4 rule identity or domain changed')


def validate_config(config, rules):
    _validate_rules(rules); client.validate_wrapper(config)
    if config['variant'] != 'classic-original':
        raise ValueError('P4 cannot change the retained classifier version')
    restored = deepcopy(config)
    for block in BLOCKS:
        for key in rules['blocks'][block]['keys']:
            v = _get(config, key)
            choices = rules['blocks'][block]['domains'][key]
            if type(v) not in (int, float) or not math.isfinite(v) or v not in choices:
                raise ValueError('P4 value outside frozen numeric domain: '+key)
            if type(_get(rules['anchor'], key)) is int and type(v) is not int:
                raise ValueError('P4 integer key changed type: '+key)
            _put(restored, key, _get(rules['anchor'], key))
    if space.canonical(restored) != space.canonical(rules['anchor']):
        raise ValueError('P4 changed a frozen structural/context/inactive field')
    v2.unique_features(config['strategy'])
    return config


def parameters(config, rules, block):
    validate_config(config, rules)
    return {k: _get(config, k) for k in rules['blocks'][block]['keys']}


def compile_config(base, rules, block, params):
    validate_config(base, rules)
    spec = rules['blocks'][block]
    if set(params) != set(spec['keys']):
        raise ValueError('P4 requires exactly the active block keys')
    cfg = deepcopy(base)
    for k in spec['keys']:
        _put(cfg, k, params[k])
    return validate_config(cfg, rules)


def configurations(rules):
    """Every possible complete future wrapper in this immutable two-block domain."""
    _validate_rules(rules)
    keys = [k for block in BLOCKS for k in rules['blocks'][block]['keys']]
    choices = {k: rules['blocks'][b]['domains'][k] for b in BLOCKS for k in rules['blocks'][b]['keys']}
    for values in product(*(choices[k] for k in keys)):
        cfg = deepcopy(rules['anchor'])
        for k, v in zip(keys, values):
            _put(cfg, k, v)
        validate_config(cfg, rules)
        yield cfg


def inspect_configs(rules):
    """Anchor, every individual edge, and combined lower/upper extrema.

    The catalog is separately batch-validated in Go. These real inspections
    exercise data initialization extremes; they are not 6,561 separate loads.
    """
    _validate_rules(rules)
    out = [('anchor', deepcopy(rules['anchor']))]
    for block in BLOCKS:
        base = parameters(rules['anchor'], rules, block)
        for k in rules['blocks'][block]['keys']:
            for v in rules['blocks'][block]['domains'][k]:
                if v != base[k]:
                    p = dict(base, **{k:v})
                    out.append((block+'-'+k+'-'+str(v), compile_config(rules['anchor'], rules, block, p)))
    for edge, index in (('combined-lower', 0), ('combined-upper', -1)):
        cfg = deepcopy(rules['anchor'])
        for block in BLOCKS:
            cfg = compile_config(cfg, rules, block,
                  {k: rules['blocks'][block]['domains'][k][index] for k in rules['blocks'][block]['keys']})
        out.append((edge, cfg))
    assert len({space.identity(c) for _, c in out}) == len(out)
    return out


def neighborhood(central, rules):
    """Freeze 24 distinct adjacent single-then-double changes before evaluation.

    A central point may be at an edge. Eight keys still give at least eight
    single changes and 28 two-key changes. Results never influence this list.
    """
    validate_config(central, rules)
    keys = [k for b in BLOCKS for k in rules['blocks'][b]['keys']]
    alternatives = {}
    for b in BLOCKS:
        for k in rules['blocks'][b]['keys']:
            values = rules['blocks'][b]['domains'][k]; i = values.index(_get(central, k))
            alternatives[k] = [values[j] for j in (i-1, i+1) if 0 <= j < len(values)]
    points = []; seen = {space.identity(central)}
    for width in (1, 2):
        for subset in combinations(keys, width):
            for values in product(*(alternatives[k] for k in subset)):
                cfg = deepcopy(central)
                for k, v in zip(subset, values):
                    _put(cfg, k, v)
                validate_config(cfg, rules)
                ident = space.identity(cfg)
                if ident in seen:
                    raise ValueError('P4 neighbour compiler produced duplicate or zero-change configuration')
                seen.add(ident)
                points.append({'name':'n%02d'%(len(points)+1), 'config':cfg,
                               'config_identity':ident, 'changed_keys':list(subset),
                               'changes':v2.changes(central, cfg)})
                if len(points) == 24:
                    return {'version':'mechanism-p4-neighborhood-v1', 'skeleton':rules['skeleton'], 'central_identity':space.identity(central),
                            'domain_identity':space.identity(rules), 'ready':True, 'points':points,
                            'policy':'fixed key order, adjacent single then double changes; no outcome-driven replacement'}
    return {'version':'mechanism-p4-neighborhood-v1', 'skeleton':rules['skeleton'], 'central_identity':space.identity(central),
            'domain_identity':space.identity(rules), 'ready':False, 'points':points}


def score_pair(accounts):
    """The same six graded v2 violations, on two real 1.5-cost accounts."""
    if len(accounts) != 2:
        raise client.ProtocolError('P4 pair requires exactly two continuous accounts')
    rows = {}; periods = None
    for a in accounts:
        key = (a.get('funding_scenario'), a.get('cost_multiplier'))
        if key not in {('central',1.5), ('proxy-adverse',1.5)} or key in rows:
            raise client.ProtocolError('unexpected/duplicate P4 training account')
        if a.get('version') != metrics.VERSION or a.get('account_mode') != 'fixed-continuous-v1':
            raise client.ProtocolError('P4 continuous summary version differs')
        ps = [{k:h[k] for k in ('name','start','end')} for h in a['half_years']]
        metrics._periods(a, ps)
        if periods is not None and periods != ps:
            raise client.ProtocolError('P4 half-year periods differ')
        periods = ps
        m = a['metrics']; r = metrics._finite(m['net_return_pct'],'P4 return')
        dd = metrics._finite(m['max_drawdown_pct'],'P4 drawdown'); n = metrics._count(m['trades'],'P4 trades')
        if r <= -100 or not 0 <= dd <= 100:
            raise client.EvaluationError('P4 nonpositive equity/invalid drawdown')
        for h in a['half_years']:
            if metrics._finite(h['net_return_pct'],'P4 half return') <= -100:
                raise client.EvaluationError('P4 nonpositive half equity')
            metrics._count(h['trades'],'P4 half trades')
        near(sum(h['trades'] for h in a['half_years']), n, 'P4 trade sum')
        near(math.prod(1+h['net_return_pct']/100 for h in a['half_years']), 1+r/100, 'P4 compounded return')
        rows[key] = a
    worse = [min(a['half_years'][i]['net_return_pct']/100 for a in accounts) for i in range(6)]
    result = metrics._vector([a['metrics']['trades'] for a in accounts],
             [h['trades'] for a in accounts for h in a['half_years']],
             [a['metrics']['max_drawdown_pct']/100 for a in accounts], worse)
    growth = [365.25 / ((search.utc(p['end'])-search.utc(p['start'])).total_seconds()/86400) * math.log1p(r)
              for p,r in zip(periods,worse)]
    return {**result, 'violations':dict(zip(result['constraint_names'],result['constraint_vector'])),
            'score_contract':'continuous-mtm-six-vector-v2-p4-pair',
            'values':[statistics.median(growth),min(growth)], 'annual_log_growth':growth,
            'worst_half_returns_pct':[100*r for r in worse],
            'max_drawdown_pct':max(a['metrics']['max_drawdown_pct'] for a in accounts),
            'envelope_compound_return_pct':100*math.expm1(result['envelope_log_return']),
            'cost_pressure_status':'not checked by pair; final full six accounts required'}


def rank_key(candidate):
    s = candidate['score']
    return (-s['values'][1], -s['values'][0], s['max_drawdown_pct'],
            int(not candidate.get('anchor_preferred',candidate.get('is_anchor',False))), space.identity(candidate['config']))


def neighborhood_check(central, frozen, outcomes):
    if frozen['central_identity'] != space.identity(central['config']) or not frozen['ready']:
        raise client.ProtocolError('P4 central/frozen neighborhood differs or lacks 24 points')
    if len(outcomes) != 24 or {x['name'] for x in outcomes} != {p['name'] for p in frozen['points']}:
        raise client.ProtocolError('all 24 P4 outcomes including failures required')
    feasible = 0; values = []; changed = 0; coverage = {k:0 for b in BLOCKS for k in KEYS[frozen.get('skeleton','A')][b]}
    by = {p['name']:p for p in frozen['points']}
    for outcome in outcomes:
        point = by[outcome['name']]
        if outcome.get('config_identity') != point['config_identity']:
            raise client.ProtocolError('P4 neighborhood response configuration differs')
        for k in point['changed_keys']:
            coverage[k] = coverage.get(k,0)+1
        analysis = outcome.get('analysis')
        if analysis is None:
            values.append(float('-inf'))
        else:
            score = score_pair(analysis['accounts'])
            if score != analysis['score']:
                raise client.ProtocolError('P4 neighbor persisted score differs')
            feasible += int(score['feasible']); values.append(score['values'][0])
            changed += int(outcome.get('trading_path_changed',False))
    middle = statistics.median(values)
    center = central['score']['values'][0]
    return {'passed':feasible>=20 and center>0 and middle>=center*.7,
            'feasible_neighbors':feasible,'denominator':24,
            'median_neighbor_growth':middle if math.isfinite(middle) else None,
            'failed_growth_policy':'failures are negative infinity; never drop from denominator',
            'central_median_growth':center,'minimum_ratio':.7,'key_coverage':coverage,
            'trading_path_changed':changed,'trading_path_change_denominator':24,
            'interpretation':'local exposed-history sensitivity, not independent market samples'}

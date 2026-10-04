#!/usr/bin/env python3
"""P0 preparation and explicitly approved fixed P1; no search implementation.

The existing Go protocol and accounting verifier remain the only execution
contract. Old research is read only. This module owns a separate manifest and
SQLite budget, and never interprets preparation as market authorization.
"""
import argparse
from copy import deepcopy
import fcntl
from importlib.metadata import version
from pathlib import Path
import shutil
import sys
import time

from enhanced import strict_loads
import enhanced_space as space
import robust_client as client
from robust_budget import Budget, BudgetHalted, atomic_json, cache_identity
from robust_data import sha256, validate_bundle, utc_ms, INTERVALS
from robust_trace import verify_account
import robust_search as search

ROOT = Path(__file__).resolve().parents[1]
VERSION = 'mechanism-v2-p0'
LIMITS = {'trial_attempts': 0, 'go_requests': 13, 'ledger_evaluations': 78}
SUFFIXES = ('M-S', 'B-S', 'M-H4', 'B-H4')
HELPERS = ('mechanism_v2.py', 'mechanism_metrics.py', 'mechanism_sources.py',
           'robust_client.py', 'robust_budget.py', 'robust_data.py',
           'robust_trace.py', 'robust_search.py', 'enhanced_space.py',
           'feature_groups.py', 'enhanced.py', 'enhanced_protocol.py',
           'optimize.py', 'optimize_enhanced.py', 'requirements.lock')
RESOURCE = {'max_trace_bytes_per_request': 4294967296,
            'minimum_free_bytes_before_request': 8589934592}


def load(path):
    return strict_loads(Path(path).read_text())


def runtime_identity():
    # Imported v1 helpers depend on these versions; this never starts Optuna.
    return {'python': sys.version, 'optuna': version('optuna'), 'numpy': version('numpy')}


def changes(a, b, prefix=''):
    out = []
    if isinstance(a, dict) and isinstance(b, dict) and a.keys() == b.keys():
        for k in sorted(a):
            out.extend(changes(a[k], b[k], prefix + '.' + k if prefix else k))
    elif space.canonical(a) != space.canonical(b):
        out.append({'field': prefix, 'from': a, 'to': b})
    return out


def unique_features(config):
    features = config['classifier']['feature_group']['features']
    if len({space.canonical(f) for f in features}) != len(features):
        raise ValueError('duplicate complete feature specification')


def legal_neighbors(values, lookback, stride):
    if type(lookback) is not int or type(stride) is not int or stride < 1 or lookback < 4:
        raise ValueError('invalid frozen lookback/stride')
    return [k for k in values if type(k) is int and 1 <= k <= (lookback - 3) // stride]


def freeze_local_rules(anchor, defaults, active_keys=()):
    """Freeze +/- one original grid step; references can never widen this domain.

    P0 calls this with no active keys. Choosing future search keys is a separate
    P4 approval, not an implicit search budget or sampler.
    """
    unique_features(anchor)
    params = search.active_parameters(anchor, defaults)
    grids = search.numeric_domains()
    extras = {f['name'] for f in anchor['classifier']['feature_group']['features']}
    structural = {'feature_count', 'max_bars_back', 'sample_stride'}
    context = ({'daily_h', 'daily_r', 'daily_x'} if 'D1_SLOPE' in extras else set())
    if 'KERNEL_DEVIATION' in extras:
        context |= {'envelope_h', 'envelope_r', 'envelope_x', 'envelope_atr_length'}
    if len(set(active_keys)) != len(active_keys) or any(k not in params or k not in grids or k in structural | context for k in active_keys):
        raise ValueError('active keys must be nonstructural numeric keys outside frozen feature context')
    rows = {}
    for key, value in params.items():
        prediction = key.startswith(('f0_', 'f1_', 'f2_', 'f3_', 'f4_', 'rvol_', 'atr_price_', 'd1_slope_', 'kernel_deviation_')) or key in {'neighbors', 'min_vote_fraction', 'vote_half_life', 'rank_half_life'}
        effect = 'prediction' if prediction else 'management' if key.startswith(('risk_', 'trail_', 'breakeven_')) or key in {'entry_mode', 'pullback_wait', 'exit_policy', 'max_hold_bars', 'near', 'far_gap'} else 'filter/context'
        domain = [value]
        if key in active_keys:
            grid = grids[key]
            i = search._grid_index(grid, value)
            domain = grid[max(0, i-1):i+2]
            if key == 'neighbors' and anchor['classic']['algorithm'] == 'aligned-knn':
                domain = legal_neighbors(domain, anchor['classic']['max_bars_back'], anchor['classic']['sample_stride'])
            if value not in domain:
                raise ValueError('anchor outside conditional legal domain')
        rows[key] = {'anchor': value, 'active': key in active_keys, 'domain': domain,
                     'category': 'numeric' if key in grids else 'structural', 'effect': effect,
                     'feature_context_frozen': key in context}
    return {'version': 'single-anchor-one-grid-v1', 'config_identity': space.identity(anchor),
            'reference_rule': 'keep frozen domain when constant/insufficient; never widen',
            'feature_specs': deepcopy(anchor['classifier']['feature_group']['features']),
            'memory_label_vote_contract': 'unchanged v1 algorithm/label/vote/absolute stride',
            'keys': rows}


def matrix(sources, defaults):
    jobs = []
    for name in ('A', 'B'):
        anchor = deepcopy(sources['anchors'][name]['config'])
        if space.normalize_config(anchor, defaults) != anchor:
            raise ValueError('anchor is not its complete normalized configuration')
        for suffix in SUFFIXES:
            cfg = deepcopy(anchor)
            both = suffix.startswith('B-')
            cfg['entry_mode'] = 'both' if both else 'main'
            cfg['pullback']['enabled'] = both
            cfg['envelope_enabled'] = both
            if both and name == 'B':
                cfg['envelope'] = {'h':8, 'r':8, 'x':25, 'atr_length':60, 'near':1.5, 'far':8}
                cfg['pullback']['max_wait_bars'] = 24
            cfg['exit'] = {'policy': 'four-bars' if suffix.endswith('H4') else 'signals', 'max_hold_bars':4}
            cfg = space.normalize_config(cfg, defaults)
            unique_features(cfg)
            # Only experiment factors and their inactive dependency defaults may differ.
            allowed = {'entry_mode', 'envelope_enabled', 'pullback.enabled', 'pullback.max_wait_bars',
                       'exit.policy', 'exit.max_hold_bars'} | {'envelope.'+k for k in defaults['envelope']}
            diff = changes(anchor, cfg)
            if any(d['field'] not in allowed for d in diff):
                raise ValueError('matrix changed an unrelated classifier/daily/risk field')
            if cfg['classifier'] != anchor['classifier'] or cfg['daily'] != anchor['daily'] or cfg['risk'] != anchor['risk']:
                raise ValueError('matrix active skeleton differs')
            if any(f['name']=='KERNEL_DEVIATION' for f in cfg['classifier']['feature_group']['features']):
                for k in ('h','r','x','atr_length'):
                    if cfg['envelope'][k] != anchor['envelope'][k]:
                        raise ValueError('matrix changed active envelope feature context')
            jobs.append({'name':name+'-'+suffix, 'role':'matrix', 'skeleton':name,
                         'config':cfg, 'config_identity':space.identity(cfg), 'changes_from_anchor':diff})
    original = deepcopy(sources['anchors']['B']['config'])
    jobs.append({'name':'B-original', 'role':'original', 'skeleton':'B',
                 'config':original, 'config_identity':space.identity(original), 'changes_from_anchor':[]})
    if jobs[1]['config'] != sources['anchors']['A']['config']:
        raise ValueError('A-original must equal A-B-S')
    for name in ('classic','aligned'):
        cfg = deepcopy(sources['public_controls'][name]['config'])
        unique_features(cfg)
        jobs.append({'name':'Public-'+name, 'role':'public-contract-preserved',
                     'config':cfg, 'config_identity':space.identity(cfg), 'changes_from_anchor':[]})
    if len({j['config_identity'] for j in jobs}) != 11:
        raise ValueError('P1 requires exactly eleven unique configurations')
    for original in ('A-B-S', 'B-original'):
        base = next(j for j in jobs if j['name']==original)
        jobs.append({'name':'replay-'+original, 'role':'independent-replay', 'replay_of':original,
                     'config':deepcopy(base['config']), 'config_identity':base['config_identity']})
    return jobs


def account_plan(bundle):
    return {'version':'robust-eval-v1', 'bundle_dir':str(Path(bundle).resolve()), 'interval':'1h',
            'history_start':'2020-01-01T00:00:00Z',
            'windows':[{'name':'mechanism-continuous', 'start':'2021-01-01T00:00:00Z', 'end':'2024-01-01T00:00:00Z'}],
            'account_mode':'fixed-continuous-v1', 'funding_mode':'proxy-stress-v1',
            'funding_scenarios':['central','proxy-adverse'], 'cost_multipliers':[1,1.5,2]}


def prepare(out, source_run, bundle, binary):
    from mechanism_sources import extract_sources
    out, binary, bundle = Path(out).resolve(), Path(binary).resolve(), Path(bundle).resolve()
    if out.exists():
        raise FileExistsError('exclusive new preparation directory required')
    b = validate_bundle(bundle)
    if b['interval']!='1h' or b['symbol']!='ETHUSDT':
        raise ValueError('P0 is scoped to ETHUSDT 1h')
    sources, rescored = extract_sources(source_run, binary)
    defaults = space.go_defaults(binary)
    jobs = matrix(sources, defaults)
    space.go_validate(binary, [j['config'] for j in jobs[:11]])
    pp = account_plan(bundle)
    inspections = []
    for j in jobs[:11]:
        ready = client.inspect(binary, pp, j['config'])
        inspections.append({'name':j['name'], 'config_identity':j['config_identity'],
                            'ready':ready, 'evaluation_calls':0})
    # Replays share the fully inspected configuration and plan, not an uninspected request.
    for j in jobs[11:]:
        base = next(x for x in inspections if x['name']==j['replay_of'])
        inspections.append({'name':j['name'], 'config_identity':j['config_identity'],
                            'same_inspected_contract_as':j['replay_of'], 'ready':deepcopy(base['ready']), 'evaluation_calls':0})
    out.mkdir(parents=True)
    atomic_json(out/'plan.json', pp)
    atomic_json(out/'sources.json', sources)
    atomic_json(out/'w1-rescore.json', rescored)
    atomic_json(out/'matrix.json', jobs)
    atomic_json(out/'inspect.json', inspections)
    for j in jobs[:11]:
        atomic_json(out/'configs'/(j['name']+'.json'), j['config'])
    atomic_json(out/'local-rules.json', {k:freeze_local_rules(sources['anchors'][k]['config'],defaults) for k in ('A','B')})
    files = [p for p in out.rglob('*') if p.is_file()]
    contract = {'version':VERSION, 'symbol':'ETHUSDT', 'data_use':'exposed-retrospective',
                'engine':{'path':str(binary), 'sha256':sha256(binary)}, 'runtime':runtime_identity(),
                'bundle_dir':str(bundle), 'bundle_identity':client.bundle_identity(bundle),
                'helpers':{str(ROOT/'tuning'/n):sha256(ROOT/'tuning'/n) for n in HELPERS},
                'plan_document':{'path':str(ROOT/'MECHANISM_V2_PLAN.md'), 'sha256':sha256(ROOT/'MECHANISM_V2_PLAN.md')},
                'prepared_files':{str(p):sha256(p) for p in files}, 'plan':pp, 'jobs':jobs,
                'budget':deepcopy(LIMITS), 'resource_limits':deepcopy(RESOURCE),
                'inspect_completed':True, 'evaluation_authorized':False,
                'old_budget_access':'read-only extraction; no cross-budget cache import'}
    atomic_json(out/'manifest.json', {'version':VERSION, 'contract_sha256':space.identity(contract), 'contract':contract})
    atomic_json(out/'approval-template.json', {'version':'mechanism-v2-approval-v1', 'manifest_sha256':sha256(out/'manifest.json'),
                'approved':False, 'authorized_by':None, 'authorization_reference':None, 'batch':'P1',
                'budget':deepcopy(LIMITS), 'funding_modes':['proxy-stress-v1']})
    atomic_json(out/'preparation.json', {'status':'prepared-inspected', 'real_historical_evaluations':0,
                'inspect_process_calls':11, 'inspected_requests':13, 'unique_configs':11, 'synthetic_evaluations':0,
                'optuna_trials':0, 'budget_consumed':dict.fromkeys(LIMITS,0), 'engineering_audit':'pending'})
    return {'out':str(out), 'status':'prepared-inspected', 'real_historical_evaluations':0, 'budget_upper_bound':LIMITS}


def load_run(out):
    out = Path(out).resolve()
    m = load(out/'manifest.json'); c = m['contract']
    if m.get('version')!=VERSION or m.get('contract_sha256')!=space.identity(c):
        raise ValueError('manifest contract differs')
    if c['runtime']!=runtime_identity() or sha256(c['engine']['path'])!=c['engine']['sha256']:
        raise ValueError('frozen runtime/engine changed')
    for path, digest in {**c['helpers'], **c['prepared_files'], c['plan_document']['path']:c['plan_document']['sha256']}.items():
        if sha256(path)!=digest:
            raise ValueError('frozen implementation/preparation/plan changed: '+path)
    if client.bundle_identity(c['bundle_dir'])!=c['bundle_identity']:
        raise ValueError('frozen bundle changed')
    if c['budget']!=LIMITS or not c['inspect_completed'] or c['evaluation_authorized'] is not False:
        raise ValueError('P1 frozen scope differs')
    return m


def authorize(out, approval):
    m = load_run(out); a = load(approval)
    if (a.get('version')!='mechanism-v2-approval-v1' or a.get('manifest_sha256')!=sha256(Path(out)/'manifest.json')
        or a.get('approved') is not True or a.get('authorized_by')!='user' or a.get('batch')!='P1'
        or not isinstance(a.get('authorization_reference'),str) or not a['authorization_reference'].strip()
        or a.get('budget')!=LIMITS or a.get('funding_modes')!=['proxy-stress-v1']):
        raise ValueError('P1 needs explicit user approval binding complete manifest and 13 Go/78 ledgers/0 trials')
    return m


def resources_ready(out, c):
    pp = c['plan']
    bars = sum((utc_ms(w['end'])-utc_ms(w['start']))//INTERVALS[pp['interval']] for w in pp['windows'])
    estimate = bars*6000*6
    limits = c['resource_limits']
    if estimate>limits['max_trace_bytes_per_request'] or shutil.disk_usage(out).free<max(limits['minimum_free_bytes_before_request'],estimate*2):
        raise ValueError('trace storage/free-space guard failed')
    return estimate


def verified_result(c, result, config):
    ready = {'plan':client.normalize_plan(c['plan']), 'bundle_identity':c['bundle_identity'], 'ledger_evaluations':6}
    client.validate_result(result, c['plan'], config, ready)
    for a in result['accounts']:
        verify_account(a, c['bundle_dir'])
    return result


def request(out, c, budget, job, evaluator):
    halted = budget.halted()
    if halted:
        raise BudgetHalted(halted)
    token = 'P1/'+job['name']
    record = budget.record(token)
    if record:
        if record['status']=='complete':
            response = budget.cached_response(record['cache_key'])
            if response is None:
                raise client.ProtocolError('completed request has no durable cached result')
            return verified_result(c, response, job['config'])
        if record['status'] in ('failed','interrupted'):
            return None
        raise ValueError('unknown request must be recovered, never retried')
    key = cache_identity(c['plan'], job['config'])
    # The budget binding contains engine/helpers/data/plan identity. Replay uses
    # a unique key and a new Go process, and never imports an old budget cache.
    replay = job['role']=='independent-replay'
    if replay:
        key = space.identity({'key':key, 'independent_replay':token})
    reservation = budget.reserve_evaluation('P1',token,key,6,fixed=True)
    if not reservation['send']:
        return verified_result(c,reservation['response'],job['config'])
    try:
        resources_ready(out,c)
        trace = Path(out)/'execution'/'traces'/job['name']
        start = time.monotonic()
        response = (client.evaluate(c['engine']['path'],c['plan'],job['config'],trace)
                    if replay else evaluator(c['plan'],job['config'],trace))
        verified_result(c,response,job['config'])
        response['wrapper_wall_seconds'] = time.monotonic()-start
        budget.finish(token,response=response)
        return response
    except client.EvaluationError as e:
        budget.finish(token,error=e)
        return None
    except BaseException as e:
        if isinstance(e,(KeyboardInterrupt,SystemExit)):
            raise
        budget.finish(token,error=e); budget.halt('identity/protocol/accounting/unknown request fault: '+str(e))
        raise


def economic_response(result):
    value = deepcopy(result)
    for key in ('id','timing','load_seconds','wrapper_wall_seconds'):
        value.pop(key,None)
    for a in value['accounts']:
        a.pop('trace_path',None)
    return value


def paired_report(reports, sources, defaults):
    """Predeclared adjacent contrasts and original-reference P2 eligibility.

    This consumes successful P1 summaries only, never sends another evaluation.
    Missing cells remain explicit. A difference of differences describes an
    interaction; differences from the original do not identify one factor.
    """
    by = {r['name']:r for r in reports}
    def index(name):
        return {(a['funding_scenario'],a['cost_multiplier']):a for a in by[name]['accounts']}
    def contrast(before, after):
        if before not in by or after not in by:
            return {'before':before,'after':after,'complete':False,'accounts':[]}
        left,right=index(before),index(after)
        rows=[]
        for key,a in left.items():
            b=right[key]
            rows.append({'funding_scenario':key[0],'cost_multiplier':key[1],
                         'net_return_pp':b['metrics']['net_return_pct']-a['metrics']['net_return_pct'],
                         'full_drawdown_pp':b['metrics']['max_drawdown_pct']-a['metrics']['max_drawdown_pct'],
                         'trade_count_delta':b['metrics']['trades']-a['metrics']['trades'],
                         'half_return_pp':[y['net_return_pct']-x['net_return_pct'] for x,y in zip(a['half_years'],b['half_years'])],
                         'half_exit_count_delta':[y['trades']-x['trades'] for x,y in zip(a['half_years'],b['half_years'])]})
        return {'before':before,'after':after,'complete':True,'accounts':rows}
    pairs,interactions,eligibility,selected=[],[],[],[]
    original_names={'A':'A-B-S','B':'B-original'}
    for skeleton in ('A','B'):
        names=[skeleton+'-'+s for s in SUFFIXES]
        adjacent=[contrast(names[0],names[1]),contrast(names[2],names[3]),
                  contrast(names[0],names[2]),contrast(names[1],names[3])]
        pairs.append({'skeleton':skeleton,'entry_signals':adjacent[0],'entry_H4':adjacent[1],
                      'exit_main':adjacent[2],'exit_both':adjacent[3]})
        interaction={'skeleton':skeleton,'complete':all(p['complete'] for p in adjacent),'accounts':[]}
        if interaction['complete']:
            for a,b in zip(adjacent[2]['accounts'],adjacent[3]['accounts']):
                interaction['accounts'].append({'funding_scenario':a['funding_scenario'],'cost_multiplier':a['cost_multiplier'],
                    'net_return_difference_pp':b['net_return_pp']-a['net_return_pp'],
                    'half_return_difference_pp':[y-x for x,y in zip(a['half_return_pp'],b['half_return_pp'])]})
        interactions.append(interaction)
        original=original_names[skeleton]
        eligible=[]
        candidates=list(dict.fromkeys(names+[original]))
        for name in candidates:
            entry={'name':name,'original_reference':original,'eligible':False,'improvement_checks':[]}
            if name in by and original in by:
                if name!=original:
                    for scene in ('central','proxy-adverse'):
                        a,b=index(original)[(scene,1.5)],index(name)[(scene,1.5)]
                        count=sum(y['net_return_pct']>=x['net_return_pct'] for x,y in zip(a['half_years'],b['half_years']))
                        entry['improvement_checks'].append({'funding_scenario':scene,'halves_not_worse':count,
                            'total_return_higher':b['metrics']['net_return_pct']>a['metrics']['net_return_pct'],
                            'full_dd_not_worse':b['metrics']['max_drawdown_pct']<=a['metrics']['max_drawdown_pct']})
                entry['eligible']=by[name]['v2_development_score']['feasible'] and all(
                    x['halves_not_worse']>=4 and x['total_return_higher'] and x['full_dd_not_worse'] for x in entry['improvement_checks'])
                if entry['eligible']:
                    score=by[name]['v2_development_score']
                    cfg=next(j['config'] for j in matrix(sources,defaults) if j['name']==name)
                    eligible.append(((-score['values'][1],-score['values'][0],score['max_drawdown_pct'],
                                      search.complexity(cfg,defaults), sources['anchors'][skeleton]['source_key'],candidates.index(name)),name))
            eligibility.append(entry)
        if eligible:
            selected.append({'skeleton':skeleton,'name':min(eligible)[1]})
    diagnostics=[]
    for name,report in by.items():
        skeleton='A' if name.startswith('A-') else 'B' if name.startswith('B-') else None
        original=original_names.get(skeleton)
        diagnostics.append({'name':name,'old_fold_diagnostic':sources['anchors'][skeleton]['old_fold_diagnostic'] if name==original else None,
                            'policy':'existing exact original configuration only; new cells left empty; never concatenate folds'})
    complete=all(n in by for s in ('A','B') for n in [s+'-'+x for x in SUFFIXES])
    return {'adjacent_pairs':pairs,'interactions':interactions,'original_reference_eligibility':eligibility,
            'selected_research_objects':selected,'diagnostic_complete':complete,'old_fold_diagnostics':diagnostics,
            'P3_P4_stop_if_no_eligible_structure':not selected,'P3_P4_authorized':False,
            'selection_status':'diagnostic-pending-independent-result-audit'}


def run(out, approval):
    from mechanism_metrics import summarize_continuous_account, continuous_score
    out = Path(out).resolve(); c = authorize(out,approval)['contract']
    execution = out/'execution'; execution.mkdir(exist_ok=True)
    # Lock before creation/recovery prevents another process marking live work unknown.
    with (execution/'run.lock').open('a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        path = execution/'budget.sqlite'
        if not path.exists() and any(p.name!='run.lock' for p in execution.iterdir()):
            raise ValueError('existing execution lost durable budget; refuse reset/replay')
        budget = Budget(path, {'manifest_contract_sha256':space.identity(c)},LIMITS,{'P1':LIMITS})
        halted = budget.halted()
        if halted:
            raise BudgetHalted(halted)
        budget.recover('P1')
        records = []
        with client.Evaluator(c['engine']['path']) as evaluator:
            for job in c['jobs']:
                # Recheck frozen inputs even when the next request is a cache hit/replay.
                load_run(out)
                result = request(out,c,budget,job,evaluator)
                record = {'name':job['name'], 'role':job['role'], 'status':budget.record('P1/'+job['name'])['status'], 'result':result}
                records.append(record)
                atomic_json(execution/'summary.json',{'status':'in-progress','records':records,'budget':budget.snapshot(False)})
        by = {r['name']:r['result'] for r in records}
        checks = []
        for job in c['jobs']:
            if 'replay_of' in job:
                a,b = by[job['name']],by[job['replay_of']]
                if a is not None and b is not None:
                    if space.canonical(economic_response(a))!=space.canonical(economic_response(b)):
                        budget.halt('independent economic replay differs')
                        raise client.ProtocolError('independent full response/trace replay differs')
                    checks.append({'name':job['name'],'matched_all_metrics_and_economic_trace':True})
                else:
                    checks.append({'name':job['name'],'matched_all_metrics_and_economic_trace':False,'reason':'ordinary/unknown source or replay failure'})
        reports = []
        try:
            for record in records:
                if record['result'] is None:
                    continue
                summaries = [summarize_continuous_account(a,bundle=c['bundle_dir']) for a in record['result']['accounts']]
                reports.append({'name':record['name'],'accounts':summaries,'v2_development_score':continuous_score(summaries)})
        except BaseException as e:
            budget.halt('continuous summary/accounting fault: '+str(e)); raise
        pairing=paired_report(reports,load(out/'sources.json'),space.go_defaults(c['engine']['path']))
        state = {'version':VERSION,'status':'complete' if all(r['status']=='complete' for r in records) else 'complete-with-failures',
                 'evidence':'exposed retrospective development; no future validation', 'records':records,'reports':reports,
                 'checks':checks,'pairing':pairing,'budget':budget.snapshot(False),'independent_result_audit':'pending',
                 'later_stage_authorized':False}
        atomic_json(execution/'summary.json',state)
        return {'status':state['status'],'budget':budget.counts(),'summary':str(execution/'summary.json')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command',required=True)
    p = sub.add_parser('prepare')
    p.add_argument('--source-run',type=Path,default=ROOT/'results/robust-v1/preparation-v3/ETHUSDT/1h')
    p.add_argument('--bundle',type=Path,default=ROOT/'data/ETHUSDT-robust-v1/1h')
    p.add_argument('--binary',type=Path,default=ROOT/'bin/lorentz-robust-v1')
    p.add_argument('--out',type=Path,required=True)
    p = sub.add_parser('inspect')
    p.add_argument('--run',type=Path,required=True)
    p = sub.add_parser('run')
    p.add_argument('--run',type=Path,required=True); p.add_argument('--approval',type=Path,required=True)
    p = sub.add_parser('report')
    p.add_argument('--run',type=Path,required=True)
    args = parser.parse_args()
    if args.command=='prepare':
        result = prepare(args.out,args.source_run,args.bundle,args.binary)
    elif args.command=='inspect':
        m = load_run(args.run)
        result = {'manifest_verified':True, 'real_historical_evaluations':0, 'inspect':load(args.run/'inspect.json'), 'budget_upper_bound':m['contract']['budget']}
    elif args.command=='run':
        result = run(args.run,args.approval)
    else:
        load_run(args.run); result = load(args.run/'execution'/'summary.json')
    print(space.canonical(result))


if __name__=='__main__':
    main()

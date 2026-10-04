#!/usr/bin/env python3
"""Bounded P3 preparation and separately approved fixed execution; no search."""
import argparse
from copy import deepcopy
import fcntl
from pathlib import Path
import time

import enhanced_space as space
import mechanism_v2 as v2
import mechanism_p3_client as client
from mechanism_metrics import summarize_continuous_account, continuous_score
from robust_budget import Budget, BudgetHalted, atomic_json, cache_identity
from robust_data import sha256, validate_bundle
from robust_trace import verify_account
import robust_search as search

ROOT = v2.ROOT
VERSION = 'mechanism-p3-v1'
LIMITS = {'trial_attempts': 0, 'go_requests': 10, 'ledger_evaluations': 60}
ORIGINAL_NAMES = {'A': 'A-B-S', 'B': 'B-original'}
HELPERS = tuple(dict.fromkeys(v2.HELPERS + ('mechanism_p3.py', 'mechanism_p3_client.py', 'build_mechanism_p3.py')))


def read_source(source_run):
    """Read the already audited P1 outputs; never open its budget for writing."""
    source_run = Path(source_run).resolve()
    m = v2.load_run(source_run)
    summary_path = source_run/'execution/summary.json'
    audit_path = source_run/'execution/result-audit.json'
    s, a = v2.load(summary_path), v2.load(audit_path)
    if (s.get('status') != 'complete' or s.get('independent_result_audit') != 'closed'
        or a.get('audit_status') != 'closed' or a.get('findings') != []
        or a.get('manifest_sha256') != sha256(source_run/'manifest.json')
        or space.identity({k: s[k] for k in a['economic_content_fields']}) != a.get('economic_content_identity')):
        raise ValueError('P3 requires closed independent P1 result audit')
    selected = s['pairing']['selected_research_objects']
    if selected != [{'name': ORIGINAL_NAMES[k], 'skeleton': k} for k in ('A', 'B')]:
        raise ValueError('this fixed P3 batch requires the two audited original A/B structures')
    by = {r['name']: r for r in s['records']}
    originals = {}
    for skeleton, name in ORIGINAL_NAMES.items():
        r = by[name]
        if r['status'] != 'complete' or r['result'] is None:
            raise ValueError('missing original response')
        originals[skeleton] = deepcopy(r['result'])
    sources = v2.load(source_run/'sources.json')
    files = [source_run/'manifest.json', summary_path, audit_path, source_run/'sources.json']
    return {'source_run': str(source_run), 'source_files': {str(p): sha256(p) for p in files},
            'source_manifest_contract': m['contract_sha256'], 'originals': originals,
            'source_keys': {k: sources['anchors'][k]['source_key'] for k in ORIGINAL_NAMES},
            'w1_rejection_unchanged': True, 'evidence': 'exposed retrospective development'}


def matrix(source):
    jobs = []
    for skeleton in ('A', 'B'):
        cfg = source['originals'][skeleton]['config']
        for variant in client.VARIANTS:
            wrapped = client.wrapper(cfg, variant)
            jobs.append({'name': skeleton+'-'+variant, 'skeleton': skeleton, 'role': 'fixed-control',
                         'config': wrapped, 'config_identity': space.identity(wrapped),
                         'strategy_identity': space.identity(cfg), 'changes_from_original': {'variant': variant}})
    for skeleton in ('A', 'B'):
        job = deepcopy(next(j for j in jobs if j['skeleton'] == skeleton and j['config']['variant'] == 'classic-original'))
        job.update(name='replay-'+job['name'], role='independent-replay', replay_of=job['name'])
        jobs.append(job)
    if len({j['config_identity'] for j in jobs}) != 8:
        raise ValueError('P3 requires eight distinct experimental configurations')
    return jobs


def prepare(out, source_run, binary, build_manifest):
    out, binary, build_manifest = map(lambda p: Path(p).resolve(), (out, binary, build_manifest))
    if out.exists():
        raise FileExistsError('exclusive new P3 preparation required')
    source = read_source(source_run)
    plan = deepcopy(source['originals']['A']['plan'])
    if space.canonical(plan) != space.canonical(source['originals']['B']['plan']):
        raise ValueError('original source account plans differ')
    if plan != v2.account_plan(plan['bundle_dir']):
        raise ValueError('P3 requires unchanged P1 continuous account plan')
    b = validate_bundle(plan['bundle_dir'])
    if b['symbol'] != 'ETHUSDT' or b['interval'] != '1h':
        raise ValueError('P3 scope is ETHUSDT 1h')
    build = v2.load(build_manifest)
    jobs = matrix(source)
    client.go_validate(binary, [j['config'] for j in jobs[:8]])
    inspections = []
    for j in jobs[:8]:
        ready = client.inspect(binary, plan, j['config'])
        inspections.append({'name': j['name'], 'config_identity': j['config_identity'], 'ready': ready, 'evaluation_calls': 0})
    for j in jobs[8:]:
        ready = deepcopy(next(r['ready'] for r in inspections if r['name'] == j['replay_of']))
        inspections.append({'name': j['name'], 'config_identity': j['config_identity'], 'ready': ready,
                            'same_inspected_contract_as': j['replay_of'], 'evaluation_calls': 0})
    out.mkdir(parents=True)
    atomic_json(out/'plan.json', plan)
    atomic_json(out/'lineage.json', source)
    atomic_json(out/'matrix.json', jobs)
    atomic_json(out/'inspect.json', inspections)
    for j in jobs[:8]:
        atomic_json(out/'configs'/(j['name']+'.json'), j['config'])
    files = [p for p in out.rglob('*') if p.is_file()]
    contract = {'version': VERSION, 'symbol': 'ETHUSDT', 'data_use': 'exposed-retrospective',
                'engine': {'path': str(binary), 'sha256': sha256(binary)},
                'build_manifest': {'path': str(build_manifest), 'sha256': sha256(build_manifest), 'content': build},
                'runtime': v2.runtime_identity(), 'bundle_dir': plan['bundle_dir'],
                'bundle_identity': client.base.bundle_identity(plan['bundle_dir']), 'plan': plan,
                'helpers': {str(ROOT/'tuning'/n): sha256(ROOT/'tuning'/n) for n in HELPERS},
                'plan_documents': {str(ROOT/n): sha256(ROOT/n) for n in ('MECHANISM_V2_PLAN.md', 'MECHANISM_P3_CONTRACT.md')},
                'prepared_files': {str(p): sha256(p) for p in files}, 'jobs': jobs,
                'budget': deepcopy(LIMITS), 'resource_limits': deepcopy(v2.RESOURCE),
                'inspect_completed': True, 'evaluation_authorized': False,
                'source_files': source['source_files'], 'old_budget_access': 'read-only results; no cross-budget cache import',
                'mechanism_contract': client.GO_CONTRACT, 'wrapper_version': client.CONFIG_VERSION}
    atomic_json(out/'manifest.json', {'version': VERSION, 'contract_sha256': space.identity(contract), 'contract': contract})
    atomic_json(out/'approval-template.json', {'version': 'mechanism-p3-approval-v1',
                'manifest_sha256': sha256(out/'manifest.json'), 'approved': False, 'authorized_by': None,
                'authorization_reference': None, 'batch': 'P3', 'budget': deepcopy(LIMITS), 'funding_modes': ['proxy-stress-v1']})
    atomic_json(out/'preparation.json', {'status': 'prepared-inspected', 'real_historical_evaluations': 0,
                'inspect_process_calls': 8, 'inspected_requests': 10, 'unique_configs': 8,
                'optuna_trials': 0, 'budget_consumed': dict.fromkeys(LIMITS, 0), 'engineering_audit': 'pending',
                'historical_original_compatibility': 'pending separately approved P3 requests'})
    # Validate all identities before returning a prepared artifact.
    load_run(out)
    return {'out': str(out), 'status': 'prepared-inspected', 'historical_evaluations': 0, 'budget_upper_bound': LIMITS}


def load_run(out):
    out = Path(out).resolve()
    m = v2.load(out/'manifest.json'); c = m['contract']
    if m.get('version') != VERSION or m.get('contract_sha256') != space.identity(c):
        raise ValueError('P3 manifest contract differs')
    if c['runtime'] != v2.runtime_identity() or sha256(c['engine']['path']) != c['engine']['sha256']:
        raise ValueError('frozen P3 runtime/engine changed')
    bound = {**c['helpers'], **c['prepared_files'], **c['plan_documents'], **c['source_files'],
             c['build_manifest']['path']: c['build_manifest']['sha256']}
    for path, digest in bound.items():
        if sha256(path) != digest:
            raise ValueError('frozen P3 dependency changed: '+path)
    # Build metadata binds actual source/overlay inputs. See builder contract.
    build = c['build_manifest']['content']
    for path, digest in build['bound_files'].items():
        if sha256(path) != digest:
            raise ValueError('experimental build input changed: '+path)
    if sha256(c['engine']['path']) != build['binary']['sha256'] or str(Path(build['binary']['path']).resolve()) != c['engine']['path']:
        raise ValueError('engine differs from actual experimental build')
    if client.base.bundle_identity(c['bundle_dir']) != c['bundle_identity']:
        raise ValueError('P3 frozen bundle changed')
    if (c['budget'] != LIMITS or not c['inspect_completed'] or c['evaluation_authorized'] is not False
        or c['mechanism_contract'] != client.GO_CONTRACT or c['wrapper_version'] != client.CONFIG_VERSION):
        raise ValueError('P3 frozen scope differs')
    source = v2.load(out/'lineage.json')
    if c['jobs'] != matrix(source):
        raise ValueError('P3 frozen matrix differs from audited original structures')
    v2.load_run(source['source_run'])
    return m


def authorize(out, approval):
    out = Path(out).resolve(); m = load_run(out); a = v2.load(approval)
    if (a.get('version') != 'mechanism-p3-approval-v1' or a.get('manifest_sha256') != sha256(out/'manifest.json')
        or a.get('approved') is not True or a.get('authorized_by') != 'user' or a.get('batch') != 'P3'
        or not isinstance(a.get('authorization_reference'), str) or not a['authorization_reference'].strip()
        or a.get('budget') != LIMITS or a.get('funding_modes') != ['proxy-stress-v1']):
        raise ValueError('P3 needs separate user approval binding 10 Go/60 ledgers/0 trials')
    audit = v2.load(out/'engineering-audit.json')
    if (audit.get('audit_status') != 'closed' or audit.get('findings') != []
        or audit.get('manifest_sha256') != sha256(out/'manifest.json')):
        raise ValueError('P3 independent engineering audit is not closed for this manifest')
    return m


def verified_result(c, result, config):
    ready = {'plan': client.base.normalize_plan(c['plan']), 'bundle_identity': c['bundle_identity'], 'ledger_evaluations': 6}
    client.validate_result(result, c['plan'], config, ready)
    client.verify_state_trace(result)
    for a in result['accounts']:
        verify_account(a, c['bundle_dir'])
    return result


def checked_response(out, c, budget, job, response):
    """Check compatibility before a new response enters the successful cache."""
    verified_result(c, response, job['config'])
    if job['config']['variant'] == 'classic-original':
        source = v2.load(Path(out)/'lineage.json')
        original = source['originals'][job['skeleton']]
        if space.canonical(economic_response(response, legacy=True)) != space.canonical(v2.economic_response(original)):
            raise client.ProtocolError('new original mode differs from preserved P1 economic response/trace')
    if 'replay_of' in job:
        base = budget.record('P3/'+job['replay_of'])
        if base and base['status'] == 'complete':
            original = budget.cached_response(base['cache_key'])
            if original is None:
                raise client.ProtocolError('independent replay source lost cached response')
            verified_result(c, original, job['config'])
            if space.canonical(economic_response(response)) != space.canonical(economic_response(original)):
                raise client.ProtocolError('independent P3 economic/state replay differs')
    return response


def checked_cache(out, c, budget, job, response):
    if response is None:
        raise client.ProtocolError('completed P3 request lost response')
    return checked_response(out, c, budget, job, response)


def request(out, c, budget, job, evaluator):
    if budget.halted():
        raise BudgetHalted(budget.halted())
    token = 'P3/'+job['name']
    try:
        record = budget.record(token)
        if record:
            if record['status'] == 'complete':
                return checked_cache(out, c, budget, job, budget.cached_response(record['cache_key']))
            if record['status'] in ('failed', 'interrupted'):
                return None
            raise ValueError('unknown P3 request must be recovered, never retried')
        key = cache_identity(c['plan'], job['config'])
        replay = job['role'] == 'independent-replay'
        if replay:
            key = space.identity({'key': key, 'independent_replay': token})
        reservation = budget.reserve_evaluation('P3', token, key, 6, fixed=True)
        if not reservation['send']:
            return checked_cache(out, c, budget, job, reservation['response'])
    except BaseException as e:
        if not isinstance(e, (KeyboardInterrupt, SystemExit)):
            # Includes cache JSON parsing inside Budget; terminal rows are not
            # finished again, and rolled-back alias reservations stay rolled back.
            budget.halt('P3 cached/reservation identity/protocol/accounting fault: '+str(e))
        raise
    try:
        v2.resources_ready(out, c)
        trace = Path(out)/'execution/traces'/job['name']
        start = time.monotonic()
        r = (client.evaluate(c['engine']['path'], c['plan'], job['config'], trace)
             if replay else evaluator(c['plan'], job['config'], trace))
        checked_response(out, c, budget, job, r)
        r['wrapper_wall_seconds'] = time.monotonic()-start
        budget.finish(token, response=r)
        return r
    except client.EvaluationError as e:
        budget.finish(token, error=e)
        return None
    except BaseException as e:
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            raise
        budget.finish(token, error=e)
        budget.halt('P3 identity/protocol/accounting/unknown fault: '+str(e))
        raise


def economic_response(result, legacy=False):
    value = v2.economic_response(result)
    trace = value.get('mechanism_trace')
    if trace is not None:
        trace.pop('path', None)
    if legacy:
        value['config'] = value['config']['strategy']
        value.pop('mechanism_contract', None)
        value.pop('mechanism_trace', None)
    return value


def incremental_report(reports, jobs, source):
    by = {r['name']: r for r in reports}
    checks, selected = [], []
    defaults = space.go_defaults(ROOT/'bin/lorentz-robust-v1')
    for skeleton in ('A', 'B'):
        qualified = []
        for variant in ('classic-original', 'classic-recent'):
            name = skeleton+'-'+variant
            candidate = by.get(name)
            comparisons = []
            for baseline in ('rq-direction', 'momentum-four'):
                control = by.get(skeleton+'-'+baseline)
                if candidate is None or control is None:
                    comparisons.append({'baseline': baseline, 'complete': False}); continue
                for scene in ('central', 'proxy-adverse'):
                    a = next(x for x in candidate['accounts'] if x['funding_scenario'] == scene and x['cost_multiplier'] == 1.5)
                    b = next(x for x in control['accounts'] if x['funding_scenario'] == scene and x['cost_multiplier'] == 1.5)
                    comparisons.append({'baseline': baseline, 'funding_scenario': scene, 'complete': True,
                        'net_return_pp': a['metrics']['net_return_pct']-b['metrics']['net_return_pct'],
                        'full_drawdown_pp': a['metrics']['max_drawdown_pct']-b['metrics']['max_drawdown_pct'],
                        'half_return_pp': [x['net_return_pct']-y['net_return_pct'] for x, y in zip(a['half_years'], b['half_years'])],
                        'total_return_higher': a['metrics']['net_return_pct'] > b['metrics']['net_return_pct'],
                        'full_dd_not_worse': a['metrics']['max_drawdown_pct'] <= b['metrics']['max_drawdown_pct'],
                        'halves_not_worse': sum(x['net_return_pct'] >= y['net_return_pct'] for x, y in zip(a['half_years'], b['half_years']))})
            eligible = (candidate is not None and candidate['v2_development_score']['feasible']
                        and len(comparisons) == 4 and all(x['complete'] and x['total_return_higher']
                            and x['full_dd_not_worse'] and x['halves_not_worse'] >= 4 for x in comparisons))
            checks.append({'name': name, 'eligible': eligible, 'comparisons': comparisons})
            if eligible:
                score = candidate['v2_development_score']
                config = next(j['config']['strategy'] for j in jobs if j['name'] == name)
                rank = (-score['values'][1], -score['values'][0], score['max_drawdown_pct'],
                        search.complexity(config, defaults), int(variant != 'classic-original'),
                        source['source_keys'][skeleton])
                qualified.append((rank, name))
        if qualified:
            selected.append({'skeleton': skeleton, 'name': min(qualified)[1]})
    return {'incremental_eligibility': checks, 'selected_research_objects': selected,
            'stop_lorentz_local_search': not selected, 'P4_authorized': False,
            'interpretation': 'development priority only; no future validation or cross-seed incremental proof',
            'selection_status': 'pending-independent-result-audit'}


def run(out, approval):
    out = Path(out).resolve(); c = authorize(out, approval)['contract']
    execution = out/'execution'; execution.mkdir(exist_ok=True)
    with (execution/'run.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = execution/'budget.sqlite'
        if not path.exists() and any(p.name != 'run.lock' for p in execution.iterdir()):
            raise ValueError('existing P3 execution lost durable budget')
        budget = Budget(path, {'manifest_contract_sha256': space.identity(c)}, LIMITS, {'P3': LIMITS})
        if budget.halted():
            raise BudgetHalted(budget.halted())
        budget.recover('P3')
        old_summary = execution/'summary.json'
        records = []
        try:
            if old_summary.exists():
                previous = v2.load(old_summary)
                if previous.get('status') == 'complete' and previous.get('independent_result_audit') == 'closed':
                    return {'status': 'already-complete', 'budget': budget.counts(), 'summary': str(old_summary)}
            source = v2.load(out/'lineage.json')
            with client.Evaluator(c['engine']['path']) as evaluator:
                for job in c['jobs']:
                    load_run(out)
                    r = request(out, c, budget, job, evaluator)
                    records.append({'name': job['name'], 'role': job['role'],
                                    'status': budget.record('P3/'+job['name'])['status'], 'result': r})
                    atomic_json(old_summary, {'status': 'in-progress', 'records': records, 'budget': budget.snapshot(False)})
        except BaseException as e:
            if not isinstance(e, (KeyboardInterrupt, SystemExit)):
                budget.halt('P3 execution identity/protocol/accounting fault: '+str(e))
            raise
        checks, reports = [], []
        by = {r['name']: r['result'] for r in records}
        try:
            for job in c['jobs']:
                r = by[job['name']]
                if 'replay_of' in job:
                    original = by[job['replay_of']]
                    matched = r is not None and original is not None and space.canonical(economic_response(r)) == space.canonical(economic_response(original))
                    if r is not None and original is not None and not matched:
                        raise client.ProtocolError('independent P3 economic/state replay differs')
                    checks.append({'name': job['name'], 'matched_full_economics_and_state_trace': matched})
                elif job['config']['variant'] == 'classic-original':
                    matched = r is not None and space.canonical(economic_response(r, legacy=True)) == space.canonical(v2.economic_response(source['originals'][job['skeleton']]))
                    if r is not None and not matched:
                        raise client.ProtocolError('new original mode differs from preserved P1 economic response/trace')
                    checks.append({'name': job['name'], 'matched_preserved_P1_economics_and_trace': matched})
            for record in records:
                if record['result'] is not None:
                    accounts = [summarize_continuous_account(a, bundle=c['bundle_dir']) for a in record['result']['accounts']]
                    reports.append({'name': record['name'], 'accounts': accounts, 'v2_development_score': continuous_score(accounts)})
            comparisons = incremental_report(reports, c['jobs'], source)
        except BaseException as e:
            budget.halt('P3 compatibility/report/accounting fault: '+str(e)); raise
        state = {'version': VERSION, 'status': 'complete' if all(r['status'] == 'complete' for r in records) else 'complete-with-failures',
                 'evidence': 'exposed retrospective development; no future validation', 'records': records, 'reports': reports,
                 'checks': checks, 'comparison': comparisons, 'budget': budget.snapshot(False),
                 'independent_result_audit': 'pending', 'later_stage_authorized': False}
        atomic_json(old_summary, state)
        return {'status': state['status'], 'budget': budget.counts(), 'summary': str(old_summary)}


def main():
    p = argparse.ArgumentParser(description=__doc__); sub = p.add_subparsers(dest='command', required=True)
    x = sub.add_parser('prepare'); x.add_argument('--out', type=Path, required=True)
    x.add_argument('--source-run', type=Path, default=ROOT/'results/mechanism-v2/ETHUSDT/1h/p1-prepared')
    x.add_argument('--binary', type=Path, default=ROOT/'bin/lorentz-mechanism-p3')
    x.add_argument('--build-manifest', type=Path, default=ROOT/'build/mechanism-p3/build-manifest.json')
    for command in ('inspect', 'run', 'report'):
        x = sub.add_parser(command); x.add_argument('--run', type=Path, required=True)
        if command == 'run': x.add_argument('--approval', type=Path, required=True)
    args = p.parse_args()
    if args.command == 'prepare': result = prepare(args.out, args.source_run, args.binary, args.build_manifest)
    elif args.command == 'run': result = run(args.run, args.approval)
    elif args.command == 'inspect': result = {'manifest_verified': bool(load_run(args.run)), 'historical_evaluations': 0,
                                              'budget_upper_bound': LIMITS, 'inspect': v2.load(args.run/'inspect.json')}
    else: load_run(args.run); result = v2.load(args.run/'execution/summary.json')
    print(space.canonical(result))


if __name__ == '__main__':
    main()

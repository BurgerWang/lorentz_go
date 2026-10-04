#!/usr/bin/env python3
"""P4 finite local search preparation; market execution needs separate approval."""
import argparse
from copy import deepcopy
import fcntl
from itertools import islice
from pathlib import Path
import statistics

from enhanced import strict_loads
import enhanced_space as space
import mechanism_p3 as p3
import mechanism_p3_client as client
import mechanism_p4_domain as domain
import mechanism_v2 as v2
from mechanism_metrics import continuous_score, summarize_continuous_account
from robust_budget import Budget, BudgetHalted, atomic_json
from robust_data import sha256
from robust_trace import verify_account

ROOT = v2.ROOT
VERSION = 'mechanism-p4-v1'
SEEDS = (17,42,73)
LIMITS = {'trial_attempts':768,'go_requests':920,'ledger_evaluations':1872}
HELPERS = p3.HELPERS + ('mechanism_p4.py','mechanism_p4_domain.py','mechanism_p4_search.py')
SOURCE = ROOT/'results/mechanism-v2/ETHUSDT/1h/p3-prepared'


def read_source(source_run):
    root = Path(source_run).resolve(); m = p3.load_run(root)
    s = v2.load(root/'execution/summary.json'); a = v2.load(root/'execution/result-audit.json')
    if (s.get('status') != 'complete' or s.get('independent_result_audit') != 'closed'
        or a.get('audit_status') != 'closed' or a.get('findings') != []
        or a.get('manifest_sha256') != sha256(root/'manifest.json')
        or space.identity({k:s[k] for k in a['economic_content_fields']}) != a.get('economic_content_identity')):
        raise ValueError('P4 needs the closed independent P3 result audit')
    expected = [{'name':k+'-classic-original','skeleton':k} for k in ('A','B')]
    if s['comparison']['selected_research_objects'] != expected or s['comparison']['stop_lorentz_local_search']:
        raise ValueError('this P4 contract only covers the two retained originals')
    anchors = {}
    for row in expected:
        k, name = row['skeleton'], row['name']
        report = next(x for x in s['reports'] if x['name']==name)
        result = next(x['result'] for x in s['records'] if x['name']==name and x['status']=='complete')
        if not report['v2_development_score']['feasible']:
            raise ValueError('P4 source has not passed all v2 development gates')
        pair = [deepcopy(x) for x in report['accounts'] if x['cost_multiplier']==1.5]
        score = domain.score_pair(pair)
        paths = []
        for account in result['accounts']:
            if account['cost_multiplier'] != 1.5:
                continue
            if sha256(account['trace_path']) != account['trace_sha256']:
                raise client.ProtocolError('preserved P3 reference trace changed')
            trades = []
            with Path(account['trace_path']).open() as stream:
                for line in stream:
                    event = strict_loads(line)
                    if event.get('phase') == 'exit':
                        trades.append({key:event['trade'][key] for key in ('entry_time','exit_time','side','entry_kind','reason')})
            paths.append({'scene':account['funding_scenario'],'cost':account['cost_multiplier'],'trades':trades})
        anchors[k] = {'config':deepcopy(result['config']), 'config_identity':space.identity(result['config']),
                      'score':score,'analysis':{'accounts':pair,'score':score,'trading_path_identity':space.identity(paths)},'response':deepcopy(result),
                      'anchor_preferred':True,
                      'source':{'kind':'prior-anchor','run':str(root),'name':name,
                                'economic_content_identity':a['economic_content_identity']}}
    paths = [root/'manifest.json',root/'execution/summary.json',root/'execution/result-audit.json',root/'lineage.json']
    return {'source_run':str(root),'source_files':{str(p):sha256(p) for p in paths},
            'source_contract_identity':m['contract_sha256'],'anchors':anchors,
            'evidence':'exposed historical development; prior rejection preserved',
            'prior_cache_policy':'reference anchors only; no cross-budget cache import'}


def plans(source):
    full = deepcopy(source['anchors']['A']['response']['plan'])
    if full != source['anchors']['B']['response']['plan'] or full != v2.account_plan(full['bundle_dir']):
        raise ValueError('P4 requires the unchanged P3 continuous full account plan')
    pair = deepcopy(full); pair['cost_multipliers'] = [1.5]
    return {'search':pair,'full':full}


def study_limits():
    out = {}
    for skeleton in ('A','B'):
        for seed in SEEDS:
            prefix = 'P4/'+skeleton+'/'+str(seed)
            for block in domain.BLOCKS:
                out[prefix+'/'+block] = {'trial_attempts':64,'go_requests':64,'ledger_evaluations':128}
            out[prefix+'/local'] = {'trial_attempts':0,'go_requests':24,'ledger_evaluations':48}
            out[prefix+'/pressure'] = {'trial_attempts':0,'go_requests':1,'ledger_evaluations':6}
    for label in ('first','last'):
        out['P4/replay-'+label] = {'trial_attempts':0,'go_requests':1,'ledger_evaluations':6}
    assert {k:sum(v[k] for v in out.values()) for k in LIMITS} == LIMITS
    return out


def blueprint():
    return {'branches':[{'skeleton':k,'seed':seed,'blocks':list(domain.BLOCKS),
                         'attempts_per_block':64,'anchor_first':True,'neighbors':24,
                         'neighbor_order':'adjacent single-key then double-key; fixed frozen key order',
                         'full_cost_check':True} for k in ('A','B') for seed in SEEDS],
            'replays':['first-central-in-stable-branch-order','last-central-in-stable-branch-order'],
            'single_central_replay':'same central twice in two separate processes',
            'no_central_replay':'skip both; never fill spare allowance',
            'startup_trials':16,'sampler':'per-trial independent TPE, (seed+number) mod 2**32, 24 EI',
            'constraints':'six native graded constraints; zero-only feasible',
            'selection':'worst growth, median growth, lower full DD, anchor at ties, stable config identity',
            'post_search_order':'freeze all central neighborhoods, all local requests, all full cost checks, two replays',
            'cache_and_failure':'never add attempts or neighbours to replace failures, repeats or cache savings'}


def prepare(out, source_run=SOURCE):
    out = Path(out).resolve()
    if out.exists():
        raise FileExistsError('P4 requires a new exclusive preparation directory')
    source = read_source(source_run); pp = plans(source)
    source_manifest = p3.load_run(source['source_run'])['contract']
    engine = deepcopy(source_manifest['engine'])
    rules = {k:domain.freeze(source['anchors'][k]['config'],k) for k in ('A','B')}
    out.mkdir(parents=True)
    atomic_json(out/'lineage.json',source); atomic_json(out/'domains.json',rules)
    atomic_json(out/'plans.json',pp); atomic_json(out/'pipeline.json',blueprint())
    counts = {}; catalog = out/'complete-configs.jsonl'
    with catalog.open('w') as stream:
        for k in ('A','B'):
            iterator = iter(domain.configurations(rules[k])); counts[k] = 0
            while batch := list(islice(iterator,128)):
                client.go_validate(engine['path'],batch)
                for cfg in batch:
                    stream.write(space.canonical({'skeleton':k,'config_identity':space.identity(cfg),'config':cfg})+'\n')
                counts[k] += len(batch)
    if counts != {'A':6561,'B':6561}:
        raise ValueError('P4 complete finite configuration catalog differs')
    inspections = []
    for k in ('A','B'):
        for label, cfg in domain.inspect_configs(rules[k]):
            atomic_json(out/'inspect-configs'/(k+'-'+label+'.json'),cfg)
            for name, plan in pp.items():
                ready = client.inspect(engine['path'],plan,cfg)
                inspections.append({'skeleton':k,'name':label,'plan':name,
                                    'config_identity':space.identity(cfg),'ready':ready,'evaluation_calls':0})
    atomic_json(out/'inspect.json',inspections)
    prepared = {str(p):sha256(p) for p in out.rglob('*') if p.is_file()}
    contract = {'version':VERSION,'symbol':'ETHUSDT','interval':'1h','data_use':'exposed-retrospective',
                'engine':engine,'build_manifest':deepcopy(source_manifest['build_manifest']),
                'runtime':v2.runtime_identity(),'bundle_dir':pp['full']['bundle_dir'],
                'bundle_identity':client.base.bundle_identity(pp['full']['bundle_dir']),
                'plan':pp['full'],'plans':pp,'domains':rules,'anchors':source['anchors'],
                'seeds':list(SEEDS),'startup_trials':16,'pipeline':blueprint(),
                'budget':deepcopy(LIMITS),'substudy_limits':study_limits(),
                'resource_limits':deepcopy(v2.RESOURCE),'inspect_completed':True,'evaluation_authorized':False,
                'helpers':{str(ROOT/'tuning'/n):sha256(ROOT/'tuning'/n) for n in HELPERS},
                'plan_documents':{str(ROOT/n):sha256(ROOT/n) for n in ('MECHANISM_V2_PLAN.md','MECHANISM_P3_CONTRACT.md','MECHANISM_P4_CONTRACT.md')},
                'prepared_files':prepared,'source_files':source['source_files'],
                'complete_catalog_counts':counts,'inspect_process_calls':len(inspections)}
    atomic_json(out/'manifest.json',{'version':VERSION,'contract_sha256':space.identity(contract),'contract':contract})
    atomic_json(out/'approval-template.json',{'version':'mechanism-p4-approval-v1','manifest_sha256':sha256(out/'manifest.json'),
                'approved':False,'authorized_by':None,'authorization_reference':None,'batch':'P4',
                'budget':deepcopy(LIMITS),'funding_modes':['proxy-stress-v1']})
    atomic_json(out/'preparation.json',{'status':'prepared-inspected','engineering_audit':'pending',
                'complete_configs':sum(counts.values()),'actual_inspect_calls':len(inspections),
                'inspect_coverage':'anchors, each individual domain edge, combined lower/upper extremes, both account plans',
                'full_catalog_go_validation':True,'real_historical_evaluations':0,'optuna_market_trials':0,
                'budget_consumed':dict.fromkeys(LIMITS,0)})
    load_run(out)
    return {'out':str(out),'status':'prepared-inspected','complete_configs':sum(counts.values()),
            'actual_inspect_calls':len(inspections),'historical_evaluations':0,'budget_upper_bound':LIMITS}


def load_run(out):
    out = Path(out).resolve(); m = v2.load(out/'manifest.json'); c = m['contract']
    if m.get('version') != VERSION or m.get('contract_sha256') != space.identity(c):
        raise ValueError('P4 manifest contract differs')
    if c['runtime'] != v2.runtime_identity() or sha256(c['engine']['path']) != c['engine']['sha256']:
        raise ValueError('P4 frozen runtime/engine changed')
    for path, digest in {**c['helpers'],**c['prepared_files'],**c['source_files'],**c['plan_documents']}.items():
        if sha256(path) != digest:
            raise ValueError('P4 frozen dependency changed: '+path)
    source = v2.load(out/'lineage.json'); p3.load_run(source['source_run'])
    if c['anchors'] != source['anchors'] or c['plans'] != plans(source) or c['plan'] != c['plans']['full']:
        raise ValueError('P4 source anchor or continuous plan differs')
    if c['domains'] != {k:domain.freeze(source['anchors'][k]['config'],k) for k in ('A','B')}:
        raise ValueError('P4 domain widened or structural context changed')
    if (c['budget'] != LIMITS or c['substudy_limits'] != study_limits() or c['seeds'] != list(SEEDS)
        or c['pipeline'] != blueprint() or c['startup_trials'] != 16 or not c['inspect_completed']
        or c['evaluation_authorized'] is not False or c['resource_limits'] != v2.RESOURCE):
        raise ValueError('P4 frozen scope differs')
    if c['bundle_identity'] != client.base.bundle_identity(c['bundle_dir']):
        raise ValueError('P4 frozen data changed')
    return m


def authorize(out, approval):
    out = Path(out).resolve(); m = load_run(out); a = v2.load(approval)
    if (a.get('version') != 'mechanism-p4-approval-v1' or a.get('manifest_sha256') != sha256(out/'manifest.json')
        or a.get('approved') is not True or a.get('authorized_by') != 'user' or a.get('batch') != 'P4'
        or not isinstance(a.get('authorization_reference'),str) or not a['authorization_reference'].strip()
        or a.get('budget') != LIMITS or a.get('funding_modes') != ['proxy-stress-v1']):
        raise ValueError('P4 market search requires separate user approval binding 768 attempts / 920 Go / 1872 ledgers')
    audit = v2.load(out/'engineering-audit.json')
    if (audit.get('audit_status') != 'closed' or audit.get('findings') != []
        or audit.get('manifest_sha256') != sha256(out/'manifest.json')):
        raise ValueError('P4 independent engineering audit is not closed for this manifest')
    return m


def analyze(c, result, plan=None, config=None):
    plan = plan or result['plan']; config = config or result['config']
    ready = {'plan':client.base.normalize_plan(plan),'bundle_identity':c['bundle_identity'],
             'ledger_evaluations':len(plan['cost_multipliers'])*2}
    client.validate_result(result,plan,config,ready); client.verify_state_trace(result)
    reports = []; signatures = []
    for account in result['accounts']:
        checked = verify_account(account,c['bundle_dir'])
        reports.append(summarize_continuous_account(account,bundle=c['bundle_dir'],checked=checked))
        signatures.append({'scene':account['funding_scenario'],'cost':account['cost_multiplier'],
                           'trades':[{k:t[k] for k in ('entry_time','exit_time','side','entry_kind','reason')} for t in checked['trades']]})
    if sum(Path(a['trace_path']).stat().st_size for a in result['accounts']) + Path(result['mechanism_trace']['path']).stat().st_size > c['resource_limits']['max_trace_bytes_per_request']:
        raise client.ProtocolError('P4 response exceeds frozen trace-size limit')
    score = domain.score_pair(reports) if plan['cost_multipliers']==[1.5] else continuous_score(reports)
    return {'accounts':reports,'score':score,'trading_path_identity':space.identity(signatures)}


def fixed_request(out,c,budget,study,token,plan,config,evaluator,replay=False,replay_original=None):
    if budget.halted():
        raise BudgetHalted(budget.halted())
    current = None
    try:
        load_run(out)
        key = space.identity({'binding_identity':space.identity(c),'plan':plan,'config':config})
        if replay:
            if replay_original is None:
                raise client.ProtocolError('P4 replay requires its successfully verified original response')
            key = space.identity({'key':key,'independent_replay':token})
        current = budget.record(token)
        if current:
            if current['status'] in ('failed','interrupted'):
                return {'name':token,'status':current['status'],'config_identity':space.identity(config),'analysis':None,'result':None}
            if current['status'] != 'complete':
                raise client.ProtocolError('P4 fixed request needs recovery; never resend unknown')
            if current.get('cache_key') != key:
                raise client.ProtocolError('P4 persisted fixed cache binding differs')
            response = budget.cached_response(current['cache_key'])
        else:
            # Failed/unknown identical requests cannot be silently repeated as a new token.
            if not replay and any(r.get('cache_key')==key and r['status'] in ('failed','interrupted','reserved')
                    for name in c['substudy_limits'] for r in budget.records_for(name).values()):
                # Fixed studies have no attempt allowance: no new request/charge is needed.
                return {'name':token,'status':'skipped-previous-failure','config_identity':space.identity(config),'analysis':None,'result':None}
            reservation = budget.reserve_evaluation(study,token,key,len(plan['cost_multipliers'])*2,fixed=True)
            current = budget.record(token)
            if reservation['send']:
                v2.resources_ready(out,dict(c,plan=plan)); trace = Path(out)/'execution/traces'/token.replace('/','_')
                response = client.evaluate(c['engine']['path'],plan,config,trace) if replay else evaluator(plan,config,trace)
            else:
                response = reservation['response']
        if response is None:
            raise client.ProtocolError('P4 completed/cached request lost response')
        report = analyze(c,response,plan,config)
        if replay and p3.economic_response(response) != p3.economic_response(replay_original):
            raise client.ProtocolError('P4 independent full economics/state replay differs')
        if current['status']=='reserved':
            budget.finish(token,response=response)
        return {'name':token,'status':'complete','config_identity':space.identity(config),'analysis':report,'result':response}
    except client.EvaluationError as e:
        current = budget.record(token)
        if current and current['status'] in ('attempt','reserved'):
            budget.finish(token,error=e)
        return {'name':token,'status':'failed','config_identity':space.identity(config),'analysis':None,'result':None,'error':str(e)}
    except BaseException as e:
        if isinstance(e,(KeyboardInterrupt,SystemExit)):
            raise
        current = budget.record(token)
        if current and current['status'] in ('attempt','reserved'):
            budget.finish(token,error=e)
        budget.halt('P4 fixed identity/protocol/accounting fault: '+str(e))
        raise


def run(out,approval,until_attempts=None):
    from mechanism_p4_search import run_search
    out = Path(out).resolve(); m = authorize(out,approval); c = m['contract']
    execution = out/'execution'; execution.mkdir(exist_ok=True)
    with (execution/'run.lock').open('a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        path = execution/'budget.sqlite'
        if not path.exists() and any(p.name!='run.lock' for p in execution.iterdir()):
            raise ValueError('P4 execution lost durable budget; cannot reset consumed usage')
        budget = Budget(path,{'manifest_contract_sha256':space.identity(c)},LIMITS,c['substudy_limits'])
        if budget.halted():
            raise BudgetHalted(budget.halted())
        def checkpoint():
            if budget.halted(): raise BudgetHalted(budget.halted())
            try: load_run(out)
            except BaseException as e:
                if not isinstance(e,(KeyboardInterrupt,SystemExit)): budget.halt('P4 frozen identity fault: '+str(e))
                raise
        try:
            old = execution/'summary.json'
            if old.exists():
                previous = v2.load(old)
                if previous.get('status') in ('complete','complete-with-failures') and previous.get('independent_result_audit')=='closed':
                    return {'status':'already-complete','budget':budget.counts(),'summary':str(old)}
            with client.Evaluator(c['engine']['path']) as evaluator:
                def evaluate(plan,config,trace):
                    checkpoint(); v2.resources_ready(out,dict(c,plan=plan))
                    return evaluator(plan,config,trace)
                searched = run_search(execution,{'binding_identity':space.identity(c),'plan':c['plans']['search'],
                    'domains':c['domains'],'seeds':c['seeds'],'anchors':c['anchors'],'startup_trials':16},
                    budget,evaluate,lambda r:analyze(c,r,c['plans']['search']),checkpoint=checkpoint,until_attempts=until_attempts)
                if searched['status'] != 'complete':
                    state = {'version':VERSION,'status':'in-progress','search':searched,'budget':budget.snapshot(False),
                             'independent_result_audit':'pending','later_stage_authorized':False}
                    atomic_json(execution/'summary.json',state)
                    return {'status':state['status'],'budget':budget.counts(),'summary':str(execution/'summary.json')}
                centers = []
                for b in searched['branches']:
                    center = b.get('selected')
                    if center is not None:
                        rules = c['domains'][b['skeleton']]; domain.validate_config(center['config'],rules)
                        frozen = domain.neighborhood(center['config'],rules)
                        key = b['skeleton']+'-'+str(b['seed'])
                        pp = execution/'neighborhoods'/(key+'.json')
                        if pp.exists() and v2.load(pp) != frozen:
                            raise client.ProtocolError('P4 frozen neighborhood changed on resume')
                        atomic_json(pp,frozen)
                        centers.append({'skeleton':b['skeleton'],'seed':b['seed'],'central':center,'frozen':frozen})
                # Every list is persisted before any neighbor is evaluated.
                atomic_json(execution/'centrals.json',centers)
                for row in centers:
                    prefix = 'P4/'+row['skeleton']+'/'+str(row['seed']); study = prefix+'/local'
                    budget.recover(study); outcomes = []
                    if row['frozen']['ready']:
                        for point in row['frozen']['points']:
                            result = fixed_request(out,c,budget,study,study+'/'+point['name'],c['plans']['search'],point['config'],evaluator)
                            result['request_token'] = result['name']; result['name'] = point['name']
                            analysis = result['analysis']
                            result['trading_path_changed'] = bool(analysis and analysis['trading_path_identity'] != row['central']['analysis'].get('trading_path_identity'))
                            outcomes.append(result)
                        row['neighborhood_check'] = domain.neighborhood_check(row['central'],row['frozen'],outcomes)
                    else:
                        row['neighborhood_check'] = {'passed':False,'reason':'fewer than 24 legal points; not ready'}
                    row['neighborhood_outcomes'] = outcomes
                    atomic_json(execution/'post-search.json',centers)
                for row in centers:
                    study = 'P4/'+row['skeleton']+'/'+str(row['seed'])+'/pressure'; budget.recover(study)
                    row['pressure'] = fixed_request(out,c,budget,study,study+'/central',c['plans']['full'],row['central']['config'],evaluator)
                    row['complete_development_eligible'] = bool(row['neighborhood_check']['passed'] and row['pressure']['analysis'] and row['pressure']['analysis']['score']['feasible'])
                    atomic_json(execution/'post-search.json',centers)
                checks = []
                replay_centers = [centers[0],centers[-1]] if centers else []
                for label,row in zip(('first','last'),replay_centers):
                    study = 'P4/replay-'+label; budget.recover(study)
                    if row['pressure']['result'] is None:
                        checks.append({'name':study,'central_config_identity':space.identity(row['central']['config']),
                                       'matched_full_economics_and_state_trace':False,'result':None,
                                       'reason':'original pressure request failed/unknown; replay skipped without retry'})
                        continue
                    replay = fixed_request(out,c,budget,study,study+'/central',c['plans']['full'],row['central']['config'],evaluator,replay=True,replay_original=row['pressure']['result'])
                    original = row['pressure']['result']
                    matched = replay['result'] is not None and original is not None and p3.economic_response(replay['result']) == p3.economic_response(original)
                    if replay['result'] is not None and original is not None and not matched:
                        raise client.ProtocolError('P4 independent full economics/state replay differs')
                    checks.append({'name':study,'central_config_identity':space.identity(row['central']['config']),
                                   'matched_full_economics_and_state_trace':matched,'result':replay})
                replay_ok = all(x['matched_full_economics_and_state_trace'] for x in checks)
                selected = []
                for skeleton in ('A','B'):
                    eligible = [x for x in centers if x['skeleton']==skeleton and x['complete_development_eligible'] and replay_ok]
                    if eligible:
                        best = min(eligible,key=lambda x:domain.rank_key(x['central'])+(x['seed'],))
                        selected.append({'skeleton':skeleton,'seed':best['seed'],'config':best['central']['config'],
                                         'config_identity':space.identity(best['central']['config'])})
                state = {'version':VERSION,'status':'complete' if replay_ok else 'complete-with-failures','evidence':'exposed historical development; no future validation',
                         'search':searched,'centrals':centers,'checks':checks,'selected_research_objects':selected,
                         'budget':budget.snapshot(False),'independent_result_audit':'pending','later_stage_authorized':False,
                         'producer_selection_status':'pending independent result audit'}
                atomic_json(execution/'summary.json',state)
                return {'status':state['status'],'budget':budget.counts(),'summary':str(execution/'summary.json')}
        except BaseException as e:
            if not isinstance(e,(KeyboardInterrupt,SystemExit)):
                budget.halt('P4 execution identity/protocol/accounting fault: '+str(e))
            raise


def main():
    p = argparse.ArgumentParser(description=__doc__); sub = p.add_subparsers(dest='command',required=True)
    x = sub.add_parser('prepare'); x.add_argument('--out',type=Path,required=True); x.add_argument('--source-run',type=Path,default=SOURCE)
    for name in ('inspect','run','report'):
        x = sub.add_parser(name); x.add_argument('--run',type=Path,required=True)
        if name=='run':
            x.add_argument('--approval',type=Path,required=True); x.add_argument('--until-attempts',type=int)
    args = p.parse_args()
    if args.command=='prepare': result = prepare(args.out,args.source_run)
    elif args.command=='run': result = run(args.run,args.approval,args.until_attempts)
    elif args.command=='inspect':
        m = load_run(args.run); result = {'manifest_verified':True,'budget_upper_bound':LIMITS,
            'complete_configs':m['contract']['complete_catalog_counts'],'actual_inspect_calls':m['contract']['inspect_process_calls'],
            'historical_evaluations':0,'optuna_market_trials':0,'inspect':v2.load(args.run/'inspect.json')}
    else: load_run(args.run); result = v2.load(args.run/'execution/summary.json')
    print(space.canonical(result))


if __name__=='__main__':
    main()

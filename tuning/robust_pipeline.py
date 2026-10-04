#!/usr/bin/env python3
"""Compile immutable robust-v1 batches, explicitly authorize, execute and report."""
from copy import deepcopy
from datetime import datetime,timezone,timedelta
import argparse
from contextvars import ContextVar
from functools import wraps
import fcntl
import hashlib
from importlib.metadata import version as package_version
import math
from pathlib import Path
import shutil
import sys
import tempfile
import time
import enhanced_protocol
from enhanced import strict_loads
import enhanced_space as space
from feature_groups import classifier_config
import robust_client as client
from robust_budget import Budget,atomic_json,cache_identity
from robust_data import validate_bundle,sha256,candidate_migration_record,verify_successor,utc_ms,DAY,iso
import robust_search as search
import robust_report as report
from robust_trace import verify_account,scheduled_flat,near

ROOT=Path(__file__).resolve().parents[1]
COUNTERS=('trial_attempts','go_requests','ledger_evaluations')
VERSION='robust-run-v1'
ACTIVE_EVALUATOR=ContextVar('robust_evaluator',default=None)
HELPER_FILES=('robust_pipeline.py','robust_client.py','robust_data.py','robust_trace.py',
    'robust_search.py','robust_budget.py','robust_report.py','enhanced_space.py',
    'feature_groups.py','enhanced.py','enhanced_protocol.py','optimize.py','optimize_enhanced.py',
    'requirements.lock')

def helper_identity():
    return {str(rooted('tuning/'+name)):sha256(rooted('tuning/'+name)) for name in HELPER_FILES}

def runtime_identity():
    return {'python':sys.version,'optuna':package_version('optuna'),'numpy':package_version('numpy')}

def managed_sessions(fn):
    @wraps(fn)
    def wrapped(run,*args,**kwargs):
        c=load_run(run)['contract']
        with client.Evaluator(c['engine']['path']) as evaluator:
            token=ACTIVE_EVALUATOR.set(evaluator)
            try:return fn(run,*args,**kwargs)
            finally:ACTIVE_EVALUATOR.reset(token)
    return wrapped

def resources_ready(run,c,pp):
    resource=c.get('protocol',{}).get('resource_limits',{'max_trace_bytes_per_request':4294967296,'minimum_free_bytes_before_request':8589934592})
    from robust_data import INTERVALS
    bars=sum((utc_ms(w['end'])-utc_ms(w['start']))//INTERVALS[pp['interval']] for w in pp['windows'])
    estimate=bars*6000*len(pp['funding_scenarios'])*len(pp['cost_multipliers'])
    if estimate>resource['max_trace_bytes_per_request']:raise ValueError('declared per-request trace storage limit exceeded')
    path=Path(run);path.mkdir(parents=True,exist_ok=True)
    if shutil.disk_usage(path).free < max(resource['minimum_free_bytes_before_request'],estimate*2):raise ValueError('declared free storage guard failed')
    return estimate


def load(path): return strict_loads(Path(path).read_text())
def limits(values): return dict(zip(COUNTERS,values))
def write_new(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('x') as stream: stream.write(space.canonical(value)+'\n')
def rooted(path): return (ROOT/path).resolve() if not Path(path).is_absolute() else Path(path).resolve()


def authorization_scopes(protocol):
    scopes={k:{'budget':limits(v),'funding_modes':['exact'] if k in ('R0','W-outer','Q2026') else (['exact','proxy-stress-v1'] if k=='B1' else ['proxy-stress-v1'])} for k,v in protocol['budgets'].items()}
    for name in ('W1','W2','W3','W4'):
        scopes[name]={'budget':limits([4608,4765,57087]),'funding_modes':['exact','proxy-stress-v1'],'origin':name}
    scopes['F1']={'budget':limits([4608,4764,57084]),'funding_modes':['exact','proxy-stress-v1'],'origin':'F1'}
    return scopes


def load_protocol(path):
    p=load(path)
    if (p.get('version')!='robust-v1' or p.get('intervals')!=['15m','1h','4h','1d'] or p.get('profile')!='native-bars-v1'
        or p.get('seeds')!=list(search.SEEDS) or p.get('bootstrap')!={'replicates':5000,'block_lengths':[7,14,28],'seed_base':20261002,'quantile':'Type7-linear'}
        or p.get('funding_mode')!='proxy-stress-v1' or p.get('funding_scenarios')!=['central','proxy-adverse']
        or p.get('cost_multipliers')!=[1,1.5,2] or p.get('data_use')!='retrospective' or p.get('evaluation_authorized') is not False):
        raise ValueError('frozen robust-v1 protocol differs')
    expected={'R0':[0,17,51],'B1':[0,26,294],'W-search':[18432,18432,221184],'W-local':[0,608,7104],
              'W-outer':[0,20,60],'F-search':[4608,4608,55296],'F-local':[0,152,1776],'Q2026':[0,4,12]}
    if p.get('budgets')!=expected: raise ValueError('frozen batch budgets differ')
    if p.get('resource_limits')!={'max_trace_bytes_per_request':4294967296,'minimum_free_bytes_before_request':8589934592,'max_statistics_days':5000,'bootstrap_replicates':5000,'max_statistics_paths':32}:raise ValueError('frozen resource limits differ')
    return p


def plan(bundle,interval,windows,history='2020-01-01T00:00:00Z',training=False,proxy=False):
    return {'version':'robust-eval-v1','bundle_dir':str(Path(bundle).resolve()),'interval':interval,'history_start':history,
        'windows':deepcopy(windows),'account_mode':'fold-reset-v1' if training else 'fixed-continuous-v1',
        'funding_mode':'proxy-stress-v1' if proxy else 'exact','funding_scenarios':['central','proxy-adverse'] if proxy else ['central'],
        'cost_multipliers':[1.5] if training else [1,1.5,2]}


def public_controls(protocol,daily_start):
    out={}
    for name,c in protocol['public_controls'].items():
        c=deepcopy(c);c['daily']['history_start']=daily_start[:10];out[name]=c
    return out


def representatives(protocol,daily_start):
    output=[]
    for family,base_name in zip(search.FAMILIES,('classic','aligned')):
        for i,group in enumerate(search.GROUPS):
            c=deepcopy(protocol['public_controls'][base_name]);c['daily']['history_start']=daily_start[:10]
            if i==0 and base_name=='classic': c['classic']['include_full_history']=True
            if i==1: c['classic']['use_dynamic_exits']=True
            if i==2: c['daily']['enabled']=True;c['risk']['enabled']=True;c['risk']['trail']=True
            if i==3: c['entry_mode']='both';c['pullback']['enabled']=True;c['envelope_enabled']=True
            if i==4: c['daily']['enabled']=True;c['risk']['enabled']=True;c['risk']['breakeven_r']=1
            if i==5:
                c['exit']={'policy':'four-bars','max_hold_bars':24};c['risk']['enabled']=True
                if base_name=='aligned': c['model']['rank_half_life']=100
            c['classifier']=classifier_config(c['classic'],group,family=family)
            output.append({'name':family+'-'+group,'config':space.normalize_config(c,protocol['public_controls']['classic'])})
    return output


def r0_entries(protocol):
    comparison=load(rooted(protocol['legacy_comparison']));entries=[]
    for family,trials in protocol['pareto_trials'].items():
        for number in trials:
            path=rooted(protocol['legacy_studies'][family])/f'candidate-trial-{number}.json'
            entries.append({'name':('Classic' if family==search.FAMILIES[0] else 'aligned')+str(number),
                'config':load(path),'source':{'path':str(path),'sha256':sha256(path),'cutoff':'2025-01-01T00:00:00Z','role':'S1-Pareto'}})
    names=['default-classic','1h-trial52-d1-false','1h-trial54-d1-false','1h-trial10-d1-false','aligned-control','euclidean']
    for name in names:
        control=next(x for x in comparison['controls'] if x['name']==name)
        entries.append({'name':name,'config':control['config'],'source':{'path':str(rooted(protocol['legacy_comparison'])),
            'sha256':sha256(rooted(protocol['legacy_comparison'])),'cutoff':'2025-01-01T00:00:00Z','role':'S1-fixed-control'}})
    return entries,comparison['development_contract']['plan']


def prepare_run(protocol_path,interval,dataset,out,inspect=True,binary=None):
    """No strategy evaluation; compile concrete R0/B1 and predeclared future stages."""
    protocol_path=Path(protocol_path).resolve();p=load_protocol(protocol_path);bundle=Path(dataset).resolve();b=validate_bundle(bundle)
    if interval not in p['intervals'] or b['interval']!=interval:raise ValueError('interval/bundle differs')
    if b['funding_mode']!='proxy-stress-v1':raise ValueError('multi-year preparation requires explicit proxy-stress bundle')
    engine=Path(binary).resolve() if binary else rooted(p['engine']);legacy_engine=rooted(p['legacy_engine'])
    defaults=space.go_defaults(engine);controls=public_controls(p,b['daily_history_start'])
    jobs=[];migrations=[];sources={str(protocol_path):sha256(protocol_path)}
    fixed2025=[{'name':'2025','start':'2025-01-01T00:00:00Z','end':'2026-01-01T00:00:00Z'}]
    if interval=='1h':
        entries,oldplan=r0_entries(p)
        for e in entries:
            sources[e['source']['path']]=e['source']['sha256']
            migration=candidate_migration_record(rooted(p['legacy_source']),bundle,'2023-01-01T00:00:00Z',e['config']['daily']['history_start']+'T00:00:00Z',
                '2025-01-01T00:00:00Z',legacy_engine,engine,space.identity(e['config']),interval=interval,funding_start='2024-01-01T00:00:00Z')
            migrations.append({'name':e['name'],'migration':migration})
            jobs.append({'batch':'R0','name':'fixed-'+e['name'],'engine':'robust','plan':plan(bundle,interval,fixed2025,history=oldplan['history_start']),
                         'config':e['config'],'role':'fixed-2025','source':e['source']})
        for name in ('Classic101','aligned213'):
            e=next(e for e in entries if e['name']==name)
            jobs.append({'batch':'R0','name':'replay-'+name,'engine':'robust','plan':plan(bundle,interval,fixed2025,history=oldplan['history_start']),
                         'config':e['config'],'role':'independent-replay','replay_of':'fixed-'+name,'source':e['source']})
            for engine_type in ('legacy','robust'):
                pp=deepcopy(oldplan) if engine_type=='legacy' else plan(bundle,interval,oldplan['folds'],history=oldplan['history_start'],training=True)
                if engine_type=='robust':pp['cost_multipliers']=[1]
                jobs.append({'batch':'R0','name':'compat-'+engine_type+'-'+name,'engine':engine_type,'plan':pp,'config':e['config'],
                             'role':'engine-comparison','comparison':name,'source':e['source']})
    training=plan(bundle,interval,search.windows('2024-01-01T00:00:00Z'),training=True,proxy=True)
    for e in representatives(p,b['daily_history_start']):
        for replay in (False,True):
            jobs.append({'batch':'B1','name':('replay-' if replay else 'initial-')+e['name'],'engine':'robust',
                'plan':training,'config':e['config'],'role':'independent-replay' if replay else 'representative',
                **({'replay_of':'initial-'+e['name']} if replay else {})})
    for name,cfg in controls.items():
        jobs.append({'batch':'B1','name':'public-'+name,'engine':'robust','plan':plan(bundle,interval,fixed2025),
                     'config':cfg,'role':'public-exact-control'})
    for j in jobs: j['ledgers']=len(j['plan']['folds']) if j['engine']=='legacy' else len(j['plan']['windows'])*len(j['plan']['funding_scenarios'])*len(j['plan']['cost_multipliers'])
    for batch in ('R0','B1'):
        selected=[j for j in jobs if j['batch']==batch]
        if selected and [0,len(selected),sum(j['ledgers'] for j in selected)]!=p['budgets'][batch]:raise ValueError('compiled fixed batch budget differs')
    out=Path(out).resolve()
    if out.exists():raise FileExistsError('immutable run directory already exists')
    out.parent.mkdir(parents=True,exist_ok=True);stage=Path(tempfile.mkdtemp(prefix='.robust-prepare-',dir=out.parent))
    try:
        inspections=[]
        for j in jobs:
            if inspect:
                if j['engine']=='robust':
                    ready=client.inspect(engine,j['plan'],j['config'])
                    if ready['ledger_evaluations']!=j['ledgers']:raise ValueError('Go/compiler ledger budgets differ')
                else:
                    # Legacy loader preserves original ready and dependencies; no strategy request.
                    fp=stage/'legacy-inspect-plan.json';fp.write_text(space.canonical(j['plan']))
                    s=enhanced_protocol.Server(legacy_engine,fp)
                    try:ready=deepcopy(s.ready)
                    finally:s.close()
                    fp.unlink()
                inspections.append({'name':j['name'],'batch':j['batch'],'ready':ready,'evaluation_calls':0})
        schedule=[]
        for origin in search.origin_schedule():
            train=plan(bundle,interval,origin['training_windows'],training=True,proxy=True)
            continuous=plan(bundle,interval,[{'name':origin['origin']+'-development','start':origin['training_windows'][0]['start'],'end':origin['cutoff']}],proxy=True)
            outer=plan(bundle,interval,[origin['outer_window']])
            if inspect:
                for kind,pp in (('train',train),('continuous',continuous),('outer',outer)):
                    r=client.inspect(engine,pp,controls['classic']);inspections.append({'name':origin['origin']+'-'+kind,'ready':r,'evaluation_calls':0})
            schedule.append({**origin,'training_plan':train,'continuous_plan':continuous,'outer_plan':outer})
        contract={'version':VERSION,'protocol':p,'protocol_path':str(protocol_path),'interval':interval,
            'bundle_dir':str(bundle),'bundle_identity':client.bundle_identity(bundle),'profile':b['profile'],
            'engine':{'path':str(engine),'sha256':sha256(engine)},'legacy_engine':{'path':str(legacy_engine),'sha256':sha256(legacy_engine)},
            'sources':sources,'runtime':sys.version,'runtime_versions':runtime_identity(),'helpers':helper_identity(),
            'defaults':defaults,'public_controls':controls,'jobs':jobs,'schedule':schedule,'migrations':migrations,
            'budgets':{key:limits(v) for key,v in p['budgets'].items()},'authorization_scopes':authorization_scopes(p),'data_use':'engineering' if b['synthetic'] else 'retrospective',
            'evaluation_authorized':False,'inspect_completed':inspect}
        artifacts={}
        for job in jobs:
            for kind,value in (('plan',job['plan']),('config',job['config']),('provenance',{k:v for k,v in job.items() if k not in ('plan','config')})):
                rel=Path('prepared')/job['batch']/(job['name']+'.'+kind+'.json');write_new(stage/rel,value);artifacts[str(out/rel)]=sha256(stage/rel)
        for origin in schedule:
            rel=Path('prepared')/'schedule'/(origin['origin']+'.json');write_new(stage/rel,origin);artifacts[str(out/rel)]=sha256(stage/rel)
        for migration in migrations:
            rel=Path('prepared')/'migrations'/(migration['name']+'.json');write_new(stage/rel,migration);artifacts[str(out/rel)]=sha256(stage/rel)
        contract['prepared_files']=artifacts
        manifest={'version':VERSION,'contract_sha256':space.identity(contract),'contract':contract}
        write_new(stage/'manifest.json',manifest);write_new(stage/'inspect.json',inspections)
        write_new(stage/'approval-template.json',{'version':'robust-approval-v1','manifest_sha256':sha256(stage/'manifest.json'),
            'authorized_by':None,'authorization_reference':None,'approved':False,'batches':[],
            'budgets':{k:v['budget'] for k,v in contract['authorization_scopes'].items()},'funding_modes':{k:v['funding_modes'] for k,v in contract['authorization_scopes'].items()}})
        write_new(stage/'preparation.json',{'status':'prepared-inspected' if inspect else 'prepared-uninspected','evaluation_calls':0,
            'R0_jobs':sum(j['batch']=='R0' for j in jobs),'B1_jobs':26,'market_execution_authorized':False})
        out.mkdir()
        for file in stage.iterdir():file.rename(out/file.name)
        return manifest
    finally:shutil.rmtree(stage,ignore_errors=True)


def load_run(directory):
    directory=Path(directory).resolve();m=load(directory/'manifest.json');c=m['contract']
    if m.get('version')!=VERSION or m.get('contract_sha256')!=space.identity(c):raise ValueError('manifest contract differs')
    if sys.version!=c['runtime']:raise ValueError('frozen Python runtime differs')
    if c.get('runtime_versions')!=runtime_identity():raise ValueError('frozen package/runtime versions differ')
    for entry in (c['engine'],c['legacy_engine']):
        if sha256(entry['path'])!=entry['sha256']:raise ValueError('frozen engine changed')
    for mapping in (c['sources'],c['helpers'],c.get('prepared_files',{})):
        for path,digest in mapping.items():
            if sha256(path)!=digest:raise ValueError('frozen source/helper changed: '+path)
    if client.bundle_identity(c['bundle_dir'])!=c['bundle_identity']:raise ValueError('frozen bundle changed')
    if not c['inspect_completed']:raise ValueError('run was not inspected')
    return m


def authorize(directory,batch,approval):
    m=load_run(directory);a=load(approval);c=m['contract']
    if (a.get('version')!='robust-approval-v1' or a.get('manifest_sha256')!=sha256(Path(directory)/'manifest.json')
        or a.get('approved') is not True or a.get('authorized_by')!='user' or not isinstance(a.get('authorization_reference'),str)
        or not a['authorization_reference'].strip() or batch not in a.get('batches',[]) or batch not in c.get('authorization_scopes',{})
        or a.get('budgets',{}).get(batch)!=c['authorization_scopes'][batch]['budget']
        or a.get('funding_modes',{}).get(batch)!=c['authorization_scopes'][batch]['funding_modes']):raise ValueError('explicit user approval must bind the complete manifest, batch, budgets and funding modes')
    return m


def fixed_request(run,c,budget,batch,name,pp,cfg,engine='robust',independent=False):
    """Exactly one persistent reservation before send, ordinary failures retained."""
    token=batch+'/'+name;record=budget.record(token)
    if record and record['status'] in ('complete','failed','interrupted'):
        if record['status']=='complete':return budget.cached_response(record['cache_key'])
        return None
    count=len(pp['folds']) if engine=='legacy' else len(pp['windows'])*len(pp['funding_scenarios'])*len(pp['cost_multipliers'])
    key=cache_identity(pp,cfg)
    if independent:key=space.identity({'ordinary_key':key,'independent_replay':token})
    reservation=budget.reserve_evaluation(batch,token,key,count,fixed=True)
    if not reservation['send']:return reservation['response']
    try:
        start=time.monotonic()
        if engine=='robust':
            resources_ready(run,c,pp)
            evaluator=ACTIVE_EVALUATOR.get()
            result=(evaluator(pp,cfg,Path(run)/'execution'/batch/'traces'/name) if evaluator is not None and not independent else client.evaluate(c['engine']['path'],pp,cfg,Path(run)/'execution'/batch/'traces'/name))
            for account in result['accounts']:verify_account(account,c['bundle_dir'])
        else:
            with tempfile.TemporaryDirectory(prefix='lorentz-legacy-plan-') as temporary:
                path=Path(temporary)/'plan.json';path.write_text(space.canonical(pp))
                server=enhanced_protocol.Server(c['legacy_engine']['path'],path)
                try:result=server.evaluate(cfg)
                finally:server.close()
        result['pipeline_wall_seconds']=time.monotonic()-start
        budget.finish(token,response=result)
        return result
    except client.EvaluationError as e:
        budget.finish(token,error=e);return None
    except BaseException as e:
        if isinstance(e,(KeyboardInterrupt,SystemExit)):raise
        budget.finish(token,error=e);budget.halt('data/protocol/accounting fault: '+str(e));raise


def batch_budget(run,c,batch,substudies=None):
    path=Path(run)/'execution'/'research-budget.sqlite'
    if not path.exists() and path.parent.exists() and any(p.name not in {'research-budget.sqlite-wal','research-budget.sqlite-shm'} for p in path.parent.iterdir()):
        raise ValueError('existing execution state has lost its durable budget; cannot reset or replay')
    return Budget(path,{'manifest_contract_sha256':space.identity(c)},
        {k:sum(v[k] for v in c['budgets'].values()) for k in COUNTERS},
        {**c['budgets'],**(substudies or search.search_limits())})


@managed_sessions
def run_fixed(run,batch,approval):
    m=authorize(run,batch,approval);c=m['contract']
    if batch not in ('R0','B1','future'):raise ValueError('fixed batch required')
    if not any(j['batch']==batch for j in c['jobs']):raise ValueError('no applicable frozen jobs for this interval/batch')
    budget=batch_budget(run,c,batch)
    folder=Path(run)/'execution'/batch;folder.mkdir(parents=True,exist_ok=True)
    with (folder/'run.lock').open('a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);budget.recover(batch);records=[]
        for j in (x for x in c['jobs'] if x['batch']==batch):
            r=fixed_request(run,c,budget,batch,j['name'],j['plan'],j['config'],j['engine'],independent=j['role'] in ('independent-replay','engine-comparison'))
            records.append({'name':j['name'],'role':j['role'],'status':'complete' if r is not None else 'failed','result':r})
            atomic_json(folder/'summary.json',{'version':VERSION,'batch':batch,'records':records,'budget':budget.snapshot(include_cache=False),'status':'in-progress'})
        by={r['name']:r['result'] for r in records};checks=[]
        for j in (x for x in c['jobs'] if x['batch']==batch and 'replay_of' in x):
            a,b=by[j['name']],by[j['replay_of']]
            if a is not None and b is not None:
                if [x['metrics'] for x in a['accounts']]!=[x['metrics'] for x in b['accounts']]:budget.halt('independent replay differs');raise client.ProtocolError('independent replay differs')
                checks.append({'replay':j['name'],'matched':True})
        if batch=='R0':
            for name in ('Classic101','aligned213'):
                old,new=by['compat-legacy-'+name],by['compat-robust-'+name]
                if old is not None and new is not None:
                    for oa,na in zip(old['folds'],new['accounts']):
                        for key,value in oa['metrics'].items():
                            if value is None:
                                if na['metrics'][key] is not None:raise client.ProtocolError('engine compatibility PF differs')
                            else:near(value,na['metrics'][key],name+' engine compatibility '+key)
                    checks.append({'candidate':name,'engine_compatibility':'verified-runtime-metrics','trace_compatibility':'new trace independently verified'})
        reports=[]
        for record in records:
            r=record['result']
            if r is not None and 'accounts' in r:
                rr=report.result_report(r,c['bundle_dir'],c['data_use'])
                if batch=='R0' and record['role']=='fixed-2025':rr['gate']=report.fixed_gate(rr['accounts'],'R0')
                reports.append({'name':record['name'],'report':rr})
        state={'version':VERSION,'batch':batch,'records':records,'checks':checks,'reports':reports,'budget':budget.snapshot(include_cache=False),
               'status':'complete-with-failures' if any(r['status']=='failed' for r in records) else 'complete','independent_audit':'pending'}
        atomic_json(folder/'summary.json',state);return state


def checkpoint(run,batch,origin=None):
    return Path(run)/'execution'/batch/((origin+'/summary.json') if origin else 'summary.json')


def prerequisites(run,c,batch):
    if batch in ('R0','B1'):return
    b1=load(checkpoint(run,'B1'))
    if b1['status']!='complete':raise ValueError('successful B1 including replays required before search')
    if c['interval']=='1h':
        r0=load(checkpoint(run,'R0'))
        if len([x for x in r0['checks'] if x.get('engine_compatibility')=='verified-runtime-metrics'])!=2:
            raise ValueError('budgeted R0 old/new engine compatibility required')
    if batch.startswith('F') or batch=='Q2026':
        outer=load(checkpoint(run,'W-outer'))
        if not outer.get('gate',{}).get('passed'):raise ValueError('W1-W4 retrospective support required before F/Q progression')


@managed_sessions
def run_search(run,batch,approval,until_attempts=None,origin=None,authorization_batch=None):
    c=authorize(run,authorization_batch or batch,approval)['contract'];prerequisites(run,c,batch)
    if batch not in ('W-search','F-search'):raise ValueError('search batch required')
    if authorization_batch and c['authorization_scopes'][authorization_batch].get('origin')!=origin:raise ValueError('authorized origin differs')
    budget=batch_budget(run,c,batch);contract=budget.binding['contract'];results=[]
    for stage in c['schedule']:
        if (stage['origin']=='F1')!=(batch=='F-search') or (origin is not None and stage['origin']!=origin):continue
        folder=Path(run)/'execution'/batch/stage['origin'];counter=[0]
        def evaluator(pp,cfg):
            counter[0]+=1
            if client.bundle_identity(c['bundle_dir'])!=c['bundle_identity'] or sha256(c['engine']['path'])!=c['engine']['sha256']:
                raise client.ProtocolError('frozen engine/bundle differs before send')
            trace=folder/'traces'/f'{time.time_ns()}-{counter[0]}'
            resources_ready(run,c,pp)
            r=ACTIVE_EVALUATOR.get()(pp,cfg,trace)
            for a in r['accounts']:verify_account(a,c['bundle_dir'])
            return r
        kwargs={'daily_history_start':validate_bundle(c['bundle_dir'])['daily_history_start'][:10],
                'validate_config':lambda cfg:space.go_validate(c['engine']['path'],[cfg]),'until_attempts':until_attempts}
        result=search.run_origin(folder,stage['training_plan'],c['defaults'],evaluator,budget,contract,origin=stage['origin'],**kwargs)
        results.append(result)
        if result['status']!='complete':break
    state={'batch':batch,'origins':results,'budget':budget.snapshot(include_cache=False),'status':'complete' if len(results)==(1 if origin is not None or batch=='F-search' else 4) and all(x['status']=='complete' for x in results) else 'paused','independent_audit':'pending'}
    atomic_json(checkpoint(run,batch),state);return state


@managed_sessions
def run_local(run,batch,approval,origin=None,authorization_batch=None):
    c=authorize(run,authorization_batch or batch,approval)['contract'];prerequisites(run,c,batch)
    if batch not in ('W-local','F-local'):raise ValueError('local batch required')
    if authorization_batch and c['authorization_scopes'][authorization_batch].get('origin')!=origin:raise ValueError('authorized origin differs')
    search_batch='F-search' if batch=='F-local' else 'W-search';budget=batch_budget(run,c,batch);results=[];selected_origin=origin
    folder=Path(run)/'execution'/batch;folder.mkdir(parents=True,exist_ok=True)
    with (folder/'run.lock').open('a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);budget.recover(batch)
        for stage in c['schedule']:
            origin=stage['origin']
            if (origin=='F1')!=(batch=='F-local') or (selected_origin is not None and origin!=selected_origin):continue
            ss=load(checkpoint(run,search_batch,origin))
            if ss['status']!='complete':raise ValueError('completed full origin search required before local screening')
            output=folder/origin;output.mkdir(parents=True,exist_ok=True);candidates=deepcopy(ss['central_candidates'])
            frozen=[]
            for candidate in candidates:
                kwargs={'validate_config':lambda cfg:space.go_validate(c['engine']['path'],[cfg])}
                if 'profile' in __import__('inspect').signature(search.neighbors).parameters:kwargs['profile']=search.compile_search_profile(stage['training_plan'],validate_bundle(c['bundle_dir'])['daily_history_start'])
                frozen.append(search.neighbors(candidate['config'],c['defaults'],origin,**kwargs))
            frozen_path=output/'frozen-neighborhoods.json'
            if frozen_path.exists():
                if space.canonical(load(frozen_path))!=space.canonical(frozen):raise ValueError('frozen neighborhood list changed')
            else:write_new(frozen_path,frozen)
            for candidate,neighborhood in zip(candidates,frozen):
                outcomes=[]
                for number,point in enumerate(neighborhood['points']):
                    outcomes.append(fixed_request(run,c,budget,batch,f'{origin}-neighbor-{candidate["config_identity"]}-{number}',stage['training_plan'],point['config']))
                candidate['neighborhood']=neighborhood;candidate['local_check']=search.neighborhood_check(candidate,neighborhood,outcomes)
                candidate['local_passed']=candidate['local_check']['passed']
                continuous=fixed_request(run,c,budget,batch,f'{origin}-continuous-{candidate["config_identity"]}',stage['continuous_plan'],candidate['config'])
                candidate['continuous_check']=search.continuous_check(continuous) if continuous else {'passed':False,'reason':'ordinary evaluation failure'}
                candidate['continuous_passed']=candidate['continuous_check']['passed']
                candidate['dependence_checks']=[]
                if continuous:
                    for a in continuous['accounts']:
                        if a['cost_multiplier']==1.5:
                            diag=report.account_diagnostics(a,c['bundle_dir']);candidate['dependence_checks'].append(diag['dependence'])
            controls={name:fixed_request(run,c,budget,batch,origin+'-public-'+name,stage['continuous_plan'],cfg) for name,cfg in c['public_controls'].items()}
            flows=search.select_flows(candidates)
            if flows['main'] is not None:
                family=flows['main']['source']['family']
                challenge=[x for x in candidates if x['local_passed'] and x['continuous_passed'] and x['source']['family']!=family]
                flows['challenger']=min(challenge,key=search.rank_key) if challenge else None
            else:flows['challenger']=None
            state={'origin':origin,'candidates':candidates,'flows':flows,'public_continuous_controls':controls,'status':'complete','evidence':'development_candidate only; no enablement','budget':budget.snapshot(include_cache=False)}
            atomic_json(output/'summary.json',state);results.append(state)
        state={'batch':batch,'origins':results,'status':'complete','budget':budget.snapshot(include_cache=False),'independent_audit':'pending'}
        atomic_json(checkpoint(run,batch),state);return state


def join_flow(segments,bundle):
    """Cash abstentions and actual paid-flat segments on one scaled equity path."""
    carry=peak=10000.;dd=0.;daily=[];trades=[];windows=[];components={'gross_pnl':0.,'fees':0.,'funding':0.}
    for segment in segments:
        w,a=segment['window'],segment['account'];start,end=utc_ms(w['start']),utc_ms(w['end'])
        if daily and daily[-1]['time']!=start:raise ValueError('flow windows not contiguous')
        if a is None:
            daily.extend({'time':t,'equity':carry} for t in range(start+DAY,end+1,DAY));windows.append({'window':w,'net_return_pct':0.,'trades':0});continue
        checked=verify_account(a,bundle)
        if a['account_mode']!='fixed-continuous-v1' or a['window']!=w:raise ValueError('outer fixed segment differs')
        factor=carry/10000.
        with Path(a['trace_path']).open() as stream:
            for line in stream:
                event=strict_loads(line)
                if event['sampled']:
                    eq=event['equity']*factor;peak=max(peak,eq);dd=max(dd,100*(peak-eq)/peak)
        for row in checked['daily_equity']:daily.append({'time':row['time'],'equity':row['equity']*factor})
        for trade in checked['trades']:
            trade=deepcopy(trade);trade['net_pnl']*=factor;trade['quantity']*=factor;trades.append(trade)
        for k in components:components[k]+=a['metrics'][k]*factor
        windows.append({'window':w,'net_return_pct':a['metrics']['net_return_pct'],'trades':a['metrics']['trades']});carry=checked['final_equity']*factor
    grid=[row['time'] for row in daily];eq=[10000]+[row['equity'] for row in daily]
    near(carry-10000,components['gross_pnl']-components['fees']+components['funding'],'flow scaled cash')
    held=sum(t['exit_time']-t['entry_time'] for t in trades)/(len(daily)*DAY)
    positives=sorted((t['net_pnl'] for t in trades if t['net_pnl']>0),reverse=True);positive_total=sum(positives)
    return {'cash_components':components,'time_in_market_fraction':held,'empty_fraction':1-held,
        'positive_concentration':{f'top{k}_share':sum(positives[:k])/positive_total if positive_total else None for k in (3,5)},
        'rolling':{str(length):[{'end':grid[i-1],'net_return_pct':100*(eq[i]/eq[i-length]-1)} for i in range(length,len(eq))] for length in (30,90,180)},
        'drawdown_measurement':'all observed execution boundaries and closed bars; no unobserved intrabar path claim','account_mode':'scheduled-flat-v1','boundary_policy':'paid half-year end exits; subsequent window empty; not M6','daily_equity':daily,
        'daily_log_returns':[math.log(b/a) for a,b in zip(eq,eq[1:])],'grid':grid,'net_return_pct':100*(carry/10000-1),'max_drawdown_pct':dd,
        'trades':len(trades),'windows':windows,'annual':report.period_returns(daily,10000,'year'),'quarterly':report.period_returns(daily,10000,'quarter'),
        'direction':{s:{'trades':sum(t['side']==s for t in trades),'net_pnl':sum(t['net_pnl'] for t in trades if t['side']==s)} for s in ('long','short')},
        'hold_days':[(t['exit_time']-t['entry_time'])/DAY for t in trades]}


def outer_gate(flows,uncertainty,training_warning):
    reasons=[];base=flows['main']['1']
    if base['trades']<100 or any(w['trades']<15 for w in base['windows']):reasons.append('trade sample')
    if sum(w['net_return_pct']>0 for w in base['windows'])<3 or min(w['net_return_pct'] for w in base['windows'])<-10:reasons.append('half-year stability')
    for cost in ('1','1.5','2'):
        x=flows['main'][cost]
        if (x['net_return_pct']<=0 if cost!='2' else x['net_return_pct']<0) or x['max_drawdown_pct']>(20 if cost=='1' else 25):reasons.append('cost '+cost)
    if sum(flows['seed-'+str(seed)]['1.5']['net_return_pct']>0 and flows['seed-'+str(seed)]['1.5']['max_drawdown_pct']<=25 for seed in search.SEEDS)<2:reasons.append('seed sensitivity')
    if training_warning:reasons.append('frozen training dependence warning')
    if any(uncertainty['blocks'][str(L)]['paths']['main-'+cost]['lower_95_one_sided']<=0 for L in report.BLOCK_LENGTHS for cost in ('1','1.5')):reasons.append('six absolute bootstrap lower bounds')
    return {'passed':not reasons,'status':'retrospective support' if not reasons else 'insufficient evidence','reasons':reasons}


@managed_sessions
def run_outer(run,approval,origin=None,authorization_batch=None):
    batch='W-outer';c=authorize(run,authorization_batch or batch,approval)['contract'];prerequisites(run,c,batch);budget=batch_budget(run,c,batch)
    if authorization_batch and c['authorization_scopes'][authorization_batch].get('origin')!=origin:raise ValueError('authorized origin differs')
    folder=Path(run)/'execution'/batch;folder.mkdir(parents=True,exist_ok=True)
    with (folder/'run.lock').open('a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);budget.recover(batch)
        for stage in c['schedule'][:4]:
            if origin is not None and stage['origin']!=origin:continue
            local=load(checkpoint(run,'W-local',stage['origin']));choices=local['flows'];configs={};roles={};warnings=False
            for role,choice in [('main',choices['main'])]+[('seed-'+str(seed),choices['seeds'][str(seed)]) for seed in search.SEEDS]:
                key=choice['config_identity'] if choice else None;roles[role]=key
                if choice:
                    configs[key]=choice['config'];warnings|=any(x['warning'] for x in choice.get('dependence_checks',[]))
            for name,cfg in c['public_controls'].items():key=space.identity(cfg);roles['public-'+name]=key;configs[key]=cfg
            responses={key:fixed_request(run,c,budget,batch,stage['origin']+'-'+key,stage['outer_plan'],cfg) for key,cfg in configs.items()}
            if any(responses[key] is None for key in configs):raise client.EvaluationError('frozen outer candidate failed: version cannot advance')
            atomic_json(folder/stage['origin']/'summary.json',{'origin':stage['origin'],'window':stage['outer_window'],'roles':roles,'responses':responses,'training_warning':warnings,'status':'complete'})
        if not all((folder/stage['origin']/'summary.json').exists() for stage in c['schedule'][:4]):
            state={'batch':batch,'origin':origin,'status':'partial','remaining_origins':[stage['origin'] for stage in c['schedule'][:4] if not (folder/stage['origin']/'summary.json').exists()],'budget':budget.snapshot(include_cache=False)}
            atomic_json(checkpoint(run,batch),state);return state
        segments={};warnings=False
        for stage in c['schedule'][:4]:
            saved=load(folder/stage['origin']/'summary.json');warnings|=saved['training_warning']
            for role,key in saved['roles'].items():
                for cost in (1,1.5,2):
                    response=saved['responses'].get(key)
                    account=next(a for a in response['accounts'] if a['cost_multiplier']==cost) if response else None
                    segments.setdefault((role,str(cost)),[]).append({'window':stage['outer_window'],'account':account})
        flows={}
        for (role,cost),ss in segments.items():flows.setdefault(role,{})[cost]=join_flow(ss,c['bundle_dir'])
        paths={role+'-'+cost:value['daily_log_returns'] for role,costs in flows.items() for cost,value in costs.items()}
        for control in ('classic','aligned'):
            for cost in ('1','1.5','2'):paths['main-minus-'+control+'-'+cost]=[a-b for a,b in zip(paths['main-'+cost],paths['public-'+control+'-'+cost])]
        uncertainty=report.uncertainty(paths,flows['main']['1']['grid']);gate=outer_gate(flows,uncertainty,warnings)
        state={'batch':batch,'status':'complete','evidence':'retrospective','flows':flows,'uncertainty':uncertainty,'gate':gate,'budget':budget.snapshot(include_cache=False),'independent_audit':'pending'}
        atomic_json(checkpoint(run,batch),state);return state


def run_origin_batch(run,origin,approval,until_attempts=None):
    c=authorize(run,origin,approval)['contract']
    if origin not in search.ORIGINS:raise ValueError('predeclared origin required')
    search_batch='F-search' if origin=='F1' else 'W-search';local_batch='F-local' if origin=='F1' else 'W-local'
    result=run_search(run,search_batch,approval,until_attempts,origin,origin)
    if result['status']!='complete':return result
    run_local(run,local_batch,approval,origin,origin)
    result=run_q(run,approval,origin) if origin=='F1' else run_outer(run,approval,origin,origin)
    # Aggregate budgets constrain every component; origin substudies/tokens also
    # freeze this exact 4608 + 152 + at-most-5 request layout.
    state={'origin':origin,'status':'complete','result_status':result['status'],'authorization_budget':c['authorization_scopes'][origin]['budget'],'independent_audit':'pending'}
    atomic_json(checkpoint(run,origin),state);return state


@managed_sessions
def run_q(run,approval,authorization_batch=None):
    batch='Q2026';c=authorize(run,authorization_batch or batch,approval)['contract'];prerequisites(run,c,batch);budget=batch_budget(run,c,batch)
    local=load(checkpoint(run,'F-local','F1'));flows=local['flows'];main=flows['main']
    if main is None:return {'status':'no qualified candidate','batch':batch}
    frozen={'main':main,'challenger':flows['challenger'],'public_controls':c['public_controls'],'Q_plan':c['schedule'][-1]['outer_plan']}
    path=Path(run)/'execution'/batch/'fixed-candidates.json';path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists():
        if space.canonical(load(path))!=space.canonical(frozen):raise ValueError('pre-Q main/challenger changed')
    else:write_new(path,frozen)
    results={};reports={}
    with (path.parent/'run.lock').open('a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);budget.recover(batch)
        configs={'main':main['config'],**{'public-'+k:v for k,v in c['public_controls'].items()}}
        if frozen['challenger']:configs['challenger']=frozen['challenger']['config']
        for role,cfg in configs.items():
            r=fixed_request(run,c,budget,batch,role,frozen['Q_plan'],cfg);results[role]=r
            if r is not None:reports[role]=report.result_report(r,c['bundle_dir'],'retrospective')
        gate=report.fixed_gate(reports['main']['accounts'],'Q2026') if 'main' in reports else {'passed':False,'status':'rejected','reasons':['frozen main ordinary evaluation failure']}
        uncertainty=None
        if 'main' in reports:
            paths={role+'-'+str(a['cost_multiplier']):a['daily_log_returns'] for role,rr in reports.items() for a in rr['accounts']}
            for control in ('classic','aligned'):
                for cost in ('1','1.5','2'):
                    if 'public-'+control in reports:paths['main-minus-'+control+'-'+cost]=[a-b for a,b in zip(paths['main-'+cost],paths['public-'+control+'-'+cost])]
            uncertainty=report.uncertainty(paths,[d['time'] for d in reports['main']['accounts'][0]['daily_equity']]);uncertainty['hard_gate']=False
        state={'batch':batch,'status':'complete','frozen':frozen,'results':results,'reports':reports,'gate':gate,'uncertainty':uncertainty,'budget':budget.snapshot(include_cache=False),'evidence':'exposed historical 2026 confirmation; not unseen final','independent_audit':'pending'}
        atomic_json(checkpoint(run,batch),state);return state


def freeze_configuration(bundle,engine,main,controls,out,known_exposed_before='2026-10-02T00:00:00Z',_now=None,training_dependence=None):
    """Freeze from actual current time; CLI cannot provide a historical freeze date."""
    now=_now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset().total_seconds()!=0:raise ValueError('actual UTC freeze timestamp required')
    bundle=Path(bundle).resolve();b=validate_bundle(bundle);engine=Path(engine).resolve()
    boundary=max(utc_ms(known_exposed_before),utc_ms(b['exposure']['known_exposed_before']),utc_ms(b['history_end']))
    start=max(int(now.timestamp()*1000)//DAY*DAY+DAY,((boundary+DAY-1)//DAY)*DAY)
    dt=datetime.fromtimestamp(start/1000,timezone.utc)
    try:end=dt.replace(year=dt.year+1)
    except ValueError:end=dt.replace(year=dt.year+1,day=28)
    if set(controls)!={'classic','aligned'}:raise ValueError('two predeclared public controls required')
    space.go_validate(engine,[main,*controls.values()])
    c={'version':'robust-future-freeze-v1','frozen_at':now.isoformat().replace('+00:00','Z'),'start':iso(start),'end':end.isoformat().replace('+00:00','Z'),
        'known_exposed_before':iso(boundary),'interval':b['interval'],'bundle_dir':str(bundle),'bundle_identity':client.bundle_identity(bundle),
        'history_start':'2020-01-01T00:00:00Z','daily_history_start':main['daily']['history_start']+'T00:00:00Z',
        'historical_dependency_end':b['history_end'],'engine':{'path':str(engine),'sha256':sha256(engine)},
        'runtime_versions':runtime_identity(),'helpers':helper_identity(),
        'main_config':deepcopy(main),'public_controls':deepcopy(controls),'account_mode':'fixed-continuous-v1','funding_mode':'exact',
        'cost_multipliers':[1,1.5,2],'endpoint_only':True,'calendar_months':12,'minimum_trades':50,'minimum_positive_quarters':3,
        'budget':limits([0,3,9]),'early_profit_confirmation_allowed':False,'evaluation_authorized':False,
        'statistics':{'replicates':5000,'blocks':[7,14,28],'seed_base':20261002,'quantile':'Type7-linear'},
        'training_dependence':training_dependence,'status':'frozen-awaiting-full-endpoint-and-separate-authorization'}
    write_new(out,c);return c


def freeze_from_run(run,out):
    c=load_run(run)['contract'];q=load(checkpoint(run,'Q2026'))
    if not q['gate']['passed']:raise ValueError('successful fixed 2026 retrospective confirmation required before future freeze')
    return freeze_configuration(c['bundle_dir'],c['engine']['path'],q['frozen']['main']['config'],c['public_controls'],out,c['protocol']['known_exposed_before'],training_dependence=q['frozen']['main'].get('dependence_checks'))


def prepare_future(frozen_path,dataset,out,attestation_path):
    frozen_path=Path(frozen_path).resolve();f=load(frozen_path);a=load(attestation_path)
    if f.get('version')!='robust-future-freeze-v1' or not f['endpoint_only'] or f['early_profit_confirmation_allowed']:raise ValueError('fixed future freeze contract required')
    if (a.get('frozen_sha256')!=sha256(frozen_path) or a.get('authorized_by')!='user' or a.get('data_use')!='prospective'
        or a.get('no_post_freeze_parameter_changes') is not True or a.get('no_selection_on_future_segment') is not True):
        raise ValueError('user declaration must bind the freeze and unseen-purpose/no-retuning contract')
    now=datetime.now(timezone.utc)
    if utc_ms(f['end'])>int(now.timestamp()*1000):raise ValueError('full frozen twelve-month endpoint has not elapsed; no early profitable stopping')
    if utc_ms(f['start'])%DAY or utc_ms(f['end'])%DAY or utc_ms(f['start'])<=utc_ms(f['frozen_at']) or utc_ms(f['start'])<utc_ms(f['known_exposed_before']):raise ValueError('half-day or exposed final rejected')
    for entry in [f['engine']]:
        if sha256(entry['path'])!=entry['sha256']:raise ValueError('frozen future engine changed')
    for path,digest in f['helpers'].items():
        if sha256(path)!=digest:raise ValueError('frozen future interface changed')
    if f.get('runtime_versions')!=runtime_identity():raise ValueError('frozen future package/runtime versions differ')
    if client.bundle_identity(f['bundle_dir'])!=f['bundle_identity']:raise ValueError('frozen historical bundle changed')
    successor=verify_successor(f['bundle_dir'],dataset,f['history_start'],f['daily_history_start'],f['historical_dependency_end'])
    b=validate_bundle(dataset)
    if utc_ms(b['history_end'])<utc_ms(f['end']):raise ValueError('future data does not cover full endpoint')
    pp=plan(dataset,f['interval'],[{'name':'future12m','start':f['start'],'end':f['end']}],history=f['history_start'])
    controls={'main':f['main_config'],**{'public-'+k:v for k,v in f['public_controls'].items()}}
    jobs=[];inspection=[]
    for name,cfg in controls.items():
        r=client.inspect(f['engine']['path'],pp,cfg);inspection.append({'name':name,'ready':r,'evaluation_calls':0})
        jobs.append({'batch':'future','name':name,'engine':'robust','plan':pp,'config':cfg,'ledgers':3,'role':'fixed-future'})
    c={'version':VERSION,'interval':f['interval'],'bundle_dir':str(Path(dataset).resolve()),'bundle_identity':client.bundle_identity(dataset),'engine':f['engine'],
       'legacy_engine':f['engine'],'sources':{str(frozen_path):sha256(frozen_path),str(Path(attestation_path).resolve()):sha256(attestation_path)},'helpers':f['helpers'],
       'authorization_scopes':{'future':{'budget':limits([0,3,9]),'funding_modes':['exact']}},'runtime':sys.version,'runtime_versions':f['runtime_versions'],'data_use':'prospective','jobs':jobs,'budgets':{'future':limits([0,3,9])},'inspect_completed':True,'freeze':f,'successor':successor,'evaluation_authorized':False}
    out=Path(out).resolve()
    if out.exists():raise FileExistsError('immutable future run exists')
    out.mkdir(parents=True);write_new(out/'manifest.json',{'version':VERSION,'contract_sha256':space.identity(c),'contract':c});write_new(out/'inspect.json',inspection)
    write_new(out/'approval-template.json',{'version':'robust-approval-v1','manifest_sha256':sha256(out/'manifest.json'),'authorized_by':None,'authorization_reference':None,
         'approved':False,'batches':[],'budgets':{'future':limits([0,3,9])},'funding_modes':{'future':['exact']}})
    return c


def run_future(run,approval):
    state=run_fixed(run,'future',approval);c=load_run(run)['contract'];reports={r['name']:r['report'] for r in state['reports']}
    if 'main' not in reports:state['gate']={'passed':False,'status':'rejected','reasons':['frozen main ordinary evaluation failure']}
    else:
        accounts=reports['main']['accounts'];paths={str(a['cost_multiplier']):a['daily_log_returns'] for a in accounts}
        grid=[d['time'] for d in accounts[0]['daily_equity']]
        for name,r in reports.items():
            if name!='main':
                for account in r['accounts']:
                    cost=str(account['cost_multiplier']);paths[name+'-'+cost]=account['daily_log_returns'];paths['main-minus-'+name+'-'+cost]=[x-y for x,y in zip(paths[cost],paths[name+'-'+cost])]
        u=report.uncertainty(paths,grid);state['uncertainty']=u;state['gate']=report.fixed_gate(accounts,'future',u,training_warning=not c['freeze'].get('training_dependence') or any(x['warning'] for x in c['freeze']['training_dependence']))
    if state['gate'].get('status')=='insufficient evidence':state['status']='endpoint-evaluated-evidence-incomplete'
    state['evidence']='prospective frozen fixed twelve-month endpoint';atomic_json(checkpoint(run,'future'),state);return state


def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='command',required=True)
    p=sub.add_parser('prepare',help='compile immutable manifests; inspect never sends a strategy request')
    p.add_argument('--protocol',type=Path,required=True);p.add_argument('--interval',choices=['15m','1h','4h','1d'],required=True)
    p.add_argument('--dataset',type=Path,required=True);p.add_argument('--out',type=Path,required=True);p.add_argument('--inspect',action='store_true');p.add_argument('--binary',type=Path)
    p=sub.add_parser('run',help='execute only a full manifest with explicit matching user approval')
    p.add_argument('--run',type=Path,required=True);p.add_argument('--batch',choices=['R0','B1','W1','W2','W3','W4','F1','W-search','W-local','W-outer','F-search','F-local','Q2026','future'],required=True)
    p.add_argument('--approval',type=Path,required=True);p.add_argument('--until-attempts',type=int)
    p=sub.add_parser('report',help='read stored verified results; does not evaluate market data');p.add_argument('--run',type=Path,required=True);p.add_argument('--batch',required=True)
    p=sub.add_parser('freeze',help='freeze successful Q main at actual time, first unseen full UTC day');p.add_argument('--run',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
    p=sub.add_parser('append',help='compile separately approved future endpoint using an immutable data successor')
    p.add_argument('--frozen',type=Path,required=True);p.add_argument('--dataset',type=Path,required=True);p.add_argument('--attestation',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
    args=parser.parse_args()
    if args.command=='prepare':result=prepare_run(args.protocol,args.interval,args.dataset,args.out,args.inspect,args.binary)
    elif args.command=='freeze':result=freeze_from_run(args.run,args.out)
    elif args.command=='append':result=prepare_future(args.frozen,args.dataset,args.out,args.attestation)
    elif args.command=='report':
        c=load_run(args.run)['contract'];result=load(checkpoint(args.run,args.batch))
        for record in result.get('records',[]):
            if record.get('result'):
                for account in record['result'].get('accounts',[]):verify_account(account,c['bundle_dir'])
        print(space.canonical({'stored_report':str(checkpoint(args.run,args.batch).resolve()),'evidence':result.get('evidence',c['data_use']),'status':result['status']}))
    elif args.batch in search.ORIGINS:result=run_origin_batch(args.run,args.batch,args.approval,args.until_attempts)
    elif args.batch in ('R0','B1'):result=run_fixed(args.run,args.batch,args.approval)
    elif args.batch in ('W-search','F-search'):result=run_search(args.run,args.batch,args.approval,args.until_attempts)
    elif args.batch in ('W-local','F-local'):result=run_local(args.run,args.batch,args.approval)
    elif args.batch=='W-outer':result=run_outer(args.run,args.approval)
    elif args.batch=='Q2026':result=run_q(args.run,args.approval)
    else:result=run_future(args.run,args.approval)
    print(space.canonical({'command':args.command,'status':result.get('status','prepared'),'evaluation_authorized':result.get('evaluation_authorized',False)}))

if __name__=='__main__':main()

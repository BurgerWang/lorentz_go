"""Verified ledger diagnostics and the predeclared robust-v1 uncertainty test.

No signal simulation, parameter selection or market evaluation occurs here.
"""
from datetime import datetime, timezone
import hashlib
import calendar
import math
from pathlib import Path
import random
import statistics
import sys
from enhanced import strict_loads
from enhanced_space import identity
from robust_data import DAY, utc_ms
from robust_trace import verify_account

BLOCK_LENGTHS = (7, 14, 28)
REPLICATES = 5000
BASE_SEED = 20261002


def type7(values, probability):
    if not values or not 0 <= probability <= 1:
        raise ValueError('nonempty quantile and probability required')
    v = sorted(values); x = (len(v)-1)*probability; i = int(x)
    return v[i] + (v[min(i+1,len(v)-1)]-v[i])*(x-i)


def stationary_indices(n, block):
    if type(n) is not int or n < 1 or block not in BLOCK_LENGTHS:
        raise ValueError('complete daily grid and frozen block length required')
    rng = random.Random(BASE_SEED+block)
    for _ in range(REPLICATES):
        i = rng.randrange(n); row = [i]
        for _ in range(1,n):
            i = rng.randrange(n) if rng.random() < 1/block else (i+1)%n
            row.append(i)
        yield row


def uncertainty(paths, grid):
    """One common index draw per L across every cost/candidate/control path."""
    n=len(grid)
    if n>5000 or len(paths)>32:raise ValueError('frozen statistics resource limit exceeded')
    if not paths or not n or any(b-a != DAY for a,b in zip(grid,grid[1:])):
        raise ValueError('common complete UTC day grid required')
    if any(t%DAY for t in grid): raise ValueError('partial UTC day rejected')
    if any(len(v)!=n or any(type(x) not in (float,int) or not math.isfinite(x) for x in v) for v in paths.values()):
        raise ValueError('daily path grids differ or contain nonfinite values')
    rows={}
    # Index generator is independent of paths and reused for every series.
    for block in BLOCK_LENGTHS:
        draws={key:[] for key in paths}
        indices_digest=hashlib.sha256()
        for indices in stationary_indices(n,block):
            indices_digest.update(','.join(map(str,indices)).encode()+b'\n')
            for key,values in paths.items(): draws[key].append(math.fsum(values[i] for i in indices)/n)
        rows[str(block)]={'seed':BASE_SEED+block,'indices_sha256':indices_digest.hexdigest(),
            'paths':{key:{'mean_daily_log_return':statistics.mean(paths[key]),'lower_95_one_sided':type7(v,.05),
                          'two_sided_95':[type7(v,.025),type7(v,.975)]} for key,v in draws.items()}}
    return {'method':'Politis-Romano-stationary-bootstrap-v1','replicates':REPLICATES,
            'quantile':'Type7-linear','runtime':sys.version,'grid_sha256':identity(grid),'N':n,'blocks':rows,
            'interpretation':'conditional historical uncertainty; does not remove search selection bias or guarantee future profit'}


def dependence_warning(log_returns, hold_days):
    n=len(log_returns)
    if not n: raise ValueError('missing daily returns')
    mean=statistics.mean(log_returns); centered=[x-mean for x in log_returns]
    denom=math.fsum(x*x for x in centered); limit=2/math.sqrt(n)
    acf={lag:(math.fsum(centered[i]*centered[i-lag] for i in range(lag,n))/denom if denom else 0.) for lag in range(29,57)}
    consecutive=any(all(abs(acf[k])>limit for k in range(j,j+3)) for j in range(29,55))
    p95=type7(hold_days,.95) if hold_days else 0.
    return {'warning':p95>28 or consecutive or denom==0,'hold_days_p95':p95,
            'acf_29_56':{str(k):v for k,v in acf.items()},'acf_threshold':limit,
            'three_consecutive_acf_exceedances':consecutive,'zero_variance':denom==0,
            'effect':'warning blocks a passing uncertainty conclusion; changing block lengths requires a new protocol'}


def period_returns(daily, initial, period):
    values={};prior=initial
    for row in daily:
        dt=datetime.fromtimestamp((row['time']-DAY)/1000,timezone.utc)
        key=str(dt.year) if period=='year' else f'{dt.year}-Q{(dt.month-1)//3+1}'
        if key not in values: values[key]={'initial_equity':prior,'final_equity':row['equity'],'days':0}
        values[key]['final_equity']=row['equity'];values[key]['days']+=1;prior=row['equity']
    return {k:{**v,'net_return_pct':100*(v['final_equity']/v['initial_equity']-1)} for k,v in values.items()}


def account_diagnostics(account,bundle):
    c=verify_account(account,bundle);trades=c['trades'];start=utc_ms(account['window']['start']);end=utc_ms(account['window']['end'])
    durations=[(t['exit_time']-t['entry_time'])/DAY for t in trades]
    held=sum(t['exit_time']-t['entry_time'] for t in trades)/(end-start)
    positives=sorted((t['net_pnl'] for t in trades if t['net_pnl']>0),reverse=True);positive_total=math.fsum(positives)
    side={s:{'trades':sum(t['side']==s for t in trades),'net_pnl':math.fsum(t['net_pnl'] for t in trades if t['side']==s)} for s in ('long','short')}
    equity=[c['initial_equity']]+[d['equity'] for d in c['daily_equity']]
    rolling={str(length):[{'end':c['daily_equity'][i-1]['time'],'net_return_pct':100*(equity[i]/equity[i-length]-1)} for i in range(length,len(equity))] for length in (30,90,180)}
    return {'account_mode':account['account_mode'],'window':account['window'],'cost_multiplier':account['cost_multiplier'],
            'funding_scenario':account['funding_scenario'],'funding_disclosure':account['funding_disclosure'],
            'drawdown_measurement':'all observed execution boundaries and closed bars; no unobserved intrabar path claim','metrics':c['metrics'],'daily_equity':c['daily_equity'],'daily_log_returns':c['daily_log_returns'],
            'annual':period_returns(c['daily_equity'],c['initial_equity'],'year'),
            'quarterly':period_returns(c['daily_equity'],c['initial_equity'],'quarter'),
            'direction':side,'hold_days':durations,'time_in_market_fraction':held,'empty_fraction':1-held,
            'positive_concentration':{f'top{k}_share':math.fsum(positives[:k])/positive_total if positive_total else None for k in (3,5)},
            'rolling':rolling,'dependence':dependence_warning(c['daily_log_returns'],durations),
            'endpoint_trades':sum(t.get('reason') in ('end-of-window','window_end','end','end-of-data','end_of_window') for t in trades)}


def result_report(result,bundle,evidence='retrospective',selection=None,neighbors=None,all_seeds=None):
    if evidence not in ('engineering','retrospective','prospective'):raise ValueError('explicit evidence class required')
    accounts=[account_diagnostics(a,bundle) for a in result['accounts']]
    return {'version':'robust-report-v1','evidence':evidence,'evidence_label':{'engineering':'工程合成验证','retrospective':'已暴露历史回顾研究','prospective':'冻结后未来固定评价'}[evidence],
        'configuration':result['config'],'plan':result['plan'],'accounts':accounts,
        'selection':selection,'neighbors':neighbors,'all_seeds':all_seeds,
        'profit_claim':'no live-profit claim; ledger results use frozen execution and cost assumptions'}


def fixed_gate(accounts,kind,dependence=None,training_warning=False):
    """R0/Q/future predeclared gates; bootstrap only hard for the future endpoint."""
    if kind not in ('R0','Q2026','future'):raise ValueError('fixed gate kind required')
    by={a['cost_multiplier']:a for a in accounts if a['funding_scenario']=='central'}
    if set(by)!={1,1.5,2}:raise ValueError('three exact fixed costs required')
    reasons=[]
    for cost,a in by.items():
        m=a['metrics']
        if (m['net_return_pct']<=0 if cost in (1,1.5) else m['net_return_pct']<0):reasons.append(f'cost {cost}: net return')
        if m['max_drawdown_pct']>(20 if cost==1 else 25):reasons.append(f'cost {cost}: drawdown')
    baseline=by[1];minimum=50 if kind=='future' else 30
    if baseline['metrics']['trades']<minimum:reasons.append('insufficient trades')
    if kind in ('Q2026','future'):
        required=3 if kind=='future' else 2
        quarter_values=baseline['quarterly']
        if kind=='future':
            start=datetime.fromtimestamp(utc_ms(baseline['window']['start'])/1000,timezone.utc);daily=baseline['daily_equity'];equity={row['time']:row['equity'] for row in daily};prior=10000.;quarter_values={}
            for i in range(1,5):
                m=start.year*12+start.month-1+3*i;year,month=m//12,m%12+1
                finish=start.replace(year=year,month=month,day=min(start.day,calendar.monthrange(year,month)[1]));t=int(finish.timestamp()*1000)
                if t not in equity:raise ValueError('future four calendar-quarter endpoint grid incomplete')
                quarter_values[str(i)]={'net_return_pct':100*(equity[t]/prior-1)};prior=equity[t]
        if sum(v['net_return_pct']>0 for v in quarter_values.values())<required:reasons.append('quarterly positivity')
    if kind=='R0':
        daily=baseline['daily_equity'];start=utc_ms(baseline['window']['start']);year=datetime.fromtimestamp(start/1000,timezone.utc).year
        split=int(datetime(year,7,1,tzinfo=timezone.utc).timestamp()*1000);half=(split-start)//DAY;equity=[10000]+[d['equity'] for d in daily]
        if max(equity[half]/equity[0]-1,equity[-1]/equity[half]-1)<=0:reasons.append('half-year positivity')
    if kind=='future':
        if dependence is None or training_warning:reasons.append('dependence warning or missing frozen training adequacy')
        if dependence is not None:
            if any(dependence['blocks'][str(L)]['paths'][str(c)]['lower_95_one_sided']<=0 for L in BLOCK_LENGTHS for c in (1,1.5)):reasons.append('six absolute bootstrap lower bounds')
    return {'passed':not reasons,'reasons':reasons,'status':'insufficient evidence' if 'insufficient trades' in reasons else ('passed' if not reasons else 'rejected')}

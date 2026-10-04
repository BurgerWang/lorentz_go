"""Independent accounting from streamed ledger observations; no strategy model."""
import hashlib,math,statistics
from pathlib import Path
from datetime import datetime,timezone
from enhanced import strict_loads
from robust_data import DAY,utc_ms,read_candles,sha256,validate_bundle
from functools import lru_cache

@lru_cache(maxsize=8)
def _validated_bundle(root,manifest_digest,file_digests):
    result=validate_bundle(root)
    if sha256(Path(root)/'bundle.json')!=manifest_digest:raise ValueError('bundle changed during trace verification')
    return result

def _trace_bundle(root):
    path=Path(root)/'bundle.json';digest=sha256(path);b=strict_loads(path.read_text())
    files=[Path(root)/v['path'] for v in b['files'].values()]
    if 'marks' in b['sources']:files.append(Path(root)/b['sources']['marks']['snapshot'])
    digests=tuple((str(p),sha256(p)) for p in files)
    return _validated_bundle(str(Path(root).resolve()),digest,digests)

@lru_cache(maxsize=8)
def _candles_snapshot(path,interval,digest):
    rows=read_candles(path,interval)
    if sha256(path)!=digest:raise ValueError('candles changed during trace source reading')
    return tuple(rows)

def _complete_layout(path,account,bundle):
    """Require every open/close and every held settlement from frozen inputs.

    This replays only the execution event grammar, without calculating signals
    or selecting a strategy. Optional entries/exits are observed, not invented.
    """
    root=Path(bundle);b=_trace_bundle(root);interval=b['interval']
    candle_path=root/b['files']['candles']['path'];digest=sha256(candle_path)
    if digest!=b['files']['candles']['sha256']:raise ValueError('trace candle input identity differs')
    rows=_candles_snapshot(str(candle_path),interval,digest)
    start,end=utc_ms(account['window']['start']),utc_ms(account['window']['end'])
    candles=[r for r in rows if start<=r[0] and r[6]<end]
    if not candles or candles[0][0]!=start or candles[-1][6]+1!=end:raise ValueError('trace scoring candles incomplete')
    fund_path=root/b['files']['funding']['path']
    if sha256(fund_path)!=b['files']['funding']['sha256']:raise ValueError('trace funding input identity differs')
    funding=strict_loads(fund_path.read_text());fi=0;position=None;trade_id=0;sequence=0
    with Path(path).open() as stream:
        events=(strict_loads(line) for line in stream);current=next(events,None)
        def take(phase,at,price=None):
            nonlocal current,sequence
            e=current
            if e is None or e.get('phase')!=phase or e.get('time')!=at:raise ValueError('missing/reordered execution or settlement: '+phase)
            sequence+=1
            if e.get('sequence')!=sequence:raise ValueError('trace unique sequence differs')
            if price is not None:near(e['price'],price,'execution candle price')
            if (phase=='funding')!=('funding' in e) or (phase=='exit')!=('trade' in e):raise ValueError('trace unexpected provenance payload')
            if phase!='entry' and phase!='exit' and e['fee']!=0:raise ValueError('fee without execution')
            if phase!='exit' and e['gross_pnl']!=0:raise ValueError('gross without exit')
            if phase!='funding' and e['funding_cash']!=0:raise ValueError('funding cash without settlement')
            wanted=trade_id if phase=='exit' else trade_id+1 if position is None else trade_id
            if e['trade_id']!=wanted:raise ValueError('trade identity differs')
            current=next(events,None);return e
        take('initial',start,0)
        def settle(until):
            nonlocal fi
            while fi<len(funding) and funding[fi]['time']<=until:
                f=funding[fi];fi+=1
                if position is None or f['time']<=position['entry_time']:continue
                e=take('funding',f['time'])
                if e['funding']!=f:raise ValueError('funding input/event differs')
        def exit_at(at,price,endpoint=False):
            nonlocal position
            if position is None:raise ValueError('exit grammar without position')
            e=take('exit',at,price);t=e['trade']
            if (t['reason']=='end_of_window')!=endpoint:raise ValueError('planned endpoint exit differs')
            if t['reason'] not in ('signal_exit','reverse','risk_exit','max_hold_bars','end_of_window'):raise ValueError('unknown execution exit reason')
            if t['entry_kind'] not in ('main_entry','pullback') or t['entry_time']!=position['entry_time'] or t['entry_decision_time']!=position['entry_time'] or not 0<=t['entry_available_time']<=t['entry_decision_time']:raise ValueError('next-open entry provenance differs')
            position=None
        for number,c in enumerate(candles):
            settle(c[0]);take('open',c[0],c[1])
            if current and current['phase']=='exit' and current['time']==c[0]:exit_at(c[0],c[1])
            if current and current['phase']=='entry' and current['time']==c[0]:
                if number==0 or position is not None:raise ValueError('entry before scored prior close or while held')
                trade_id+=1;position={'entry_time':c[0]};take('entry',c[0],c[1])
            settle(c[6])
            if number==len(candles)-1 and position is not None:exit_at(c[6],c[4],True)
            take('close',c[6],c[4])
        if current is not None or position is not None:raise ValueError('extra execution events or unfinished position')
    return {'source_completeness_verified':True,'candles':len(candles),'funding_input_rows':len(funding)}

def near(a,b,label):
    if not math.isfinite(a) or not math.isfinite(b) or not math.isclose(a,b,rel_tol=2e-9,abs_tol=1e-7):raise ValueError('trace accounting differs: '+label)

def verify_account(account,bundle=None):
    if bundle is None:raise ValueError('complete trace verification requires frozen candle/funding bundle')
    path=Path(account['trace_path'])
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=account['trace_sha256']:raise ValueError('trace file identity differs')
    layout=_complete_layout(path,account,bundle)
    cfg=account['ledger_config'];cash=initial=cfg['initial_equity'];peak=initial;dd=0.;side=0;qty=entry=0.;fees=gross=funding=0.;active=None;trades=[];daily=[];seq=0;last_time=-1
    counts={'open':0,'entry':0,'exit':0,'funding':0,'close':0}
    stress={};fundrows={}
    if bundle:
        b=Path(bundle);manifest=strict_loads((b/'bundle.json').read_text())
        stress={r['time']:r for r in strict_loads((b/manifest['files']['stress']['path']).read_text())}
        fundrows={r['time']:r for r in strict_loads((b/manifest['files']['funding']['path']).read_text())}
    with path.open() as stream:
        for line in stream:
            e=strict_loads(line);seq+=1;phase=e['phase'];t=e['time']
            if e['version']!='ledger-trace-v1' or e['sequence']!=seq or t<last_time or (seq==1 and phase!='initial'):raise ValueError('trace sequence/time differs')
            last_time=t
            if phase=='initial':
                if seq!=1 or t!=cfg['start_time'] or not e['sampled']:raise ValueError('trace initial differs')
            elif phase=='entry':
                if side or e['direction'] not in (-1,1):raise ValueError('trace entry while held')
                side=e['direction'];qty=e['quantity'];entry=e['entry_price'];entrycash=cash
                near(qty,cash*cfg['position_fraction']/entry,'entry sizing')
                near(entry,e['price']*(1+side*cfg['slippage_bps']/10000),'entry slippage')
                fee=qty*entry*cfg['fee_bps']/10000;near(e['fee'],fee,'entry fee');cash-=fee;fees+=fee
                active={'entry_time':t,'entry_equity':entrycash,'entry_price':entry,'quantity':qty,'side':'long' if side==1 else 'short','fee':fee,'funding':0.}
            elif phase=='funding':
                if not side or not active or t<=active['entry_time'] or e['sampled']:raise ValueError('funding boundary differs')
                f=e['funding']
                if f['time']!=t or (bundle and f!=fundrows[t]):raise ValueError('funding input differs')
                price=f['mark_price']
                if account['funding_scenario']=='proxy-adverse' and f.get('mark_price_source'):
                    if not bundle:raise ValueError('proxy trace requires bundle bounds')
                    bounds=stress[t];price=bounds['upper_mark'] if side*qty*f['rate']>0 else bounds['lower_mark']
                    if bounds['available_time']>t:raise ValueError('future funding input')
                near(e['price'],price,'funding scenario mark');pay=-side*qty*price*f['rate'];near(e['funding_cash'],pay,'funding cash')
                cash+=pay;funding+=pay;active['funding']+=pay
            elif phase=='exit':
                if not side or not active:raise ValueError('trace exit without entry')
                trade=e['trade'];price=e['price']*(1-side*cfg['slippage_bps']/10000);g=side*qty*(price-entry);fee=qty*price*cfg['fee_bps']/10000
                near(trade['exit_price'],price,'exit slippage');near(e['gross_pnl'],g,'gross pnl');near(e['fee'],fee,'exit fee')
                cash+=g-fee;fees+=fee;gross+=g;net=g-active['fee']-fee+active['funding']
                for key,value in {'entry_time':active['entry_time'],'exit_time':t,'quantity':qty,'entry_price':entry,'gross_pnl':g,'fees':active['fee']+fee,'funding':active['funding'],'net_pnl':net,'return_pct':100*net/active['entry_equity']}.items():near(trade[key],value,key)
                if trade['side']!=active['side'] or trade['entry_available_time']>trade['entry_decision_time'] or trade['entry_decision_time']>trade['entry_time']:raise ValueError('trade side/timing differs')
                trades.append(trade);side=0;qty=entry=0.;active=None
            elif phase not in ('open','close'):raise ValueError('unknown trace phase')
            if phase in counts:counts[phase]+=1
            near(e['cash'],cash,'cash path');near(e['equity'],cash+side*qty*(e['price']-entry),'equity path')
            if e['direction']!=side:raise ValueError('trace position differs')
            near(e['quantity'],qty,'quantity path');near(e['entry_price'],entry,'entry-price path')
            if bool(e['sampled'])!=(phase!='funding'):raise ValueError('drawdown sampling differs')
            if e['sampled']:
                if e['equity']<=0:raise ValueError('nonpositive protected path')
                peak=max(peak,e['equity']);dd=max(dd,100*(peak-e['equity'])/peak)
            if phase=='close' and (t+1)%DAY==0:daily.append({'time':t+1,'equity':e['equity']})
    if active or not seq:raise ValueError('unfinished trace account')
    m=account['metrics'];wins=sum(t['net_pnl']>0 for t in trades);losses=sum(t['net_pnl']<0 for t in trades)
    derived={'trades':len(trades),'wins':wins,'losses':losses,'breakeven':len(trades)-wins-losses,'win_rate_pct':100*wins/len(trades) if trades else 0.,'net_return_pct':100*(cash-initial)/initial,'max_drawdown_pct':dd,'gross_pnl':gross,'fees':fees,'funding':funding,'net_pnl':cash-initial,'expectancy_pct':statistics.mean(t['return_pct'] for t in trades) if trades else 0.}
    for k,v in derived.items():near(m[k],v,k)
    gains=sum(t['net_pnl'] for t in trades if t['net_pnl']>0);loss=-sum(t['net_pnl'] for t in trades if t['net_pnl']<0)
    pf=gains/loss if loss else None
    if pf is None:
        if m['profit_factor'] is not None:raise ValueError('profit factor null differs')
    else:near(m['profit_factor'],pf,'profit factor')
    start,end=utc_ms(account['window']['start']),utc_ms(account['window']['end'])
    if [d['time'] for d in daily]!=list(range(start+DAY,end+1,DAY)):raise ValueError('incomplete common UTC daily grid')
    values=[initial]+[d['equity'] for d in daily];returns=[math.log(b/a) for a,b in zip(values,values[1:])]
    return {'verified':True,**layout,'events':seq,'phase_counts':counts,'metrics':derived,'initial_equity':initial,'final_equity':cash,'daily_equity':daily,'daily_log_returns':returns,'trades':trades}

def scheduled_flat(accounts,bundle=None):
    """Scale 1x independently flat windows and join every actual DD sample."""
    if not accounts:raise ValueError('missing scheduled-flat windows')
    carry=10000.;peak=carry;dd=0.;daily=[];totals={'gross_pnl':0.,'fees':0.,'funding':0.};trades=0;last_end=None
    for a in accounts:
        checked=verify_account(a,bundle)
        if a['account_mode']!='fixed-continuous-v1':raise ValueError('scheduled-flat requires independently fixed full windows')
        start,end=utc_ms(a['window']['start']),utc_ms(a['window']['end'])
        if last_end is not None and start!=last_end:raise ValueError('scheduled-flat noncontiguous windows')
        last_end=end;factor=carry/checked['initial_equity']
        with Path(a['trace_path']).open() as f:
            for line in f:
                e=strict_loads(line)
                if e['sampled']:
                    eq=e['equity']*factor;peak=max(peak,eq);dd=max(dd,100*(peak-eq)/peak)
        for d in checked['daily_equity']:daily.append({'time':d['time'],'equity':d['equity']*factor})
        for k in totals:totals[k]+=a['metrics'][k]*factor
        trades+=a['metrics']['trades'];carry=checked['final_equity']*factor
    near(carry-10000,totals['gross_pnl']-totals['fees']+totals['funding'],'scheduled scaled cash')
    return {'account_mode':'scheduled-flat-v1','initial_equity':10000,'final_equity':carry,'net_return_pct':100*(carry/10000-1),'max_drawdown_pct':dd,'trades':trades,'cash_components':totals,'daily_equity':daily,'boundary_policy':'paid end-of-window exits; empty position at each new half-year; not M6 cross-window holdings'}

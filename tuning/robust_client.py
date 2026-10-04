"""Protocol3 adapter. The Go engine alone computes signals and ledger results."""
from pathlib import Path
import collections
import hashlib
import os
import selectors
import subprocess
import tempfile
import threading
from contextlib import nullcontext
from copy import deepcopy
import enhanced
import enhanced_protocol
import optimize
from enhanced_space import canonical
from robust_data import validate_bundle

ProtocolError = optimize.ProtocolError
EvaluationError = optimize.EvaluationError

def normalize_plan(plan):
    p=deepcopy(plan)
    p['bundle_dir']=str(Path(p['bundle_dir']).resolve())
    p['history_start']=enhanced_protocol.utc(p['history_start'])
    for w in p['windows']:
        w['start'],w['end']=enhanced_protocol.utc(w['start']),enhanced_protocol.utc(w['end'])
    return p

def bundle_identity(directory):
    root=Path(directory);b=validate_bundle(root)
    h=hashlib.sha256((root/'bundle.json').read_bytes())
    for k in ('candles','daily','funding','stress'):h.update(f"{k}:{b['files'][k]['sha256']}:".encode())
    return h.hexdigest()

def validate_ready(r,plan):
    enhanced_protocol.finite_tree(r)
    count=len(plan['windows'])*len(plan['funding_scenarios'])*len(plan['cost_multipliers'])
    if (r.get('type')!='ready' or r.get('protocol_version')!=3 or r.get('version')!='robust-eval-v1'
        or r.get('plan')!=normalize_plan(plan) or r.get('bundle_identity')!=bundle_identity(plan['bundle_dir'])
        or type(r.get('ledger_evaluations')) is not int or r['ledger_evaluations']!=count
        or r.get('evaluation_calls')!=0 or not isinstance(r.get('default_config'),dict)):
        raise ProtocolError('robust ready contract differs')
    return r

def validate_result(r,plan,config,ready):
    try:
        enhanced_protocol.finite_tree(r)
        if (r.get('type')!='result' or r.get('protocol_version')!=3 or r.get('version')!='robust-eval-v1'
            or r.get('plan')!=normalize_plan(plan) or r.get('bundle_identity')!=ready['bundle_identity']
            or canonical(r.get('config'))!=canonical(config)
            or type(r.get('ledger_evaluations')) is not int or r['ledger_evaluations']!=ready['ledger_evaluations']):
            raise ProtocolError('robust result identity differs')
        expected=[(w,s,c) for w in ready['plan']['windows'] for s in plan['funding_scenarios'] for c in plan['cost_multipliers']]
        if not isinstance(r.get('accounts'),list) or len(r['accounts'])!=len(expected):raise ProtocolError('robust account count differs')
        for a,(w,s,c) in zip(r['accounts'],expected):
            if a.get('window')!=w or a.get('funding_scenario')!=s or a.get('cost_multiplier')!=c or a.get('account_mode')!=plan['account_mode']:raise ProtocolError('robust account identity differs')
            m=a['metrics'];counts={'trades','wins','losses','breakeven'};numbers={'win_rate_pct','net_return_pct','max_drawdown_pct','expectancy_pct','gross_pnl','fees','funding','net_pnl'}
            if set(m)!=counts|numbers|{'profit_factor'} or any(type(m[k]) is not int or m[k]<0 for k in counts) or any(not enhanced_protocol.number(m[k]) for k in numbers):raise ProtocolError('robust metric schema')
            if m['wins']+m['losses']+m['breakeven']!=m['trades'] or not 0<=m['max_drawdown_pct']<=100 or not 0<=m['win_rate_pct']<=100 or m['net_return_pct']<=-100 or m['fees']<0:raise ProtocolError('robust metric accounting')
            if m['profit_factor'] is not None and (not enhanced_protocol.number(m['profit_factor']) or m['profit_factor']<0):raise ProtocolError('invalid profit factor')
            b=a['ledger_config']
            if b['initial_equity']!=10000 or b['position_fraction']!=1 or b['fee_bps']!=5*c or b['slippage_bps']!=2*c:raise ProtocolError('ledger safeguards/costs changed')
            d=a['funding_disclosure']
            if d['mode']!=plan['funding_mode'] or any(type(d[k]) is not int or d[k]<0 for k in ('exact_rows','proxy_rows')) or d['signed_cash']!=m['funding'] or d['adverse_is_proven_bound'] is not False or (plan['funding_mode']=='exact' and d['proxy_rows']):raise ProtocolError('funding disclosure differs')
        if any(not enhanced_protocol.number(r['timing'][k]) or r['timing'][k]<0 for k in ('strategy_seconds','ledger_seconds','request_seconds')):raise ProtocolError('robust timing')
        return r
    except (KeyError,TypeError,ValueError,OverflowError,RecursionError,AttributeError) as e:raise ProtocolError('invalid robust response structure') from e

class Server(enhanced_protocol.Server):
    def __init__(self,binary,plan,timeout=600,termination=None):
        self.source_plan=enhanced.strict_loads(Path(plan).read_text());self.timeout=timeout;self.termination=termination
        self.buffer=b'';self.stderr=collections.deque(maxlen=100);self.process=self.selector=self.thread=None;self.next_id=0
        try:
            with termination.defer() if termination else nullcontext():
                self.process=subprocess.Popen([str(binary),'robust-eval','-plan',str(plan)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
                self.selector=selectors.DefaultSelector();self.selector.register(self.process.stdout,selectors.EVENT_READ)
                self.thread=threading.Thread(target=self._drain_stderr,daemon=True);self.thread.start()
            self.ready=self._read();validate_ready(self.ready,self.source_plan)
        except BaseException:self.close();raise
    def evaluate(self,config,trace_dir=None):
        self.next_id+=1
        req={'id':self.next_id,'config':config,'trace_dir':str(Path(trace_dir).resolve()) if trace_dir else ''}
        try:self.process.stdin.write((canonical(req)+'\n').encode());self.process.stdin.flush()
        except OSError as e:raise ProtocolError('robust request pipe failed') from e
        r=self._read()
        if type(r.get('id')) is not int or r['id']!=self.next_id:raise ProtocolError('robust response ID differs')
        if r.get('type')=='error':
            if r.get('error_kind') in ('equity_protection','configuration_insufficient'):raise EvaluationError(r.get('error','equity protection'))
            raise ProtocolError('robust engine error: '+str(r))
        return validate_result(r,self.source_plan,config,self.ready)

def inspect(binary,plan,config=None,timeout=600):
    p=enhanced.strict_loads(Path(plan).read_text()) if isinstance(plan,(str,Path)) else plan
    with tempfile.TemporaryDirectory(prefix='lorentz-inspect-') as tmp:
        path=Path(tmp)/'plan.json';path.write_text(canonical(p));cmd=[str(Path(binary).resolve()),'robust-eval','-plan',str(path),'-inspect']
        if config is not None:
            cp=Path(tmp)/'config.json';cp.write_text(canonical(config));cmd+=['-config',str(cp)]
        try:r=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=timeout)
        except (OSError,subprocess.TimeoutExpired) as e:raise ProtocolError('robust inspect failed') from e
        if r.returncode:raise ProtocolError(r.stderr.decode(errors='replace'))
        try:return validate_ready(enhanced.strict_loads(r.stdout),p)
        except (ValueError,RecursionError) as e:raise ProtocolError('robust inspect invalid JSON') from e

def evaluate(binary,plan,config,trace_dir=None,timeout=600):
    with tempfile.TemporaryDirectory(prefix='lorentz-eval-') as tmp:
        pp=Path(tmp)/'plan.json';pp.write_text(canonical(plan))
        with optimize.controlled_termination() as termination:
            server=Server(Path(binary).resolve(),pp,timeout,termination)
            try:r=server.evaluate(config,trace_dir);r['timing']['load_seconds']=server.ready['load_seconds'];return r
            finally:server.close()

class Evaluator:
    """Serial persistent Go sessions per immutable plan, with live file checks.

    Independent child studies may each own one instance. Sharing this object
    between concurrent workers is intentionally unsupported.
    """
    def __init__(self,binary,timeout=600):
        self.binary=Path(binary).resolve();self.timeout=timeout;self.tmp=tempfile.TemporaryDirectory(prefix='lorentz-sessions-');self.sessions={}
        self.engine_hash=hashlib.sha256(self.binary.read_bytes()).hexdigest()
    def _snapshots(self,plan):
        root=Path(plan['bundle_dir']);b=enhanced.strict_loads((root/'bundle.json').read_text())
        paths=[root/'bundle.json']+[root/v['path'] for v in b['files'].values()]
        if 'marks' in b['sources']:paths.append(root/b['sources']['marks']['snapshot'])
        return {str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    def __call__(self,plan,config,trace_dir=None):
        key=hashlib.sha256(canonical(normalize_plan(plan)).encode()).hexdigest()
        if key not in self.sessions:
            pp=Path(self.tmp.name)/(key+'.json');pp.write_text(canonical(plan))
            snapshots=self._snapshots(plan)
            server=Server(self.binary,pp,self.timeout)
            if self._snapshots(plan)!=snapshots:server.close();raise ProtocolError('data changed during persistent session loading')
            self.sessions[key]=(server,snapshots)
        server,snapshots=self.sessions[key]
        if hashlib.sha256(self.binary.read_bytes()).hexdigest()!=self.engine_hash or self._snapshots(plan)!=snapshots:raise ProtocolError('immutable engine/data changed during evaluation')
        result=server.evaluate(config,trace_dir)
        if self._snapshots(plan)!=snapshots:raise ProtocolError('data changed during evaluation')
        result['timing']['load_seconds']=server.ready['load_seconds']
        return result
    def close(self):
        for s,_ in self.sessions.values():s.close()
        self.sessions.clear();self.tmp.cleanup()
    def __enter__(self):return self
    def __exit__(self,*exc):self.close()

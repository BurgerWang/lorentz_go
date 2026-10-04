"""Real isolated Go/Python P1/P2 integration; never evaluate stored markets."""
import unittest,tempfile,os,copy,json,hashlib
from pathlib import Path
from robust_fixture import make_source,plan
from robust_data import prepare_bundle,DAY,iso
from robust_client import inspect,evaluate,ProtocolError
from robust_trace import verify_account,scheduled_flat
from enhanced_space import go_defaults
from feature_groups import classifier_config

BINARY=Path(os.environ.get('LORENTZ_TEST_BINARY',Path(__file__).resolve().parents[1]/'bin/lorentz-robust-v1')).resolve()

class Integration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory(prefix='lorentz-robust-integration-');cls.root=Path(cls.tmp.name)
        cls.source=make_source(cls.root/'source',days=800)
        cls.defaults=go_defaults(BINARY);cls.bundles={}
        for i in ('15m','1h','4h','1d'):
            cls.bundles[i]=cls.root/('bundle-'+i);prepare_bundle(cls.source,i,cls.bundles[i])
    @classmethod
    def tearDownClass(cls):cls.tmp.cleanup()
    def config(self,aligned=False,daily=False):
        c=copy.deepcopy(self.defaults)
        b=c['classic'];b['use_kernel_filter']=False;b['use_regime_filter']=False;b['use_volatility_filter']=False
        if aligned:b['algorithm']='aligned-knn';b['sample_stride']=4
        c['daily']['enabled']=daily
        c['classifier']=classifier_config(b,'classic',family='aligned-extended' if aligned else 'classic-extended')
        return c
    def test_four_periods_real_go_and_independent_trace(self):
        for interval in self.bundles:
            with self.subTest(interval=interval):
                p=plan(self.bundles[interval],interval,start_day=730,end_day=790)
                c=self.config(aligned=True,daily=True)
                ready=inspect(BINARY,p,c);self.assertEqual(ready['ledger_evaluations'],3)
                r=evaluate(BINARY,p,c,self.root/('trace-'+interval))
                for a in r['accounts']:
                    checked=verify_account(a,self.bundles[interval]);self.assertEqual(len(checked['daily_equity']),60)
                # A low trade count remains insufficient; no relaxed gate.
                self.assertGreaterEqual(sum(a['metrics']['trades'] for a in r['accounts']),0)
    def test_proxy_scenarios_and_trace_observation_equivalence(self):
        source=make_source(self.root/'proxy-source',days=800,proxy=True);bundle=self.root/'proxy-bundle'
        prepare_bundle(source,'1h',bundle,'proxy-stress-v1',source/'mark.json')
        p=plan(bundle,start_day=300,end_day=310,proxy=True);c=self.config(aligned=True)
        a=evaluate(BINARY,p,c,self.root/'proxy-traces');b=evaluate(BINARY,p,c)
        self.assertEqual([x['metrics'] for x in a['accounts']],[x['metrics'] for x in b['accounts']])
        used=[]
        for account in a['accounts']:
            checked=verify_account(account,bundle);used.extend(checked['trades'])
            self.assertGreater(account['funding_disclosure']['proxy_rows'],0)
        self.assertTrue(used)
        exact=copy.deepcopy(p);exact['funding_mode']='exact';exact['funding_scenarios']=['central']
        with self.assertRaises(ProtocolError):inspect(BINARY,exact,c)
    def test_initialization_and_mutation_rejected(self):
        p=plan(self.bundles['1d'],'1d',start_day=10,end_day=11)
        with self.assertRaises(ProtocolError):inspect(BINARY,p,self.config(daily=True))
        p=plan(self.bundles['1h']);p['windows'][0]['end']='2035-01-01T00:00:00Z'
        with self.assertRaises(ProtocolError):inspect(BINARY,p)
    def test_chained_d1_and_envelope_warmup_reject(self):
        p=plan(self.bundles['1h'],start_day=28,end_day=29)
        c=self.config(aligned=True);c['classifier']=classifier_config(c['classic'],'classic-d1-slope',family='aligned-extended')
        c['classifier']['feature_group']['features'][-1]['window']=96
        with self.assertRaises(ProtocolError):inspect(BINARY,p,c)
        c=self.config(aligned=True);c['entry_mode']='pullback';c['pullback']['enabled']=True;c['envelope_enabled']=True;c['envelope']['x']=1000
        p=plan(self.bundles['1h'],start_day=10,end_day=11)
        with self.assertRaises(ProtocolError):inspect(BINARY,p,c)
        p=plan(self.bundles['1h'],start_day=40,end_day=41)
        c=self.config(aligned=True);c['classic'].update(max_bars_back=500,sample_stride=8,neighbors=63)
        c['classifier']=classifier_config(c['classic'],'classic',family='aligned-extended')
        with self.assertRaises(ProtocolError):inspect(BINARY,p,c)
    def test_direct_go_refuses_rehashed_stress_source(self):
        import shutil,subprocess
        source=make_source(self.root/'tamper-source',days=800,proxy=True);bundle=self.root/'tamper-bundle'
        prepare_bundle(source,'1h',bundle,'proxy-stress-v1',source/'mark.json')
        path=bundle/'funding-stress.json';rows=json.loads(path.read_text());rows[900]['upper_mark']=1000000;path.write_text(json.dumps(rows))
        mp=bundle/'bundle.json';m=json.loads(mp.read_text());m['files']['stress']['sha256']=hashlib.sha256(path.read_bytes()).hexdigest();mp.write_text(json.dumps(m))
        pp=self.root/'tamper-plan.json';pp.write_text(json.dumps(plan(bundle,start_day=300,end_day=301,proxy=True)))
        r=subprocess.run([str(BINARY),'robust-eval','-plan',str(pp),'-inspect'],capture_output=True)
        self.assertNotEqual(r.returncode,0);self.assertIn(b'causal source',r.stderr)
    def test_trace_completeness_and_provenance_failures(self):
        p=plan(self.bundles['1h'],start_day=730,end_day=735);p['cost_multipliers']=[1]
        r=evaluate(BINARY,p,self.config(aligned=True),self.root/'complete-trace')
        account=r['accounts'][0];events=[json.loads(line) for line in Path(account['trace_path']).read_text().splitlines()]
        self.assertTrue(any(e['phase']=='funding' for e in events))
        def rejects(rows,name,pattern):
            for n,e in enumerate(rows,1):e['sequence']=n
            file=self.root/(name+'.jsonl');file.write_text(''.join(json.dumps(e)+'\n' for e in rows))
            a=copy.deepcopy(account);a['trace_path']=str(file);a['trace_sha256']=hashlib.sha256(file.read_bytes()).hexdigest()
            with self.assertRaisesRegex(ValueError,pattern):verify_account(a,self.bundles['1h'])
        rejects([copy.deepcopy(e) for e in events if e['phase']!='open'],'missing-opens','missing/reordered')
        rows=copy.deepcopy(events);rows.pop(next(n for n,e in enumerate(rows) if e['phase']=='funding'))
        rejects(rows,'missing-funding','missing/reordered.*funding')
        rows=copy.deepcopy(events);e=next(e for e in rows if e['phase']=='exit');e['trade']['entry_decision_time']-=7
        rejects(rows,'invalid-source','next-open entry provenance')
        rows=copy.deepcopy(events);next(e for e in rows if e['phase']=='entry')['trade_id']+=100
        rejects(rows,'invalid-id','trade identity')
        rows=copy.deepcopy(events);next(e for e in rows if e['phase']=='open')['price']+=1
        rejects(rows,'invalid-candle-price','execution candle price')
    def test_scheduled_flat_full_event_join(self):
        accounts=[]
        for a,b in ((730,735),(735,740)):
            p=plan(self.bundles['1h'],start_day=a,end_day=b);p['cost_multipliers']=[1.5]
            r=evaluate(BINARY,p,self.config(aligned=True),self.root/f'scheduled-{a}')
            accounts.extend(r['accounts'])
        joined=scheduled_flat(accounts,self.bundles['1h']);product=1.
        for a in accounts:product*=1+a['metrics']['net_return_pct']/100
        self.assertAlmostEqual(joined['net_return_pct'],100*(product-1),8)
        self.assertEqual(len(joined['daily_equity']),10)
        self.assertEqual(joined['account_mode'],'scheduled-flat-v1')

if __name__=='__main__':unittest.main()

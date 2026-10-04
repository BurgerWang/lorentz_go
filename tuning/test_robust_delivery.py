"""P4 isolated Go/report checks and rejection of unapproved real-run contracts."""
from copy import deepcopy
from datetime import datetime,timezone
import hashlib
import json
import math
import os
from pathlib import Path
import random
import shutil
import tempfile
import unittest
from unittest.mock import patch
import robust_pipeline as pipeline
import robust_report as report
from robust_fixture import make_source,plan
from robust_data import prepare_bundle,DAY,iso
from robust_client import evaluate
from robust_budget import Budget
from enhanced_space import go_defaults,identity,canonical,go_validate
from feature_groups import classifier_config

BINARY=Path(os.environ.get('LORENTZ_TEST_BINARY',Path(__file__).resolve().parents[1]/'bin/lorentz-robust-v1')).resolve()

class Delivery(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory(prefix='lorentz-delivery-');cls.root=Path(cls.tmp.name)
        cls.source=make_source(cls.root/'source',800,True);cls.bundle=cls.root/'bundle'
        prepare_bundle(cls.source,'4h',cls.bundle,'proxy-stress-v1',cls.source/'mark.json')
        cls.defaults=go_defaults(BINARY);cls.config=deepcopy(cls.defaults)
        cls.config['classic']['algorithm']='aligned-knn';cls.config['classic']['use_kernel_filter']=False
        cls.config['classic']['use_regime_filter']=False;cls.config['classic']['use_volatility_filter']=False
        cls.config['classifier']=classifier_config(cls.config['classic'],family='aligned-extended')
        pp=plan(cls.bundle,'4h',730,790,True)
        cls.result=evaluate(BINARY,pp,cls.config,cls.root/'traces')
    @classmethod
    def tearDownClass(cls):cls.tmp.cleanup()
    def test_synthetic_go_report_not_live_profit(self):
        r=report.result_report(self.result,self.bundle,'engineering');self.assertEqual(r['evidence_label'],'工程合成验证')
        for a in r['accounts']:
            self.assertEqual(len(a['daily_log_returns']),60)
            self.assertAlmostEqual(a['empty_fraction']+a['time_in_market_fraction'],1)
            self.assertEqual(sum(v['trades'] for v in a['direction'].values()),a['metrics']['trades'])
            self.assertIn('top3_share',a['positive_concentration']);self.assertIn('2022-Q1',a['quarterly'])
    def test_frozen_bootstrap_shared_indices_and_type7(self):
        self.assertEqual(report.type7([0,10],.05),.5)
        paths={'main':[.001,.002,-.001,.003],'same':[.001,.002,-.001,.003],'difference':[0.,0.,0.,0.]}
        u=report.uncertainty(paths,[DAY*i for i in range(1,5)])
        for L in (7,14,28):
            p=u['blocks'][str(L)]['paths'];self.assertEqual(p['main'],p['same']);self.assertEqual(p['difference']['lower_95_one_sided'],0)
        idx=list(report.stationary_indices(4,7));self.assertEqual(len(idx),5000);self.assertEqual(idx,list(report.stationary_indices(4,7)))
        with self.assertRaises(ValueError):report.uncertainty({'x':[0,0]},[DAY,DAY+1])
    def test_dependence_adequacy_blocks_false_pass(self):
        self.assertTrue(report.dependence_warning([0.]*90,[])['warning'])
        self.assertTrue(report.dependence_warning([.01,-.01]*50,[29]*10)['warning'])
        warning=report.dependence_warning([math.sin(i/100) for i in range(400)],[])
        self.assertTrue(warning['three_consecutive_acf_exceedances'])
    def test_paid_flat_with_cash_abstention(self):
        pp=deepcopy(self.result['plan']);cost=1
        a=next(a for a in self.result['accounts'] if a['cost_multiplier']==cost and a['funding_scenario']=='central')
        w=a['window'];cash={'name':'cash','start':w['end'],'end':iso(utc_ms(w['end'])+10*DAY)}
        joined=pipeline.join_flow([{'window':w,'account':a},{'window':cash,'account':None}],self.bundle)
        self.assertEqual(joined['account_mode'],'scheduled-flat-v1');self.assertEqual(len(joined['daily_equity']),70)
        self.assertAlmostEqual(joined['net_return_pct'],a['metrics']['net_return_pct'])
        self.assertTrue(all(d['equity']==joined['daily_equity'][59]['equity'] for d in joined['daily_equity'][60:]))
    def test_fixed_request_persistent_replay_and_budget(self):
        run=self.root/'one-request';c={'engine':{'path':str(BINARY)},'bundle_dir':str(self.bundle)}
        budget=Budget(run/'budget.sqlite',{'synthetic_bundle_identity':identity(json.loads((self.bundle/'bundle.json').read_text()))},
            pipeline.limits([0,2,12]),{'synthetic':pipeline.limits([0,2,12])})
        r=pipeline.fixed_request(run,c,budget,'synthetic','initial',self.result['plan'],self.config)
        with patch.object(pipeline.client,'evaluate',side_effect=AssertionError('must not replay')):
            cached=pipeline.fixed_request(run,c,budget,'synthetic','initial',self.result['plan'],self.config)
        self.assertEqual(r,cached);self.assertEqual(budget.counts()['counts']['go_requests'],1)
    def test_protocol_representatives_cover_and_match_go(self):
        p=pipeline.load_protocol(pipeline.ROOT/'tuning/protocols/robust-v1.json')
        r=pipeline.representatives(p,'2019-11-27T00:00:00Z');self.assertEqual(len(r),12)
        go_validate(BINARY,[x['config'] for x in r])
        self.assertTrue(any(x['config']['classic']['include_full_history'] for x in r))
        self.assertTrue(any(x['config']['classic']['use_dynamic_exits'] for x in r))
        self.assertTrue(any(x['config']['model']['rank_half_life'] for x in r))
        public=pipeline.public_controls(p,'2019-11-27T00:00:00Z');a=deepcopy(public['classic']);a['classic']['algorithm']='aligned-knn';a['classifier']['family']='aligned-extended'
        self.assertEqual(a,public['aligned'])
        entries,old=pipeline.r0_entries(p);self.assertEqual(len(entries),11);self.assertEqual(entries[1]['name'],'Classic121')
        self.assertEqual(old['history_start'],'2023-01-01T00:00:00Z')
        self.assertTrue(all(e['config']['daily']['history_start']=='2020-01-01' for e in entries))
    def test_future_actual_freeze_complete_day_and_old_final_refusal(self):
        p=pipeline.load_protocol(pipeline.ROOT/'tuning/protocols/robust-v1.json');out=self.root/'future-freeze.json'
        f=pipeline.freeze_configuration(self.bundle,BINARY,self.config,p['public_controls'],out,
             _now=datetime(2026,10,2,13,42,tzinfo=timezone.utc))
        self.assertEqual(f['start'],'2026-10-03T00:00:00Z');self.assertEqual(f['end'],'2027-10-03T00:00:00Z')
        self.assertFalse(f['early_profit_confirmation_allowed']);self.assertEqual(f['minimum_trades'],50)
        self.assertEqual(f['daily_history_start'],'2020-01-01T00:00:00Z')
        with self.assertRaises(FileExistsError):pipeline.freeze_configuration(self.bundle,BINARY,self.config,p['public_controls'],out)
        attestation=self.root/'attestation.json';attestation.write_text(canonical({'frozen_sha256':hashlib.sha256(out.read_bytes()).hexdigest(),'authorized_by':'user','data_use':'prospective','no_post_freeze_parameter_changes':True,'no_selection_on_future_segment':True}))
        with self.assertRaisesRegex(ValueError,'endpoint has not elapsed'):pipeline.prepare_future(out,self.bundle,self.root/'future-run',attestation)
    def test_complete_synthetic_prepare_has_concrete_files_and_no_evaluation(self):
        source=make_source(self.root/'full-source',2466,True);bundle=self.root/'full-bundle'
        prepare_bundle(source,'4h',bundle,'proxy-stress-v1',source/'mark.json');run=self.root/'prepared-run'
        with patch.object(pipeline.client,'evaluate',side_effect=AssertionError('prepare must not evaluate')), patch.object(pipeline.client.Server,'evaluate',side_effect=AssertionError('inspect must not request strategy')):
            m=pipeline.prepare_run(pipeline.ROOT/'tuning/protocols/robust-v1.json','4h',bundle,run,inspect=True)
        c=m['contract'];self.assertEqual(len(c['jobs']),26);self.assertEqual(sum(j['ledgers'] for j in c['jobs']),294)
        self.assertEqual(len(c['prepared_files']),83);self.assertTrue(all(Path(p).exists() for p in c['prepared_files']))
        self.assertFalse(json.loads((run/'approval-template.json').read_text())['approved'])
        self.assertTrue(all(x['evaluation_calls']==0 for x in json.loads((run/'inspect.json').read_text())))
        self.assertEqual(pipeline.load_run(run),m)
        self.assertTrue({'feature_groups.py','enhanced.py','enhanced_protocol.py','optimize.py','optimize_enhanced.py'} <= {Path(p).name for p in c['helpers']})
        self.assertEqual(c['runtime_versions'],pipeline.runtime_identity())
        real_version=pipeline.package_version
        with patch.object(pipeline,'package_version',side_effect=lambda name:'changed' if name=='numpy' else real_version(name)):
            with self.assertRaisesRegex(ValueError,'package/runtime versions differ'):pipeline.load_run(run)
        real_hash=pipeline.sha256
        with patch.object(pipeline,'sha256',side_effect=lambda path:'changed' if Path(path).name=='feature_groups.py' else real_hash(path)):
            with self.assertRaisesRegex(ValueError,'source/helper changed'):pipeline.load_run(run)
        with self.assertRaises(ValueError):pipeline.run_fixed(run,'B1',run/'approval-template.json')
        with self.assertRaises(FileExistsError):pipeline.prepare_run(pipeline.ROOT/'tuning/protocols/robust-v1.json','4h',bundle,run,inspect=False)
    def test_future_rejects_self_consistent_replacement_of_frozen_history(self):
        bundle=self.root/'frozen-history';shutil.copytree(self.bundle,bundle)
        p=pipeline.load_protocol(pipeline.ROOT/'tuning/protocols/robust-v1.json');freeze=self.root/'history-freeze.json'
        f=pipeline.freeze_configuration(bundle,BINARY,self.config,p['public_controls'],freeze,
            _now=datetime(2026,10,2,13,42,tzinfo=timezone.utc))
        funding=bundle/'funding.json';rows=json.loads(funding.read_text());rows[12]['rate']=.001234
        funding.write_text(canonical(rows));manifest=json.loads((bundle/'bundle.json').read_text())
        digest=hashlib.sha256(funding.read_bytes()).hexdigest()
        manifest['files']['funding']['sha256']=digest;manifest['sources']['funding']['sha256']=digest
        (bundle/'bundle.json').write_text(canonical(manifest))
        pipeline.validate_bundle(bundle)
        self.assertNotEqual(pipeline.client.bundle_identity(bundle),f['bundle_identity'])
        attestation=self.root/'history-attestation.json';attestation.write_text(canonical({
            'frozen_sha256':hashlib.sha256(freeze.read_bytes()).hexdigest(),'authorized_by':'user',
            'data_use':'prospective','no_post_freeze_parameter_changes':True,'no_selection_on_future_segment':True}))
        class FutureNow(datetime):
            @classmethod
            def now(cls,tz=None):return cls(2028,1,1,tzinfo=timezone.utc)
        with patch.object(pipeline,'datetime',FutureNow),patch.object(pipeline.client,'inspect',side_effect=AssertionError('modified history must reject before inspect')):
            with self.assertRaisesRegex(ValueError,'frozen historical bundle changed'):
                pipeline.prepare_future(freeze,bundle,self.root/'rejected-future',attestation)
        self.assertFalse((self.root/'rejected-future').exists())
    def test_missing_budget_rejects_existing_execution(self):
        run=self.root/'lost-budget';(run/'execution'/'B1').mkdir(parents=True)
        with self.assertRaisesRegex(ValueError,'lost its durable budget'):
            pipeline.batch_budget(run,{},'B1')
    def test_origin_authorization_does_not_run_other_origins(self):
        p=pipeline.load_protocol(pipeline.ROOT/'tuning/protocols/robust-v1.json');c={'authorization_scopes':pipeline.authorization_scopes(p)}
        with patch.object(pipeline,'authorize',return_value={'contract':c}), patch.object(pipeline,'run_search',return_value={'status':'complete'}) as sr, patch.object(pipeline,'run_local') as local, patch.object(pipeline,'run_outer',return_value={'status':'partial'}) as outer:
            run=self.root/'one-origin';r=pipeline.run_origin_batch(run,'W1',self.root/'user-approval.json')
        self.assertEqual(sr.call_args.args[4:6],('W1','W1'));self.assertEqual(local.call_args.args[3:5],('W1','W1'))
        self.assertEqual(outer.call_args.args[2:4],('W1','W1'));self.assertEqual(r['authorization_budget']['go_requests'],4765)
    def test_trace_and_statistics_resource_rejection(self):
        with self.assertRaisesRegex(ValueError,'resource limit'):
            report.uncertainty({'x':[0.]},[DAY]*5001)
        c={'protocol':{'resource_limits':{'max_trace_bytes_per_request':1,'minimum_free_bytes_before_request':1}}}
        with self.assertRaisesRegex(ValueError,'trace storage limit'):
            pipeline.resources_ready(self.root,c,self.result['plan'])
    def test_approval_required_and_exact_manifest_binding(self):
        a={'version':'robust-approval-v1','manifest_sha256':'different','authorized_by':'user','authorization_reference':'synthetic test only',
           'approved':True,'batches':['B1'],'budgets':{'B1':pipeline.limits([0,26,294])},'funding_modes':{'B1':['exact','proxy-stress-v1']}}
        path=self.root/'approval.json';path.write_text(canonical(a));fake=self.root/'approval-run';fake.mkdir(exist_ok=True);(fake/'manifest.json').write_text('{}')
        with patch.object(pipeline,'load_run',return_value={'contract':{'budgets':a['budgets'],'authorization_scopes':{'B1':{'budget':a['budgets']['B1'],'funding_modes':['exact','proxy-stress-v1']}}}}):
            with self.assertRaises(ValueError):pipeline.authorize(fake,'B1',path)
            a['manifest_sha256']=hashlib.sha256((fake/'manifest.json').read_bytes()).hexdigest();path.write_text(canonical(a))
            self.assertEqual(pipeline.authorize(fake,'B1',path)['contract']['budgets'],a['budgets'])
            with self.assertRaises(ValueError):pipeline.authorize(fake,'R0',path)
            a['approved']=False;path.write_text(canonical(a))
            with self.assertRaises(ValueError):pipeline.authorize(fake,'B1',path)

from robust_data import utc_ms
if __name__=='__main__':unittest.main()

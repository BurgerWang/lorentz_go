"""P0 wrapper checks. All evaluations use isolated generated candles in /tmp."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import enhanced_space as space
from feature_groups import classifier_config
import mechanism_v2 as v2
import mechanism_metrics as metrics
from robust_budget import Budget, BudgetHalted, atomic_json
from robust_data import prepare_bundle
from robust_fixture import make_source
import robust_client as client

BINARY = v2.ROOT/'bin/lorentz-robust-v1'


class Rules(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.defaults = space.go_defaults(BINARY)

    def test_matrix_factors_context_reactivation_public_original(self):
        d = self.defaults
        a = deepcopy(d); a['entry_mode']='both'; a['envelope_enabled']=True; a['pullback']['enabled']=True
        a['envelope'].update(h=12,r=6,x=10,atr_length=30,near=2,far=5)
        a['pullback']['max_wait_bars']=48
        a['classifier']=classifier_config(a['classic'],'classic-kernel-deviation',family='classic-extended')
        a=space.normalize_config(a,d)
        b=deepcopy(d); b['exit']={'policy':'four-bars','max_hold_bars':48}
        b['classifier']=classifier_config(b['classic'],'classic-d1-slope',family='classic-extended')
        b['daily'].update(h=16,r=5,x=30); b=space.normalize_config(b,d)
        aligned=deepcopy(d); aligned['classic']['algorithm']='aligned-knn'; aligned['classifier']['family']='aligned-extended'
        sources={'anchors':{'A':{'config':a},'B':{'config':b}},'public_controls':{'classic':{'config':d},'aligned':{'config':aligned}}}
        jobs=v2.matrix(sources,d)
        self.assertEqual(len(jobs),13); self.assertEqual(jobs[1]['config'],a)
        for j in jobs[:4]:
            self.assertEqual(j['config']['envelope']['h'],12)
            self.assertEqual(j['config']['envelope']['atr_length'],30)
        self.assertEqual(jobs[0]['config']['envelope']['near'],d['envelope']['near'])
        self.assertEqual(jobs[1]['config']['envelope']['near'],2)
        for j in jobs[4:8]: self.assertEqual(j['config']['daily'],b['daily'])
        self.assertEqual(jobs[5]['config']['envelope'],d['envelope'])
        self.assertEqual(jobs[9]['config'],d)
        self.assertEqual(jobs[10]['config'],aligned)
        self.assertEqual(jobs[-2]['replay_of'],'A-B-S')
        self.assertEqual(jobs[-1]['replay_of'],'B-original')
        space.go_validate(BINARY,[j['config'] for j in jobs])

    def test_local_single_anchor_small_grid_and_conditional_k(self):
        d=self.defaults; c=space.normalize_config(d,d)
        rules=v2.freeze_local_rules(c,d,('neighbors',))
        self.assertEqual(rules['keys']['neighbors']['domain'],[7,8,9])
        self.assertEqual(rules['keys']['kernel_h']['domain'],[8])
        self.assertFalse(rules['keys']['kernel_h']['active'])
        self.assertEqual(v2.legal_neighbors([61,62,63],500,8),[61,62])
        for key in ('sample_stride','max_bars_back','feature_count'):
            with self.assertRaises(ValueError):v2.freeze_local_rules(c,d,(key,))
        c['classifier']=classifier_config(c['classic'],'classic-d1-slope',family='classic-extended')
        with self.assertRaises(ValueError):v2.freeze_local_rules(c,d,('daily_h',))
        c=deepcopy(d); c['classifier']['feature_group']['features'][1]=deepcopy(c['classifier']['feature_group']['features'][0])
        with self.assertRaises(ValueError):v2.unique_features(c)
        c=deepcopy(d); c['classifier']['feature_group']['features']=[{'name':'RSI','a':14,'b':1,'normalization':'legacy','window':0}, {'name':'RSI','a':9,'b':1,'normalization':'legacy','window':0}]
        v2.unique_features(c) # Same name and different complete specifications are legal.

    def test_unapproved_and_mismatched_approval_cannot_start(self):
        with tempfile.TemporaryDirectory() as td:
            run=Path(td); atomic_json(run/'manifest.json',{})
            approval={'version':'mechanism-v2-approval-v1','manifest_sha256':v2.sha256(run/'manifest.json'),
                      'approved':False,'authorized_by':'user','authorization_reference':'synthetic only','batch':'P1',
                      'budget':v2.LIMITS,'funding_modes':['proxy-stress-v1']}
            atomic_json(run/'approval.json',approval)
            with patch.object(v2,'load_run',return_value={'contract':{}}),patch.object(client,'Evaluator',side_effect=AssertionError('not authorized')):
                with self.assertRaises(ValueError):v2.run(run,run/'approval.json')
                approval['approved']=True; approval['budget']={**v2.LIMITS,'go_requests':14}; atomic_json(run/'approval.json',approval)
                with self.assertRaises(ValueError):v2.run(run,run/'approval.json')
            self.assertFalse((run/'execution').exists())

    def test_paired_gate_keeps_factors_and_strict_original_improvement(self):
        from test_mechanism_metrics import scoring_summaries
        reports=[]
        names=[s+'-'+x for s in ('A','B') for x in v2.SUFFIXES]+['B-original']
        for name in names:
            rows=scoring_summaries()
            # A-M-H4 improves every half/total but has worse DD, so cannot advance.
            # B-B-S improves with equal DD and may advance; Public is not eligible.
            if name in ('A-M-H4','B-B-S'):
                for a in rows:
                    for h in a['half_years']:h['net_return_pct']=4
                    a['metrics']['net_return_pct']=100*(1.04**6-1)
                    if name=='A-M-H4':a['metrics']['max_drawdown_pct']=20.1 if a['cost_multiplier']!=1 else 20
            reports.append({'name':name,'accounts':rows,'v2_development_score':metrics.continuous_score(rows)})
        sources={'anchors':{s:{'old_fold_diagnostic':[], 'source_key':['synthetic',s]} for s in ('A','B')}}
        jobs=[{'name':n,'config':self.defaults} for n in names]
        with patch.object(v2,'matrix',return_value=jobs):
            result=v2.paired_report(reports,sources,self.defaults)
            self.assertTrue(result['diagnostic_complete'])
            self.assertEqual(result['selected_research_objects'],[{'skeleton':'A','name':'A-B-S'},{'skeleton':'B','name':'B-B-S'}])
            a=next(x for x in result['original_reference_eligibility'] if x['name']=='A-M-H4')
            self.assertFalse(a['eligible']);self.assertFalse(a['improvement_checks'][0]['full_dd_not_worse'])
            self.assertFalse(result['P3_P4_authorized'])
            self.assertEqual(result['adjacent_pairs'][1]['entry_signals']['accounts'][0]['half_return_pp'],[1]*6)
            self.assertEqual(result['interactions'][1]['accounts'][0]['half_return_difference_pp'],[-1]*6)
            partial=v2.paired_report(reports[:-2],sources,self.defaults)
            self.assertFalse(partial['diagnostic_complete'])

    def test_persistent_halt_blocks_terminal_cache_and_run_recovery(self):
        with tempfile.TemporaryDirectory(prefix='lorentz-mechanism-halt-') as td:
            run=Path(td);execution=run/'execution';execution.mkdir()
            c={'engine':{'path':str(BINARY)},'jobs':[]}
            b=Budget(execution/'budget.sqlite',{'manifest_contract_sha256':space.identity(c)},v2.LIMITS,{'P1':v2.LIMITS})
            b.reserve_evaluation('P1','P1/complete','complete-key',6,fixed=True)
            b.finish('P1/complete',response={'synthetic':True})
            b.reserve_evaluation('P1','P1/last','fault-key',6,fixed=True)
            b.finish('P1/last',error='synthetic protocol fault')
            b.halt('identity/protocol fault on final request')
            atomic_json(execution/'summary.json',{'status':'in-progress'})
            before=(execution/'summary.json').read_bytes()
            for name in ('complete','last'):
                with self.assertRaises(BudgetHalted):v2.request(run,c,b,{'name':name},None)
            with patch.object(v2,'authorize',return_value={'contract':c}),patch.object(client,'Evaluator',side_effect=AssertionError('halted batch must not start engine')):
                with self.assertRaises(BudgetHalted):v2.run(run,run/'synthetic-approval.json')
            self.assertEqual((execution/'summary.json').read_bytes(),before)
            self.assertEqual(b.counts()['counts']['go_requests'],2)
            self.assertIn('protocol',b.halted())


class SyntheticWrapper(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory(prefix='lorentz-mechanism-wrapper-'); cls.root=Path(cls.tmp.name)
        source=make_source(cls.root/'source',days=1462,proxy=True); cls.bundle=cls.root/'bundle'
        prepare_bundle(source,'1h',cls.bundle,'proxy-stress-v1',source/'mark.json')
        cls.pp=v2.account_plan(cls.bundle)
        cls.cfg=space.go_defaults(BINARY)
        cls.cfg['classic'].update(max_bars_back=500,neighbors=4,use_kernel_filter=False,use_regime_filter=False,use_volatility_filter=False)
        cls.cfg['classifier']=classifier_config(cls.cfg['classic'],'classic',family='classic-extended')
        cls.cfg=space.normalize_config(cls.cfg,space.go_defaults(BINARY))
        cls.c={'engine':{'path':str(BINARY)},'bundle_dir':str(cls.bundle),'bundle_identity':client.bundle_identity(cls.bundle),
               'plan':cls.pp,'resource_limits':v2.RESOURCE}
    @classmethod
    def tearDownClass(cls):cls.tmp.cleanup()

    def budget(self,name):
        run=self.root/name; run.mkdir()
        return run,Budget(run/'budget.sqlite',{'synthetic':True},v2.LIMITS,{'P1':v2.LIMITS})

    def job(self,name,role='matrix'):
        return {'name':name,'role':role,'config':self.cfg}

    def test_actual_go_cache_replay_and_real_trace_mtm_summary(self):
        run,b=self.budget('success')
        with client.Evaluator(BINARY) as ev:
            first=v2.request(run,self.c,b,self.job('first'),ev)
            with patch.object(client.Evaluator,'__call__',side_effect=AssertionError('cache must not evaluate')):
                again=v2.request(run,self.c,b,self.job('first'),ev)
                alias=v2.request(run,self.c,b,self.job('alias'),ev)
            self.assertEqual(first,again); self.assertEqual(first,alias)
            replay=v2.request(run,self.c,b,self.job('replay','independent-replay'),ev)
        self.assertEqual(v2.economic_response(first),v2.economic_response(replay))
        self.assertEqual(b.counts()['counts'],{'trial_attempts':0,'go_requests':2,'ledger_evaluations':12})
        summaries=[metrics.summarize_continuous_account(a,bundle=self.bundle) for a in first['accounts']]
        self.assertEqual(len(summaries),6)
        self.assertEqual(metrics.continuous_score(summaries)['score_contract'],'continuous-mtm-six-vector-v2')
        for a in summaries:
            self.assertEqual(sum(h['trades'] for h in a['half_years']),a['metrics']['trades'])
        # A cached account with missing trace bytes is refused, with no resend.
        Path(first['accounts'][0]['trace_path']).unlink()
        with patch.object(client.Evaluator,'__call__',side_effect=AssertionError('must not repair by market replay')):
            with self.assertRaises(ValueError):v2.request(run,self.c,b,self.job('first'),None)

    def test_unknown_request_recovery_never_retries_and_budget_stays_charged(self):
        run,b=self.budget('unknown')
        b.reserve_evaluation('P1','P1/unknown','unknown-key',6,fixed=True)
        self.assertEqual(b.recover('P1'),['P1/unknown'])
        b2=Budget(run/'budget.sqlite',{'synthetic':True},v2.LIMITS,{'P1':v2.LIMITS})
        self.assertIsNone(v2.request(run,self.c,b2,self.job('unknown'),lambda *a: self.fail('must not send unknown')))
        self.assertEqual(b2.counts()['counts']['go_requests'],1)
        self.assertEqual(b2.record('P1/unknown')['status'],'interrupted')

    def test_ordinary_failure_retained_protocol_fault_halts(self):
        run,b=self.budget('failure')
        def ordinary(*a):raise client.EvaluationError('synthetic equity protection')
        self.assertIsNone(v2.request(run,self.c,b,self.job('equity'),ordinary))
        self.assertIsNone(v2.request(run,self.c,b,self.job('equity'),lambda *a:self.fail('failure resend')))
        self.assertIsNone(b.halted())
        def unknown(*a):raise client.ProtocolError('synthetic timeout; outcome unknown')
        with self.assertRaises(client.ProtocolError):v2.request(run,self.c,b,self.job('timeout'),unknown)
        self.assertTrue(b.halted()); self.assertEqual(b.counts()['counts']['go_requests'],2)

    def test_real_go_aligned_conditional_initialization_inspect(self):
        c=deepcopy(self.cfg); c['classic'].update(algorithm='aligned-knn',sample_stride=8,neighbors=62)
        c['classifier']=classifier_config(c['classic'],'classic',family='aligned-extended')
        self.assertEqual(client.inspect(BINARY,self.pp,c)['evaluation_calls'],0)
        c['classic']['neighbors']=63
        with self.assertRaises(client.ProtocolError):client.inspect(BINARY,self.pp,c)


if __name__=='__main__':unittest.main()

"""P4 finite-domain/approval/accounting tests; market consumption remains zero."""
from copy import deepcopy
from itertools import product
from pathlib import Path
import json
import math
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import enhanced_space as space
from feature_groups import classifier_config
import mechanism_p4 as p4
import mechanism_p4_domain as domain
import mechanism_p4_search as search
import mechanism_p3_client as client
import mechanism_v2 as v2
from robust_budget import Budget, BudgetHalted, atomic_json
from robust_data import prepare_bundle
from robust_fixture import make_source
from test_mechanism_metrics import scoring_summaries


class Contract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = p4.read_source(p4.SOURCE)
        report = v2.load(p4.SOURCE/'execution/summary.json')
        cls.full_scores = {k:next(x['v2_development_score'] for x in report['reports'] if x['name']==k+'-classic-original') for k in ('A','B')}

    def test_mirrors_stage_freeze_context_and_domain_rejections(self):
        for k in ('A','B'):
            anchor = self.source['anchors'][k]['config']; r = domain.freeze(anchor,k)
            params = domain.parameters(anchor,r,'classifier')
            f = r['blocks']['classifier']['keys'][1]; params[f] += 1
            selected = domain.compile_config(anchor,r,'classifier',params)
            self.assertEqual(selected['strategy']['classic']['features'][0]['a'],selected['strategy']['classifier']['feature_group']['features'][0]['a'])
            self.assertEqual(selected['strategy']['daily'],anchor['strategy']['daily'])
            management = domain.parameters(selected,r,'management')
            key = r['blocks']['management']['keys'][0]; management[key] = r['blocks']['management']['domains'][key][0]
            changed = domain.compile_config(selected,r,'management',management)
            self.assertEqual(changed['strategy']['classifier'],selected['strategy']['classifier'])
            for mutation in ('mirror','D1','memory','switch','recent','widen','bool'):
                bad = deepcopy(changed)
                if mutation=='mirror': bad['strategy']['classic']['features'][0]['a'] += 1
                elif mutation=='D1': bad['strategy']['daily']['x'] += 1
                elif mutation=='memory': bad['strategy']['classic']['max_bars_back']=2000
                elif mutation=='switch': bad['strategy']['risk']['enabled']=not bad['strategy']['risk']['enabled']
                elif mutation=='recent': bad['variant']='classic-recent'
                elif mutation=='widen': bad['strategy']['classic']['neighbors'] += 100
                else: bad['strategy']['classic']['neighbors']=True
                with self.assertRaises((ValueError,client.ProtocolError),msg=mutation): domain.validate_config(bad,r)
            bad_rules = deepcopy(r); bad_rules['blocks']['classifier']['domains']['neighbors'].append(100)
            with self.assertRaises(ValueError): domain.validate_config(anchor,bad_rules)

    def test_every_corner_has_24_adjacent_legal_unique_neighbors_covering_all_keys(self):
        for k in ('A','B'):
            anchor = self.source['anchors'][k]['config']; rules = domain.freeze(anchor,k)
            keys = [key for b in domain.BLOCKS for key in rules['blocks'][b]['keys']]
            for edges in product((0,-1),repeat=8):
                config = deepcopy(anchor)
                for b, subset in zip(domain.BLOCKS,(edges[:4],edges[4:])):
                    config = domain.compile_config(config,rules,b,{key:rules['blocks'][b]['domains'][key][i]
                             for key,i in zip(rules['blocks'][b]['keys'],subset)})
                frozen = domain.neighborhood(config,rules)
                self.assertTrue(frozen['ready']); self.assertEqual(len(frozen['points']),24)
                self.assertEqual(len({x['config_identity'] for x in frozen['points']}),24)
                self.assertEqual({key for x in frozen['points'] for key in x['changed_keys']},set(keys))
                for point in frozen['points']:
                    domain.validate_config(point['config'],rules)
                    for block in domain.BLOCKS:
                        spec = rules['blocks'][block]
                        a,b = domain.parameters(config,rules,block),domain.parameters(point['config'],rules,block)
                        for key in spec['keys']:
                            self.assertLessEqual(abs(spec['domains'][key].index(a[key])-spec['domains'][key].index(b[key])),1)
            self.assertEqual(len(domain.inspect_configs(rules)),19)

    def test_two_account_score_matches_full_score_six_vector_and_zero_is_strict(self):
        for k in ('A','B'):
            score = self.source['anchors'][k]['score']
            self.assertTrue(score['feasible']); self.assertEqual(score['constraint_vector'],[0]*6)
            self.assertEqual(score['constraint_vector'],self.full_scores[k]['constraint_vector'])
            self.assertEqual(score['values'],self.full_scores[k]['values'])
            self.assertEqual(set(score['violations']),set(score['constraint_names']))
        rows = [x for x in scoring_summaries() if x['cost_multiplier']==1.5]
        for row in rows:
            for h in row['half_years']:h['net_return_pct']=0
            row['metrics']['net_return_pct']=0
        zero = domain.score_pair(rows)
        self.assertFalse(zero['feasible']); self.assertEqual(zero['constraint_vector'][-1],1)
        for row in rows:
            for h in row['half_years']:h['net_return_pct']=.001
            row['metrics']['net_return_pct']=100*(math.prod(1+h['net_return_pct']/100 for h in row['half_years'])-1)
        self.assertEqual(domain.score_pair(rows)['constraint_vector'][-1],0)
        with self.assertRaises(client.ProtocolError): domain.score_pair(rows+[deepcopy(rows[0])])
        rows[1]['cost_multiplier']=2
        with self.assertRaises(client.ProtocolError): domain.score_pair(rows)

    def test_neighbor_failure_and_unchanged_path_stay_in_denominator(self):
        anchor = self.source['anchors']['A']; rules = domain.freeze(anchor['config'],'A')
        frozen = domain.neighborhood(anchor['config'],rules)
        def outcomes(failures):
            return [{'name':p['name'],'config_identity':p['config_identity'],
                     'analysis':None if i<failures else anchor['analysis'],'trading_path_changed':False}
                    for i,p in enumerate(frozen['points'])]
        four = domain.neighborhood_check(anchor,frozen,outcomes(4))
        self.assertTrue(four['passed']); self.assertEqual(four['denominator'],24)
        self.assertEqual(four['trading_path_changed'],0)
        five = domain.neighborhood_check(anchor,frozen,outcomes(5))
        self.assertFalse(five['passed']); self.assertEqual(five['feasible_neighbors'],19)
        with self.assertRaises(client.ProtocolError): domain.neighborhood_check(anchor,frozen,outcomes(4)[:-1])

    def test_budget_exact_and_approval_before_any_execution(self):
        self.assertEqual({k:sum(row[k] for row in p4.study_limits().values()) for k in p4.LIMITS},p4.LIMITS)
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);atomic_json(root/'manifest.json',{})
            approval={'version':'mechanism-p4-approval-v1','manifest_sha256':p4.sha256(root/'manifest.json'),
                      'approved':False,'authorized_by':'user','authorization_reference':'isolated synthetic test',
                      'batch':'P4','budget':p4.LIMITS,'funding_modes':['proxy-stress-v1']}
            atomic_json(root/'approval.json',approval)
            with patch.object(p4,'load_run',return_value={'contract':{}}),patch.object(client,'Evaluator',side_effect=AssertionError('must not start')):
                with self.assertRaises(ValueError):p4.run(root,root/'approval.json')
                approval['approved']=True;atomic_json(root/'approval.json',approval)
                atomic_json(root/'engineering-audit.json',{'audit_status':'pending','findings':[]})
                with self.assertRaises(ValueError):p4.run(root,root/'approval.json')
            self.assertFalse((root/'execution').exists())

    def test_fixed_unknown_cache_fault_and_live_identity_halt(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);limits={'trial_attempts':0,'go_requests':3,'ledger_evaluations':6}
            c={'substudy_limits':{'fixed':limits},'resource_limits':v2.RESOURCE,'plan':{'cost_multipliers':[1.5]}}
            b=Budget(root/'budget.sqlite',c,limits,c['substudy_limits']);cfg=self.source['anchors']['A']['config'];pp=c['plan']
            calls=[]
            def evaluate(p,c,t):calls.append(t);return {'marker':'valid'}
            with patch.object(p4,'load_run'),patch.object(v2,'resources_ready'),patch.object(p4,'analyze',return_value={'score':{'feasible':True}}):
                p4.fixed_request(root,c,b,'fixed','first',pp,cfg,evaluate)
                p4.fixed_request(root,c,b,'fixed','first',pp,cfg,evaluate)
            self.assertEqual(len(calls),1)
            with patch.object(p4,'load_run'),patch.object(p4,'analyze',side_effect=client.ProtocolError('cached trace changed')):
                with self.assertRaises(client.ProtocolError):p4.fixed_request(root,c,b,'fixed','first',pp,cfg,evaluate)
            self.assertEqual(b.record('first')['status'],'complete');self.assertIsNotNone(b.halted())
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);b=Budget(root/'budget.sqlite',c,limits,c['substudy_limits'])
            key=space.identity({'binding_identity':space.identity(c),'plan':pp,'config':cfg})
            b.reserve_evaluation('fixed','unknown',key,2,fixed=True);b.recover('fixed')
            with patch.object(p4,'load_run'),patch.object(p4,'analyze',side_effect=AssertionError('no retry')):
                result=p4.fixed_request(root,c,b,'fixed','unknown',pp,cfg,evaluate)
                self.assertEqual(result['status'],'interrupted')
                self.assertEqual(p4.fixed_request(root,c,b,'fixed','alias',pp,cfg,evaluate)['status'],'skipped-previous-failure')
            self.assertEqual(b.counts()['counts']['go_requests'],1)
            with patch.object(p4,'load_run',side_effect=ValueError('frozen source changed')):
                with self.assertRaises(ValueError):p4.fixed_request(root,c,b,'fixed','fresh',pp,cfg,evaluate)
            self.assertIsNotNone(b.halted())

    def test_complete_post_search_neighbors_pressure_replays_resume_and_closed_idempotence(self):
        with tempfile.TemporaryDirectory(prefix='lorentz-p4-post-synthetic-') as td:
            root=Path(td);anchor=deepcopy(self.source['anchors']['A'])
            rules=domain.freeze(anchor['config'],'A');plans=p4.plans(self.source)
            c={'engine':{'path':'/tmp/no-go-used-by-this-fake-test'},'domains':{'A':rules},
               'anchors':{'A':anchor},'plans':plans,'plan':plans['full'],'seeds':list(p4.SEEDS),
               'substudy_limits':p4.study_limits(),'resource_limits':v2.RESOURCE}
            result={'status':'complete','branches':[{'skeleton':'A','seed':17,'selected':anchor}]}
            calls=[]
            def evaluate(plan,config,trace):
                calls.append((deepcopy(plan),deepcopy(config)))
                return {'config':config,'plan':plan,'accounts':[],
                        'mechanism_trace':{'path':str(trace),'sha256':'unchanged-state'}}
            def analyze(c,response,plan=None,config=None):
                if response['plan']['cost_multipliers']==[1.5]:return deepcopy(anchor['analysis'])
                full=scoring_summaries()
                return {'accounts':full,'score':p4.continuous_score(full),'trading_path_identity':'full-cost-path'}
            with patch.object(p4,'authorize',return_value={'contract':c}),patch.object(p4,'load_run'),\
                 patch.object(search,'run_search',return_value=result),patch.object(v2,'resources_ready'),\
                 patch.object(p4,'analyze',side_effect=analyze),patch.object(client,'evaluate',side_effect=lambda binary,p,c,t:evaluate(p,c,t)),\
                 patch.object(client,'Evaluator') as factory:
                factory.return_value.__enter__.return_value=evaluate
                first=p4.run(root,root/'synthetic-approval.json')
                self.assertEqual(first['status'],'complete')
                summary=v2.load(root/'execution/summary.json')
                self.assertEqual(len(summary['centrals'][0]['neighborhood_outcomes']),24)
                self.assertEqual({x['name'] for x in summary['centrals'][0]['neighborhood_outcomes']},{'n%02d'%i for i in range(1,25)})
                self.assertTrue(all(x['request_token'].endswith('/'+x['name']) for x in summary['centrals'][0]['neighborhood_outcomes']))
                self.assertTrue(summary['centrals'][0]['neighborhood_check']['passed'])
                self.assertEqual(summary['centrals'][0]['neighborhood_check']['trading_path_changed'],0)
                self.assertTrue(all(x['matched_full_economics_and_state_trace'] for x in summary['checks']))
                self.assertEqual(len(summary['selected_research_objects']),1)
                self.assertEqual(summary['budget']['counts'],{'trial_attempts':0,'go_requests':27,'ledger_evaluations':66})
                self.assertEqual(len(calls),27)
                p4.run(root,root/'synthetic-approval.json')
                self.assertEqual(len(calls),27)
                self.assertEqual(v2.load(root/'execution/summary.json'),summary)
                for terminal in ('complete','complete-with-failures'):
                    with self.subTest(terminal=terminal):
                        summary['status']=terminal
                        summary['independent_result_audit']='closed';summary['audit_closure']={'sentinel':'must preserve'}
                        atomic_json(root/'execution/summary.json',summary)
                        before=(root/'execution/summary.json').read_bytes()
                        with patch.object(client,'Evaluator',side_effect=AssertionError('closed batch must not load evaluator')):
                            self.assertEqual(p4.run(root,root/'synthetic-approval.json')['status'],'already-complete')
                        self.assertEqual(before,(root/'execution/summary.json').read_bytes())
                        self.assertEqual(len(calls),27)
                (root/'execution/summary.json').write_text('{"broken":')
                with patch.object(client,'Evaluator',side_effect=AssertionError('damaged summary must stop before evaluator')):
                    with self.assertRaises(ValueError):p4.run(root,root/'synthetic-approval.json')
                budget=Budget(root/'execution/budget.sqlite',{'manifest_contract_sha256':space.identity(c)},p4.LIMITS,c['substudy_limits'])
                self.assertIsNotNone(budget.halted())
                self.assertEqual(budget.counts()['counts']['go_requests'],27)

    def test_replay_mismatch_fails_reserved_request_before_cache_and_halts(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);limits={'trial_attempts':0,'go_requests':1,'ledger_evaluations':6}
            c={'substudy_limits':{'replay':limits},'resource_limits':v2.RESOURCE,'engine':{'path':'/tmp/unused-fake-replay'}}
            b=Budget(root/'budget.sqlite',c,limits,c['substudy_limits'])
            plan={'cost_multipliers':[1,1.5,2]};config=self.source['anchors']['A']['config']
            old={'accounts':[],'config':config,'plan':plan,'marker':'original'}
            changed=dict(old,marker='changed')
            with patch.object(p4,'load_run'),patch.object(p4,'analyze',return_value={}),\
                 patch.object(v2,'resources_ready'),patch.object(client,'evaluate',return_value=changed):
                with self.assertRaisesRegex(client.ProtocolError,'replay differs'):
                    p4.fixed_request(root,c,b,'replay','replay/token',plan,config,None,replay=True,replay_original=old)
            self.assertEqual(b.record('replay/token')['status'],'failed')
            self.assertEqual(b.snapshot(False)['cache'],{})
            self.assertIsNotNone(b.halted())

    def test_real_frozen_domains_integrate_native_search_with_fake_evaluator(self):
        import optuna
        optuna.logging.set_verbosity(optuna.logging.ERROR)
        with tempfile.TemporaryDirectory(prefix='lorentz-p4-domain-native-') as td:
            root=Path(td);quota=2;binding='isolated-domain-native-no-market'
            rules={k:domain.freeze(self.source['anchors'][k]['config'],k) for k in ('A','B')}
            contract={'binding_identity':binding,'plan':p4.plans(self.source)['search'],
                      'domains':rules,'anchors':deepcopy(self.source['anchors']),
                      'seeds':[17],'startup_trials':16}
            limits={'trial_attempts':quota,'go_requests':quota,'ledger_evaluations':quota*2}
            substudy={f'P4/{k}/17/{block}':deepcopy(limits) for k in rules for block in domain.BLOCKS}
            total={key:sum(row[key] for row in substudy.values()) for key in limits}
            budget=Budget(root/'budget.sqlite',{'manifest_contract_sha256':binding},total,substudy)
            calls=[]
            def evaluate(plan,config,trace):
                calls.append(deepcopy(config))
                return {'plan':client.base.normalize_plan(plan),'config':config,'synthetic':True}
            def analyze(response):
                for k,r in rules.items():
                    try:domain.validate_config(response['config'],r)
                    except ValueError:continue
                    return deepcopy(self.source['anchors'][k]['analysis'])
                raise AssertionError('complete native configuration escaped both frozen domains')
            result=search.run_search(root/'execution',contract,budget,evaluate,analyze,quota=quota)
            self.assertEqual(result['status'],'complete')
            self.assertEqual(budget.counts()['counts']['trial_attempts'],8)
            self.assertEqual(budget.counts()['counts']['go_requests'],len(calls))
            self.assertEqual(budget.counts()['counts']['ledger_evaluations'],2*len(calls))
            for branch in result['branches']:
                k=branch['skeleton']
                self.assertEqual(branch['selected']['config'],self.source['anchors'][k]['config'])
                self.assertEqual(branch['selected']['source']['kind'],'prior-anchor')
                for block in domain.BLOCKS:
                    folder=root/'execution/search'/k/'17'/block
                    storage=optuna.storages.JournalStorage(optuna.storages.journal.JournalFileBackend(str(folder/'study.journal')))
                    study=optuna.load_study(study_name=f'P4/{k}/17/{block}',storage=storage)
                    self.assertEqual(len(study.trials),quota)
                    for trial in study.trials:
                        self.assertEqual(trial.state.name,'COMPLETE')
                        self.assertEqual(trial.constraints,dict(zip(branch['selected']['score']['constraint_names'],[0]*6)))
                        domain.validate_config(trial.user_attrs['config'],rules[k])
            count=len(calls)
            search.run_search(root/'execution',contract,budget,evaluate,analyze,quota=quota)
            self.assertEqual(len(calls),count)


class SyntheticIntegration(unittest.TestCase):
    def test_actual_go_pair_and_full_cost_cash_trace_and_score_adaptation(self):
        # Exactly two real Go requests on disposable synthetic prices: 2+6 ledgers.
        # Prior-market data are never passed to the evaluator.
        with tempfile.TemporaryDirectory(prefix='lorentz-p4-synthetic-go-') as td:
            root=Path(td);source=make_source(root/'source',days=1462,proxy=True)
            bundle=root/'bundle';prepare_bundle(source,'1h',bundle,'proxy-stress-v1',source/'mark.json')
            full=v2.account_plan(bundle);pair=deepcopy(full);pair['cost_multipliers']=[1.5]
            cfg=space.go_defaults(ROOT/'bin/lorentz-robust-v1')
            cfg['classic'].update(max_bars_back=80,neighbors=4,use_volatility_filter=False,use_regime_filter=False,use_kernel_filter=False)
            cfg['classifier']=classifier_config(cfg['classic'],'classic',family='classic-extended')
            cfg=space.normalize_config(cfg,space.go_defaults(ROOT/'bin/lorentz-robust-v1'))
            wrapper=client.wrapper(cfg,'classic-original')
            c={'bundle_dir':str(bundle),'bundle_identity':client.base.bundle_identity(bundle),'resource_limits':v2.RESOURCE}
            with client.Evaluator(ROOT/'bin/lorentz-mechanism-p3') as evaluator:
                rp=evaluator(pair,wrapper,root/'pair');ap=p4.analyze(c,rp,pair,wrapper)
                rf=evaluator(full,wrapper,root/'full');af=p4.analyze(c,rf,full,wrapper)
            self.assertEqual(rp['ledger_evaluations'],2);self.assertEqual(rf['ledger_evaluations'],6)
            self.assertEqual(ap['score']['constraint_vector'],af['score']['constraint_vector'])
            self.assertEqual(ap['score']['values'],af['score']['values'])
            for row in ap['accounts']:
                original=next(a for a in af['accounts'] if a['cost_multiplier']==1.5 and a['funding_scenario']==row['funding_scenario'])
                self.assertEqual(row,original)
            damaged=deepcopy(rp);value=damaged['accounts'][0]['metrics']['net_return_pct']
            damaged['accounts'][0]['metrics']['net_return_pct']+=max(1,abs(value)*.01)
            with self.assertRaises((ValueError,client.ProtocolError)):p4.analyze(c,damaged,pair,wrapper)


ROOT=v2.ROOT
if __name__=='__main__':unittest.main()

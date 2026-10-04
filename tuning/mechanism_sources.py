"""Read-only W1 source extraction for P0; no old-state writes.

Run with repository .venv/bin/python and PYTHONDONTWRITEBYTECODE=1.
This does not construct Optuna storage, Budget, an evaluator, or a Go process.
Journal subset matches opened Optuna 5.0.0 local primary source. Unsupported
operations, malformed/partial logs, source/config/score/cache mismatches fail.
"""
import sys
if not __debug__:
    raise RuntimeError("source validation requires unoptimized Python")
import collections
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import sqlite3

ROOT = Path(__file__).resolve().parents[1]
import enhanced_space as space
from enhanced import strict_loads
import robust_search as search
import robust_client as client
from robust_budget import cache_identity


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        while chunk := f.read(1048576):
            h.update(chunk)
    return h.hexdigest()


def load(path):
    return strict_loads(Path(path).read_text())


def external(value, distribution):
    d = strict_loads(distribution) if isinstance(distribution, str) else distribution
    name, a = d['name'], d['attributes']
    if name == 'CategoricalDistribution':
        assert type(value) in (int, float) and value == int(value)
        return a['choices'][int(value)]
    if name == 'IntDistribution':
        assert type(value) in (int, float) and value == int(value)
        return int(value)
    assert name == 'FloatDistribution'
    return float(value)


def read_journal(path):
    studies = []
    trials = []
    with path.open('rb') as stream:
        for line_number, raw in enumerate(stream, 1):
            assert raw.endswith(b'\n'), (path, line_number, 'partial journal')
            e = strict_loads(raw)
            op = e['op_code']
            assert op in (0, 2, 4, 5, 6, 8, 9), (path, line_number, op)
            if op == 0:
                assert not studies and e['directions'] == [2, 2]
                studies.append({'name': e['study_name'], 'attrs': {}})
            elif op == 2:
                assert e['study_id'] == 0
                studies[0]['attrs'].update(e['user_attr'])
            elif op == 4:
                assert len(studies) == 1 and e['study_id'] == 0
                params = {k: external(v, e['distributions'][k]) for k, v in e.get('params', {}).items()}
                trials.append({'state': e.get('state', 0), 'params': params,
                    'attrs': e.get('user_attrs', {}), 'system': e.get('system_attrs', {}),
                    'values': e.get('values'), 'created_line': line_number})
            else:
                t = trials[e['trial_id']]
                assert t['state'] in (0, 4), (path, line_number, 'update to finished trial')
                if op == 5:
                    t['params'][e['param_name']] = external(e['param_value_internal'], e['distribution'])
                elif op == 6:
                    assert e['state'] in (0, 1, 3)
                    t['state'] = e['state']
                    if e['values'] is not None:
                        t['values'] = e['values']
                    if e['state'] in (1, 3):
                        t['finished_line'] = line_number
                elif op == 8:
                    t['attrs'].update(e['user_attr'])
                elif op == 9:
                    t['system'].update(e['system_attr'])
    assert len(studies) == 1 and all(t['state'] in (1, 3) for t in trials)
    return studies[0], trials


def degree(response):
    from mechanism_metrics import old_fold_score
    return old_fold_score(response)['constraint_vector']


def extract_sources(source_run, binary):
    RUN = Path(source_run).resolve()
    SEARCH = RUN / 'execution/W-search/W1'
    manifest_path = RUN / 'manifest.json'
    local_path = RUN / 'execution/W-local/W1/summary.json'
    protocol_path = ROOT / 'tuning/protocols/robust-v1.json'
    db_path = RUN / 'execution/research-budget.sqlite'
    binding_path = SEARCH / 'run-contract.json'
    journals = sorted(SEARCH.glob('*/*/*/study.journal'))
    assert len(journals) == 40
    wal = Path(str(db_path) + '-wal')
    assert not wal.exists() or not wal.stat().st_size, 'immutable SQLite requires empty WAL'
    fixed_sources = [manifest_path, local_path, protocol_path, db_path, binding_path,
                     SEARCH / 'summary.json', SEARCH / 'state.json', ROOT / 'MECHANISM_V2_PLAN.md']
    before = {str(p): sha(p) for p in fixed_sources + journals}
    manifest = load(manifest_path)
    contract = manifest['contract']
    assert manifest['contract_sha256'] == space.identity(contract)
    binding = load(binding_path)
    state = load(SEARCH / 'state.json')
    assert state['status'] == 'complete'
    assert binding['contract'] == {'manifest_contract_sha256': manifest['contract_sha256']}
    assert binding['plan'] == contract['schedule'][0]['training_plan']
    for path, digest in contract['helpers'].items():
        assert sha(path) == digest, ('changed helper', path)
    assert sha(contract['engine']['path']) == contract['engine']['sha256']
    assert Path(binary).resolve() == Path(contract['engine']['path']).resolve() and sha(binary) == contract['engine']['sha256'], 'P0 must use original frozen engine'
    assert client.bundle_identity(contract['bundle_dir']) == contract['bundle_identity'], 'old source bundle differs'
    plan = contract['schedule'][0]['training_plan']
    defaults = contract['defaults']
    db = sqlite3.connect('file:' + str(db_path) + '?mode=ro&immutable=1', uri=True)
    db.row_factory = sqlite3.Row
    assert strict_loads(db.execute("SELECT value FROM metadata WHERE key='binding'").fetchone()[0])['contract'] == binding['contract']
    assert strict_loads(db.execute("SELECT value FROM metadata WHERE key='halted'").fetchone()[0]) is None
    rows, fail, stages = [], [], collections.Counter()
    ready = {'plan': plan, 'bundle_identity': contract['bundle_identity'], 'ledger_evaluations': 12}
    for path in journals:
        study, trials = read_journal(path)
        family, seed_text, stage_text = path.relative_to(SEARCH).parts[:3]
        stage, group = ('C1', stage_text[3:]) if stage_text.startswith('C1-') else (stage_text, None)
        assert study['name'] == search.study_name('W1', family, int(seed_text), stage, group)
        specification = study['attrs']['contract']['space']
        assert study['attrs']['contract']['binding_identity'] == space.identity(binding)
        assert study['attrs']['contract']['name'] == study['name']
        for number, trial in enumerate(trials):
            token = study['name'] + '/' + str(number)
            record = db.execute('SELECT * FROM records WHERE token=?', (token,)).fetchone()
            assert record is not None and record['study'] == study['name'] and record['attempts'] == 1
            if trial['state'] == 3:
                assert record['status'] == 'failed'
                fail.append({'token': token, 'error': trial['attrs']['error'], 'record': dict(record)})
                continue
            a = trial['attrs']
            assert {'response', 'score', 'source', 'execution'} <= a.keys()
            response, source = a['response'], a['source']
            cfg = response['config']
            digest = space.identity(cfg)
            assert space.normalize_config(cfg, defaults) == cfg
            assert space.sample_config(search.FixedTrial(trial['params']), defaults, specification) == cfg
            assert response['plan'] == plan
            client.validate_result(response, plan, cfg, ready)
            old_score = search.objective(response)
            assert old_score == a['score'] and old_score['values'] == trial['values']
            assert trial['system']['constraints:robust_gates'] == (0.0 if old_score['feasible'] else 1.0)
            assert source['config_identity'] == digest and source['study'] == str(path.parent)
            assert source['trial'] == number and source['score_windows'] == plan['windows']
            assert search.study_name(source['origin'], source['family'], source['seed'], source['stage'], source['group']) == study['name']
            assert source['family'] == cfg['classifier']['family'] and source['data_use'] == contract['data_use']
            expected_parents = []
            if stage == 'C2':
                assert specification['anchors'] == state['frozen'][study['name']]['anchors']
                expected_parents = [next(x['source'] for x in specification['anchors'] if x['config_identity'] == trial['params']['anchor'])]
            elif stage == 'C3':
                expected_parents = state['frozen'][study['name']]['reference_sources']
            assert source['parents'] == expected_parents
            search.check_cutoff(source, '2024-01-01T00:00:00Z')
            key = cache_identity(plan, cfg)
            assert record['status'] == 'complete' and record['cache_key'] == key and a['execution']['cache_key'] == key
            cached = db.execute('SELECT * FROM cache WHERE key=?', (key,)).fetchone()
            assert cached is not None and strict_loads(cached['response']) == response
            owner = db.execute('SELECT * FROM records WHERE token=?', (cached['token'],)).fetchone()
            assert owner is not None and owner['status'] == 'complete' and owner['cache_key'] == key
            violations = degree(response)
            assert (not any(violations)) == old_score['feasible']
            candidate = {'config': cfg, 'config_identity': digest, 'source': source,
                'score': old_score, 'complexity': search.complexity(cfg, defaults)}
            row = {**candidate, 'token': token, 'journal': str(path),
                'created_line': trial['created_line'], 'finished_line': trial['finished_line'],
                'cache_key': key, 'cache_owner': cached['token'], 'budget_record': dict(record),
                'execution': a['execution'], 'response': response, 'params': trial['params'],
                'violation_vector': violations, 'source_key': list(search.source_key(candidate))}
            rows.append(row)
            stages[source['stage']] += 1
    assert len(rows) == 2966 and len(fail) == 106
    assert db.execute("SELECT COUNT(*) FROM records WHERE study LIKE 'W1/%'").fetchone()[0] == len(rows) + len(fail)
    db.close()
    aligned = search.deduplicate([r for r in rows if r['source']['family'] == 'aligned-extended' and r['source']['stage'] == 'C1'])
    def reserve_key(r):
        return (max(r['violation_vector']), sum(r['violation_vector']), -r['score']['values'][1],
                -r['score']['values'][0], tuple(r['complexity']), search.source_key(r))
    aligned.sort(key=reserve_key)
    reserve = aligned[0]
    local = load(local_path)
    anchor_ids = {'A': '12ca514fede4872fe9c955a03df4c59d284aa5cf177970eb2feb494721ec72cd',
                  'B': 'dda1e52a3b7def749dc5a11c23e3568196a4f7839a4fd71f933bd247bf144a68'}
    anchors = {}
    for name, digest in anchor_ids.items():
        candidate = next(c for c in local['candidates'] if c['config_identity'] == digest)
        row = next(r for r in rows if r['source'] == candidate['source'])
        for field in ('config', 'score', 'complexity', 'response', 'execution'):
            assert candidate[field] == row[field], (name, field)
        anchors[name] = {k: row[k] for k in ('config', 'config_identity', 'source', 'score', 'complexity',
            'token', 'journal', 'cache_key', 'source_key')}
        anchors[name]['old_fold_diagnostic'] = [{k:a[k] for k in ('window','funding_scenario','cost_multiplier','metrics')} for a in row['response']['accounts']]
        anchors[name]['old_local_passed'] = candidate['local_passed']
        continuous_token = 'W-local/W1-continuous-' + digest
        anchors[name]['continuous_cache_token'] = continuous_token
    protocol = load(protocol_path)
    controls = {}
    for name, cfg in contract['public_controls'].items():
        assert cfg == local['public_continuous_controls'][name]['config']
        full_path = RUN / 'prepared/B1' / ('public-' + name + '.config.json')
        assert load(full_path) == cfg
        assert sha(full_path) == contract['prepared_files'][str(full_path)]
        before[str(full_path)] = sha(full_path)
        from_protocol = deepcopy(protocol['public_controls'][name])
        from_protocol['daily']['history_start'] = cfg['daily']['history_start']
        assert from_protocol == cfg
        controls[name] = {'config': cfg, 'config_identity': space.identity(cfg),
            'protocol_config_identity': space.identity(protocol['public_controls'][name]),
            'source': str(protocol_path), 'prepared_source': str(manifest_path), 'complete_config_path':str(full_path),
            'preparation_adjustment': {'daily.history_start': {'protocol': '2020-01-01', 'prepared': cfg['daily']['history_start']}},
            'normalization': 'original prepared full JSON retained; enhanced_space.normalize_config would change ADX b=2 to b=1',
            'continuous_cache_token': 'W-local/W1-public-' + name}
    counts = {'journals': len(journals), 'attempts': len(rows) + len(fail), 'complete': len(rows),
        'failed': len(fail), 'unique_complete_configs': len({r['config_identity'] for r in rows}),
        'cached_complete': sum(r['budget_record']['go'] == 0 for r in rows),
        'complete_by_stage': dict(stages), 'old_feasible_records': sum(r['score']['feasible'] for r in rows),
        'old_feasible_unique_configs': len({r['config_identity'] for r in rows if r['score']['feasible']}),
        'equivalent_new_feasibility': len(rows), 'aligned_C1_complete': sum(r['source']['family'] == 'aligned-extended' and r['source']['stage'] == 'C1' for r in rows),
        'aligned_C1_unique': len(aligned)}
    assert all(sha(p) == digest for p, digest in before.items()), 'source changed while read'
    assert not wal.exists() or not wal.stat().st_size
    summary = {'version': 'mechanism-source-extraction-v1', 'real_historical_evaluations': 0,
        'anchors': anchors, 'public_controls': controls, 'defaults': defaults,
        'continuous_plan': contract['schedule'][0]['continuous_plan'], 'training_plan': plan,
        'engine': contract['engine'], 'bundle_identity': contract['bundle_identity'],
        'old_manifest_contract_sha256': manifest['contract_sha256'], 'source_file_sha256': before,
        'verified_frozen_helper_sha256': contract['helpers'], 'counts': counts,
        'aligned_reserve': {k: reserve[k] for k in ('config', 'config_identity', 'source', 'sources', 'score',
            'complexity', 'token', 'journal', 'cache_key', 'source_key', 'violation_vector')},
        'aligned_selection': {'rank_tuple': list(reserve_key(reserve)), 'execution_authorized': False,
            'top_five': [{'config_identity': r['config_identity'], 'source': r['source'],
                'violation_vector': r['violation_vector'], 'rank_tuple': list(reserve_key(r))} for r in aligned[:5]]},
        'verification_scope': ['40 strict JSON journal replays; only known operations; all attempts terminal',
            'all COMPLETE normalized config and stored trial params reconstruction equal',
            'all COMPLETE source identities, recursive cutoff, response protocol metrics schema',
            'all COMPLETE old objective, stored score, values and original gate constraint equal',
            'all COMPLETE SQL reservation/cache key, cache owner and full response equality',
            'all COMPLETE plan section2.1 degree feasibility equals old flag',
            'A/B W-local full config and source equal journal; public prepared config equal actual response',
            'source hashes before and after equal; frozen helpers and engine hash verified'],
        'verification_limits': ['No strategy request, no Go inspect, no Optuna trial',
            'Full economic trace re-verification not performed here; bundle identity validated',
            'Trace identities are preserved in responses but bytes not rehashed across all 80+ GiB',
            'Existing COMPLETE metrics re-scored only; no new continuous history evaluation'],
        'failure_summary': dict(collections.Counter(x['error'] for x in fail))}
    rescored = {'version':'old-fold-six-vector-v2', 'real_historical_evaluations':0,
        'counts':counts, 'rows':[{'token':r['token'], 'config_identity':r['config_identity'],
            'source':r['source'], 'journal':r['journal'], 'old_score':r['score'],
            'complexity':r['complexity'], 'source_key':r['source_key'], 'cache_key':r['cache_key'],
            'old_feasible':r['score']['feasible'],
            'constraint_vector':r['violation_vector'],
            'feasible':not any(r['violation_vector'])} for r in sorted(rows,key=search.source_key)],
        'verification_limits':summary['verification_limits']}
    return summary, rescored

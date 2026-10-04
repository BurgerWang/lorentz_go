"""Bounded serial P4 Journal search, isolated from every prior research cache.

The caller supplies its separately authorized budget, Go evaluator, verified
account analyzer and live binding checkpoint. No preparation calls this runner.
A damaged journal halts durably; this module never trims or repairs it.
"""
from copy import deepcopy
import fcntl
import importlib
import math
from pathlib import Path

from enhanced import strict_loads
from enhanced_space import canonical, identity
from optimize_enhanced import sampler
from robust_budget import BudgetExhausted, BudgetHalted, atomic_json
from robust_client import EvaluationError, ProtocolError, normalize_plan

VERSION = 'mechanism-p4-search-v1'
BLOCKS = ('classifier', 'management')


class SearchHalted(ProtocolError):
    """Identity, journal, accounting or persistence fault; no automatic resume."""


def _same(a, b):
    return canonical(a) == canonical(b)


def _halt(budget, reason):
    budget.halt('P4 search: ' + str(reason))
    raise SearchHalted(str(reason))


def _checkpoint(checkpoint):
    if checkpoint is not None:
        checkpoint()


def _read(path):
    return strict_loads(Path(path).read_text())


def _journal(path):
    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise ProtocolError('journal is not a regular file')
        with path.open('rb') as stream:
            for line in stream:
                if not line.endswith(b'\n'):
                    raise ProtocolError('incomplete journal tail requires explicit recovery')
                strict_loads(line)


def _score(analysis):
    score = analysis['score']
    names, vector = score['constraint_names'], score['constraint_vector']
    if (not isinstance(names, list) or len(names) != 6 or len(set(names)) != 6
        or any(not isinstance(k, str) or not k for k in names)
        or not isinstance(vector, list) or len(vector) != 6
        or any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in vector)):
        raise ProtocolError('six named nonnegative graded constraints required')
    constraints = dict(zip(names, vector))
    if (not _same(score.get('violations'), constraints)
        or type(score.get('feasible')) is not bool
        or score['feasible'] != all(v == 0 for v in vector)
        or score.get('constraint_sum') != sum(vector)
        or score.get('max_violation') != max(vector)
        or not isinstance(score.get('values'), list) or len(score['values']) != 2
        or any(type(v) not in (int, float) or not math.isfinite(v) for v in score['values'])
        or type(score.get('max_drawdown_pct')) not in (int, float)
        or not math.isfinite(score['max_drawdown_pct']) or score['max_drawdown_pct'] < 0
        or not isinstance(analysis.get('accounts'), list) or len(analysis['accounts']) != 2):
        raise ProtocolError('invalid verified pair score/analysis')
    canonical(analysis)  # Reject non-JSON or nonfinite persisted payloads.
    return score, constraints


def _cache_key(binding, plan, config):
    return identity({'binding_identity': binding, 'plan': plan, 'config': config})


def _config(domain, base, rules, block, params):
    keys = rules['blocks'][block]['keys']
    domains = rules['blocks'][block]['domains']
    if set(params) != set(keys):
        raise ValueError('trial parameter names differ from frozen block')
    for key in keys:
        value = params[key]
        if (type(value) not in (int, float) or not math.isfinite(value)
            or not any(type(value) is type(v) and value == v for v in domains[key])):
            raise ValueError('trial parameter is outside its frozen numeric domain')
    config = domain.compile_config(deepcopy(base['config']), deepcopy(rules), block, deepcopy(params))
    validated = domain.validate_config(deepcopy(config), deepcopy(rules))
    if not _same(validated, config):
        raise ProtocolError('domain validation changed compiled configuration')
    if not _same(domain.parameters(config, rules, block), params):
        raise ProtocolError('compiled configuration/parameters differ')
    return config


def _source(spec, number, config, record, token):
    return {'kind': 'p4-go' if record['charged']['go_requests'] else 'p4-cache',
            'study': spec['name'], 'study_path': spec['folder'],
            'skeleton': spec['skeleton'], 'seed': spec['seed'], 'block': spec['block'],
            'trial': number, 'binding_identity': spec['binding_identity'],
            'config_identity': identity(config), 'parents': [deepcopy(spec['base']['source'])],
            'data_use': 'exposed retrospective development',
            'reservation_token': token, 'cache_key': record['cache_key'],
            'cache_source_token': record.get('cache_token', token)}


def _candidate(config, response, analysis, source, base):
    return {'config': deepcopy(config), 'config_identity': identity(config),
            'score': deepcopy(analysis['score']), 'analysis': deepcopy(analysis),
            'response': deepcopy(response), 'source': deepcopy(source),
            'anchor_preferred': _same(config, base['config'])}


def _record_shape(record, name, token):
    if (not record or record['study'] != name
        or record['charged']['trial_attempts'] != 1
        or record['charged']['go_requests'] not in (0, 1)
        or record['charged']['ledger_evaluations'] != 2 * record['charged']['go_requests']):
        raise ProtocolError('missing/mismatched charged attempt: ' + token)


def _verified_trial(study, trial, spec, budget, domain, analyze, checkpoint, recover=False):
    """Reverify native params, cached response, traces, score and ancestry."""
    import optuna
    token = spec['name'] + '/' + str(trial.number)
    record = budget.record(token)
    _record_shape(record, spec['name'], token)
    if record['status'] != 'complete':
        raise ProtocolError('successful native trial has no successful budget record')
    config = _config(domain, spec['base'], spec['rules'], spec['block'], trial.params)
    key = _cache_key(spec['binding_identity'], spec['plan'], config)
    if record.get('cache_key') != key or not _same(trial.user_attrs.get('config'), config):
        raise ProtocolError('native trial config/cache binding differs')
    response = budget.cached_response(key)
    if (not isinstance(response, dict) or not _same(response.get('config'), config)
        or not _same(response.get('plan'), normalize_plan(spec['plan']))):
        raise ProtocolError('durable response plan/config differs')
    origin = budget.record(record.get('cache_token', token))
    if (not origin or origin['status'] != 'complete' or origin.get('cache_key') != key
        or origin['charged']['go_requests'] != 1 or origin['charged']['ledger_evaluations'] != 2):
        raise ProtocolError('cached response has no matching charged P4 Go source')
    source = _source(spec, trial.number, config, record, token)
    saved_source = trial.user_attrs.get('source')
    # A cache hit commits its complete budget row before any journal metadata.
    # Its validated original source makes this narrow missing-metadata window
    # recoverable without sending or weakening checks on actual Go requests.
    cache_metadata_window = recover and not record['charged']['go_requests'] and saved_source is None
    if not cache_metadata_window and not _same(saved_source, source):
        raise ProtocolError('persisted execution source/ancestry differs')
    if recover and record['charged']['go_requests'] and trial.user_attrs.get('execution') is None:
        raise ProtocolError('completed Go response lost pre-send native reservation')
    _checkpoint(checkpoint)
    analysis = analyze(deepcopy(response))
    score, constraints = _score(analysis)
    expected = {'response': response, 'analysis': analysis, 'score': score, 'execution': record}
    if recover:
        if trial.state.name != 'RUNNING':
            raise ProtocolError('only a running trial may recover a completed response')
        for key, value in expected.items():
            old = trial.user_attrs.get(key)
            # Execution was first saved as the pending reservation before send.
            if key == 'execution' and old is not None:
                comparable = dict(old, status='complete')
                if not _same(comparable, record):
                    raise ProtocolError('pending execution charge differs')
            elif old is not None and not _same(old, value):
                raise ProtocolError('partial completed native payload differs: ' + key)
        existing = trial.constraints
        if any(key not in constraints or value != constraints[key] for key, value in existing.items()):
            raise ProtocolError('partial native constraints differ')
        live = optuna.trial.Trial(study, trial._trial_id)
        live.set_user_attr('source', source)
        for key, value in expected.items():
            live.set_user_attr(key, value)
        for key, value in constraints.items():
            if key not in existing:
                live.set_constraint(key, value)
        study.tell(live, score['values'])
    else:
        if (trial.state.name != 'COMPLETE' or any(not _same(trial.user_attrs.get(k), v) for k, v in expected.items())
            or not _same(trial.constraints, constraints) or not _same(list(trial.values), score['values'])):
            raise ProtocolError('completed native payload/score/constraints differs')
    return _candidate(config, response, analysis, source, spec['base'])


def _open_study(folder, spec, budget, seed, startup):
    import optuna
    block_path, journal_path = folder / 'block.json', folder / 'study.journal'
    if block_path.exists():
        if not _same(_read(block_path), spec):
            raise ProtocolError('frozen block/base/helper binding differs')
    else:
        if journal_path.exists() or budget.records_for(spec['name']):
            raise ProtocolError('persistent study/attempt has no frozen block')
        atomic_json(block_path, spec)
    _journal(journal_path)
    storage = optuna.storages.JournalStorage(optuna.storages.journal.JournalFileBackend(str(journal_path)))
    names = optuna.get_all_study_names(storage)
    if names and names != [spec['name']]:
        raise ProtocolError('unexpected study in isolated journal')
    sampling = sampler(seed, startup, 'independent')
    study = (optuna.load_study(study_name=spec['name'], storage=storage, sampler=sampling) if names
             else optuna.create_study(study_name=spec['name'], storage=storage, sampler=sampling,
                                      directions=['maximize', 'maximize']))
    frozen = study.user_attrs.get('contract')
    if frozen is None:
        if study.trials:
            raise ProtocolError('native study has no frozen binding')
        study.set_user_attr('contract', spec)
    elif not _same(frozen, spec):
        raise ProtocolError('native study frozen binding differs')
    if not study.trials:
        study.enqueue_trial(spec['anchor_params'], user_attrs={'queued_anchor': True})
    elif (study.trials[0].user_attrs.get('queued_anchor') is not True
          or not _same(study.trials[0].system_attrs.get('fixed_params'), spec['anchor_params'])):
        raise ProtocolError('first native trial is not the frozen queued anchor')
    return study


def _recover(study, spec, budget, domain, analyze, checkpoint):
    import optuna
    records = budget.records_for(spec['name'])
    verified = {}
    trials = study.trials
    if len(records) > spec['quota'] or len(trials) > spec['quota']:
        raise ProtocolError('native study exceeds frozen attempt quota')
    for trial in trials:
        token = spec['name'] + '/' + str(trial.number)
        record = records.get(token)
        if trial.state.name == 'WAITING' and record is None:
            if trial.number != 0 or len(trials) != 1:
                raise ProtocolError('unexpected uncharged waiting trial')
            continue
        _record_shape(record, spec['name'], token)
        if trial.state.name == 'COMPLETE' and record['status'] != 'complete':
            raise ProtocolError('complete journal and budget disagree')
        if trial.state.name == 'FAIL' and record['status'] not in ('failed', 'interrupted'):
            raise ProtocolError('failed journal and budget disagree')
        if trial.state.name not in ('COMPLETE', 'FAIL', 'RUNNING', 'WAITING'):
            raise ProtocolError('unsupported persistent trial state')
    for token, record in records.items():
        prefix, number = token.rsplit('/', 1)
        if prefix != spec['name'] or not number.isdigit() or str(int(number)) != number:
            raise ProtocolError('invalid persisted attempt token')
        number = int(number)
        _record_shape(record, spec['name'], token)
        trials = study.trials
        if number >= len(trials):
            if (number != len(trials) or record['status'] != 'attempt'
                or record['charged']['go_requests']):
                raise ProtocolError('charged attempt lost its native journal trial')
            # The sole safe missing-trial window is reservation before ask.
            live = study.ask()
            if live.number != number:
                raise ProtocolError('reservation-before-ask number differs')
            study.tell(live, state=optuna.trial.TrialState.FAIL)
        else:
            trial = trials[number]
            if trial.state.name == 'RUNNING' and record['status'] == 'complete':
                verified[number] = _verified_trial(study, trial, spec, budget, domain, analyze, checkpoint, recover=True)
            elif trial.state.name in ('RUNNING', 'WAITING'):
                if record['status'] not in ('attempt', 'reserved', 'failed', 'interrupted'):
                    raise ProtocolError('unknown native request/budget state differs')
                if trial.state.name == 'WAITING':
                    live = study.ask()
                    if live.number != number:
                        raise ProtocolError('queued reservation recovery number differs')
                    study.tell(live, state=optuna.trial.TrialState.FAIL)
                else:
                    study.tell(number, state=optuna.trial.TrialState.FAIL)
    budget.recover(spec['name'])
    final = study.trials
    if len([t for t in final if t.state.name != 'WAITING']) != len(records):
        raise ProtocolError('budget/native trial counts differ')
    for trial in final:
        if trial.state.name == 'COMPLETE' and trial.number not in verified:
            verified[trial.number] = _verified_trial(study, trial, spec, budget, domain, analyze, checkpoint)
    return verified


def _blocked_keys(budget):
    return {r['cache_key'] for r in budget.snapshot(include_cache=False)['records'].values()
            if r['status'] in ('failed', 'interrupted') and r.get('cache_key')}


def _fail(study, trial, budget, token, error):
    import optuna
    record = budget.record(token)
    if record and record['status'] in ('attempt', 'reserved'):
        budget.finish(token, error=error)
    trial.set_user_attr('error', str(error))
    trial.set_user_attr('execution', budget.record(token))
    study.tell(trial, state=optuna.trial.TrialState.FAIL)


def _step(study, spec, budget, domain, evaluator, analyze, checkpoint):
    import optuna
    trials = study.trials
    waiting = [t for t in trials if t.state.name == 'WAITING']
    number = waiting[0].number if waiting else len(trials)
    token = spec['name'] + '/' + str(number)
    record = budget.reserve_attempt(spec['name'], token)  # Durable before ask/compile.
    if record['status'] != 'attempt':
        raise ProtocolError('next native number already consumed')
    trial = study.ask()
    if trial.number != number:
        raise ProtocolError('next native number differs from charged attempt')
    try:
        block = spec['rules']['blocks'][spec['block']]
        params = {key: trial.suggest_categorical(key, block['domains'][key]) for key in block['keys']}
        config = _config(domain, spec['base'], spec['rules'], spec['block'], params)
    except ValueError as error:
        _fail(study, trial, budget, token, error)
        return
    trial.set_user_attr('config', config)
    key = _cache_key(spec['binding_identity'], spec['plan'], config)
    if key in _blocked_keys(budget):
        _fail(study, trial, budget, token, 'identical failed/unknown cache key; no automatic retry')
        return
    reservation = budget.reserve_evaluation(spec['name'], token, key, 2)
    record = reservation['record']
    trial.set_user_attr('execution', record)
    source = _source(spec, number, config, record, token)
    trial.set_user_attr('source', source)
    try:
        if reservation['send']:
            _checkpoint(checkpoint)
            trace = Path(spec['folder']) / 'traces' / str(number)
            response = evaluator(deepcopy(spec['plan']), deepcopy(config), trace)
        else:
            response = reservation['response']
        if (not isinstance(response, dict) or not _same(response.get('plan'), normalize_plan(spec['plan']))
            or not _same(response.get('config'), config)):
            raise ProtocolError('response plan/config changed')
        analysis = analyze(deepcopy(response))
        score, constraints = _score(analysis)
        if reservation['send']:
            budget.finish(token, response=response)
        trial.set_user_attr('response', response)
        trial.set_user_attr('analysis', analysis)
        trial.set_user_attr('score', score)
        trial.set_user_attr('execution', budget.record(token))
        for name, value in constraints.items():
            trial.set_constraint(name, value)
        study.tell(trial, score['values'])
        saved = study.trials[number]
        if not _same(saved.constraints, constraints) or not _same(list(saved.values), score['values']):
            raise ProtocolError('native completed score/constraints differ after tell')
        return _candidate(config, response, analysis, source, spec['base'])
    except EvaluationError as error:
        _fail(study, trial, budget, token, error)


def _selected(spec, verified, domain):
    # Reuse validated evidence rather than re-reading every large trace twice.
    base = deepcopy(spec['base'])
    base['anchor_preferred'] = True
    if not base['score']['feasible']:
        raise ProtocolError('current selected anchor must be feasible')
    candidates = [base] + [c for c in verified.values() if c['score']['feasible']]
    return deepcopy(min(candidates, key=domain.rank_key))


def _prior_anchor(candidate):
    base = deepcopy(candidate)
    if not isinstance(base.get('config'), dict) or not isinstance(base.get('score'), dict) or not base['score'].get('feasible'):
        raise ProtocolError('closed feasible prior anchor required')
    parent = deepcopy(base.get('source', {}))
    if parent.get('kind') != 'prior-anchor':
        base['source'] = {'kind': 'prior-anchor', 'parents': [parent], 'config_identity': identity(base['config'])}
    base['config_identity'] = identity(base['config'])
    base['anchor_preferred'] = True
    return base


def run_search(execution, contract, budget, evaluator, analyze, checkpoint=None,
               until_attempts=None, quota=64):
    """Run/resume the frozen studies; successful native rows never evaluate again.

    until_attempts is an absolute global P4 attempt checkpoint, not a refill.
    quota is frozen in each block; smaller values exist only for isolated tests.
    """
    import optuna
    if budget.halted():
        raise BudgetHalted(budget.halted())  # Precheck before any filesystem/recovery mutation.
    if (optuna.__version__ != '5.0.0' or not hasattr(optuna.trial.Trial, 'set_constraint')
        or type(quota) is not int or not 1 <= quota <= 64
        or (until_attempts is not None and (type(until_attempts) is not int or until_attempts < 0))
        or contract.get('startup_trials') != 16):
        _halt(budget, 'unsupported runtime/quota/startup contract')
    pair = contract.get('plan', {})
    if (budget.binding.get('contract') != {'manifest_contract_sha256': contract.get('binding_identity')}
        or pair.get('account_mode') != 'fixed-continuous-v1'
        or pair.get('funding_mode') != 'proxy-stress-v1'
        or pair.get('funding_scenarios') != ['central', 'proxy-adverse']
        or pair.get('cost_multipliers') != [1.5] or len(pair.get('windows', [])) != 1):
        _halt(budget, 'P4 budget binding or two-ledger continuous plan differs')
    domain = importlib.import_module('mechanism_p4_domain')
    root = Path(execution).resolve() / 'search'
    try:
        _checkpoint(checkpoint)
        root.mkdir(parents=True, exist_ok=True)
        lock = (root / 'run.lock').open('a+')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            lock.close()
            raise
    except (KeyboardInterrupt, SystemExit, BlockingIOError):
        raise
    except Exception as error:
        _halt(budget, error)
    branches = []
    try:
        with lock:
            for skeleton, rules in contract['domains'].items():
                for seed in contract['seeds']:
                    branch = {'skeleton': skeleton, 'seed': seed, 'classifier_selected': None,
                              'selected': None, 'studies': []}
                    branches.append(branch)
                    base = _prior_anchor(contract['anchors'][skeleton])
                    for block in BLOCKS:
                        _checkpoint(checkpoint)
                        name = f'P4/{skeleton}/{seed}/{block}'
                        if budget.binding['substudy_limits'].get(name) != {
                                'trial_attempts': quota, 'go_requests': quota, 'ledger_evaluations': quota * 2}:
                            raise ProtocolError('substudy budget differs from frozen quota')
                        folder = root / skeleton / str(seed) / block
                        spec = {'version': VERSION, 'name': name, 'folder': str(folder),
                                'binding_identity': contract['binding_identity'],
                                'plan': deepcopy(contract['plan']), 'skeleton': skeleton,
                                'seed': seed, 'block': block, 'rules': deepcopy(rules),
                                'base': deepcopy(base), 'quota': quota, 'startup_trials': 16,
                                'sampler': {'seed_rule': '(seed+trial.number)%2**32', 'n_ei_candidates': 24,
                                            'native_constraints': 'six graded named violations'},
                                'journal_tail_policy': 'durable halt; explicit recovery required',
                                'anchor_params': domain.parameters(base['config'], rules, block)}
                        study = _open_study(folder, spec, budget, seed, 16)
                        verified = _recover(study, spec, budget, domain, analyze, checkpoint)
                        while len([t for t in study.trials if t.state.name != 'WAITING']) < quota:
                            if until_attempts is not None and budget.counts()['counts']['trial_attempts'] >= until_attempts:
                                break
                            candidate = _step(study, spec, budget, domain, evaluator, analyze, checkpoint)
                            if candidate is not None:
                                verified[candidate["source"]["trial"]] = candidate
                        selected = _selected(spec, verified, domain)
                        consumed = len([t for t in study.trials if t.state.name != 'WAITING'])
                        complete = consumed == quota
                        branch['studies'].append({'name': name, 'block': block, 'folder': str(folder),
                                                  'status': 'complete' if complete else 'partial',
                                                  'trial_attempts': consumed, 'selected': selected})
                        if block == 'classifier':
                            branch['classifier_selected'] = selected
                        branch['selected'] = selected
                        if not complete:
                            result = {'status': 'partial', 'branches': branches}
                            atomic_json(root / 'summary.json', result)
                            return result
                        base = selected
            result = {'status': 'complete', 'branches': branches}
            atomic_json(root / 'summary.json', result)
            return result
    except (KeyboardInterrupt, SystemExit):
        raise  # Charged pending requests remain consumed and are recovered as FAIL.
    except (BudgetExhausted, BudgetHalted):
        raise
    except Exception as error:
        _halt(budget, error)

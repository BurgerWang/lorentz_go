"""Experimental wrapper over the unchanged protocol3 accounting client."""
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path
import collections
import hashlib
import selectors
import subprocess
import tempfile
import threading

import enhanced
import enhanced_space as space
import optimize
import robust_client as base
from robust_data import utc_ms, INTERVALS

CONFIG_VERSION = 'mechanism-p3-config-v1'
GO_CONTRACT = 'mechanism-p3-go-v1'
TRACE_VERSION = 'mechanism-indicator-v1'
VARIANTS = ('classic-original', 'rq-direction', 'momentum-four', 'classic-recent')
ProtocolError, EvaluationError = base.ProtocolError, base.EvaluationError


def wrapper(strategy, variant):
    value = {'version': CONFIG_VERSION, 'variant': variant, 'strategy': deepcopy(strategy)}
    validate_wrapper(value)
    return value


def validate_wrapper(value):
    if (not isinstance(value, dict) or set(value) != {'version', 'variant', 'strategy'}
        or value['version'] != CONFIG_VERSION or value['variant'] not in VARIANTS
        or not isinstance(value['strategy'], dict)):
        raise ProtocolError('invalid P3 complete wrapper/variant')
    c = value['strategy']
    if (c.get('schema_version') != 5 or c.get('classifier', {}).get('family') != 'classic-extended'
        or c.get('classic', {}).get('algorithm') != 'original-online'
        or c['classic'].get('sample_stride') != 4):
        raise ProtocolError('P3 requires schema5 Classic-online with frozen absolute four-bar phase')
    return value


def validate_ready(ready, plan):
    base.validate_ready(ready, plan)
    if ready.get('mechanism_contract') != GO_CONTRACT or ready.get('supported_variants') != list(VARIANTS):
        raise ProtocolError('experimental ready contract differs')
    validate_wrapper(ready['default_config'])
    return ready


def validate_result(result, plan, config, ready):
    validate_wrapper(config)
    if result.get('mechanism_contract') != GO_CONTRACT or space.canonical(result.get('config')) != space.canonical(config):
        raise ProtocolError('experimental result/wrapper identity differs')
    unpacked = dict(result, config=config['strategy'])
    base.validate_result(unpacked, plan, config['strategy'], ready)
    trace = result.get('mechanism_trace')
    if (not isinstance(trace, dict) or trace.get('version') != TRACE_VERSION
        or trace.get('variant') != config['variant'] or type(trace.get('rows')) is not int
        or trace['rows'] <= 0 or type(trace.get('emitted')) is not bool):
        raise ProtocolError('missing experimental state trace contract')
    return result


def verify_state_trace(result):
    """Check sidecar integrity and observable recent-window invariants.

    Go remains the strategy implementation. Full direction/state correctness is
    covered by Go prefix and counterexample tests; this does not duplicate it.
    """
    config = validate_wrapper(result['config'])
    info = result['mechanism_trace']
    if not info['emitted'] or not info.get('path'):
        raise ProtocolError('P3 execution requires exclusive state trace')
    path = Path(info['path'])
    if not path.is_file() or path.is_symlink():
        raise ProtocolError('missing or nonregular state trace')
    h = hashlib.sha256()
    count = 0
    c = config['strategy']['classic']
    previous_time = None
    plan = result['plan']
    step = INTERVALS[plan['interval']]
    history = utc_ms(plan['history_start'])
    expected_rows = (utc_ms(plan['windows'][-1]['end'])-history)//step
    with path.open('rb') as f:
        for line in f:
            h.update(line)
            row = enhanced.strict_loads(line)
            if (type(row.get('index')) is not int or row['index'] != count
                or row.get('variant') != config['variant'] or row.get('version') != TRACE_VERSION):
                raise ProtocolError('state trace index/variant differs')
            t = row.get('time')
            if type(t) is not int or t != history+(count+1)*step-1 or (previous_time is not None and t <= previous_time):
                raise ProtocolError('state trace times differ')
            previous_time = t
            for k in ('prediction', 'raw_direction', 'signal', 'bars_held', 'search_start', 'search_end', 'neighbors', 'max_neighbor_index'):
                if type(row.get(k)) is not int:
                    raise ProtocolError('state trace integer field differs')
            for k in ('filter_ok', 'ready', 'start_long', 'start_short', 'end_long', 'end_short'):
                if type(row.get(k)) is not bool:
                    raise ProtocolError('state trace boolean field differs')
            if row['signal'] not in (-1, 0, 1) or row['bars_held'] < 0:
                raise ProtocolError('state trace signal/age differs')
            if row['raw_direction'] != (1 if row['prediction'] > 0 else -1 if row['prediction'] < 0 else 0):
                raise ProtocolError('state trace raw direction/vote differs')
            indices = row.get('queue_indices')
            if not isinstance(indices, list) or len(indices) > c['neighbors'] or any(type(j) is not int for j in indices):
                raise ProtocolError('state trace queue indices invalid')
            if row['neighbors'] != len(indices) or row['max_neighbor_index'] != max(indices, default=-1):
                raise ProtocolError('state trace queue summary differs')
            if config['variant'] in ('rq-direction', 'momentum-four') and (indices or row['prediction'] not in (-1, 0, 1)):
                raise ProtocolError('direction baseline retained classifier votes')
            if config['variant'] == 'classic-recent':
                start = max(0, count-c['max_bars_back']+1)
                if row['search_start'] != start or row['search_end'] != count:
                    raise ProtocolError('recent search interval differs')
                if any(j < start or j > count or j % 4 == 0 for j in indices):
                    raise ProtocolError('expired or phase-shifted recent vote')
            count += 1
    if count != expected_rows or count != info['rows'] or h.hexdigest() != info.get('sha256'):
        raise ProtocolError('state trace count/hash differs')
    return {'verified': True, 'rows': count, 'variant': config['variant'], 'sha256': h.hexdigest()}


class Server(base.Server):
    def __init__(self, binary, plan, timeout=600, termination=None):
        self.source_plan = enhanced.strict_loads(Path(plan).read_text())
        self.timeout, self.termination = timeout, termination
        self.buffer, self.stderr = b'', collections.deque(maxlen=100)
        self.process = self.selector = self.thread = None
        self.next_id = 0
        try:
            with termination.defer() if termination else nullcontext():
                self.process = subprocess.Popen([str(binary), 'mechanism-eval', '-plan', str(plan)],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.selector = selectors.DefaultSelector()
                self.selector.register(self.process.stdout, selectors.EVENT_READ)
                self.thread = threading.Thread(target=self._drain_stderr, daemon=True)
                self.thread.start()
            self.ready = self._read()
            validate_ready(self.ready, self.source_plan)
        except BaseException:
            self.close()
            raise

    def evaluate(self, config, trace_dir=None):
        validate_wrapper(config)
        self.next_id += 1
        req = {'id': self.next_id, 'config': config,
               'trace_dir': str(Path(trace_dir).resolve()) if trace_dir else ''}
        try:
            self.process.stdin.write((space.canonical(req)+'\n').encode())
            self.process.stdin.flush()
        except OSError as e:
            raise ProtocolError('experimental request pipe failed') from e
        r = self._read()
        if type(r.get('id')) is not int or r['id'] != self.next_id:
            raise ProtocolError('experimental response ID differs')
        if r.get('type') == 'error':
            if r.get('error_kind') in ('equity_protection', 'configuration_insufficient'):
                raise EvaluationError(r.get('error', 'configuration/equity protection'))
            raise ProtocolError('experimental engine error: '+str(r))
        return validate_result(r, self.source_plan, config, self.ready)


def inspect(binary, plan, config, timeout=600):
    validate_wrapper(config)
    with tempfile.TemporaryDirectory(prefix='lorentz-p3-inspect-') as tmp:
        pp, cp = Path(tmp)/'plan.json', Path(tmp)/'config.json'
        pp.write_text(space.canonical(plan)); cp.write_text(space.canonical(config))
        r = subprocess.run([str(binary), 'mechanism-eval', '-plan', str(pp), '-config', str(cp), '-inspect'],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        if r.returncode:
            raise ProtocolError(r.stderr.decode(errors='replace'))
        ready = validate_ready(enhanced.strict_loads(r.stdout), plan)
        if ready.get('inspected_config') != config:
            raise ProtocolError('inspect did not bind complete experimental configuration')
        return ready


def go_validate(binary, configs):
    for c in configs:
        validate_wrapper(c)
    r = subprocess.run([str(binary), 'mechanism-config-validate', '-batch'],
                       input=space.canonical(configs).encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    if r.returncode:
        raise ProtocolError(r.stderr.decode(errors='replace'))
    if space.canonical(enhanced.strict_loads(r.stdout)) != space.canonical(configs):
        raise ProtocolError('complete experimental configurations changed during Go validation')


class Evaluator(base.Evaluator):
    def __call__(self, plan, config, trace_dir=None):
        key = space.identity(base.normalize_plan(plan))
        if key not in self.sessions:
            pp = Path(self.tmp.name)/(key+'.json'); pp.write_text(space.canonical(plan))
            snapshots = self._snapshots(plan)
            server = Server(self.binary, pp, self.timeout)
            if self._snapshots(plan) != snapshots:
                server.close(); raise ProtocolError('data changed during experimental load')
            self.sessions[key] = (server, snapshots)
        server, snapshots = self.sessions[key]
        if hashlib.sha256(self.binary.read_bytes()).hexdigest() != self.engine_hash or self._snapshots(plan) != snapshots:
            raise ProtocolError('experimental engine/data changed')
        r = server.evaluate(config, trace_dir)
        if self._snapshots(plan) != snapshots:
            raise ProtocolError('data changed during experimental evaluation')
        r['timing']['load_seconds'] = server.ready['load_seconds']
        return r


def evaluate(binary, plan, config, trace_dir=None, timeout=600):
    # A new evaluator/process for every independent replay.
    with Evaluator(binary, timeout) as evaluator:
        return evaluator(plan, config, trace_dir)

"""Atomic shared research/substudy budgets. Reservations precede every Go send.

The single durable SQLite transaction is the authority for counters, in-flight consumption
and successful cache entries. A killed request is charged and never replayed.
"""
from contextlib import contextmanager
from copy import deepcopy
import sqlite3
import json
import os
from pathlib import Path
import tempfile

from enhanced_space import canonical, identity
from enhanced import strict_loads

COUNTERS = ('trial_attempts', 'go_requests', 'ledger_evaluations')


class BudgetExhausted(RuntimeError):
    pass


class BudgetHalted(RuntimeError):
    pass


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(canonical(value) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _limits(value):
    if not isinstance(value, dict) or set(value) != set(COUNTERS):
        raise ValueError('complete explicit budget limits required')
    if any(type(v) is not int or v < 0 for v in value.values()):
        raise ValueError('budget limits must be nonnegative integers')
    return deepcopy(value)



class Budget:
    """Atomic counters and successful cache shared across independent studies.

    SQLite transactions reserve global and substudy allowances together, with
    synchronous durability before Go is sent anything. Attempts and successes
    are separate rows, avoiding rewriting all previous responses on each trial.
    """
    def __init__(self, path, contract, global_limits, substudy_limits):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.binding = {'version': 'robust-budget-v1-sqlite', 'contract': deepcopy(contract),
                        'global_limits': _limits(global_limits),
                        'substudy_limits': {k: _limits(v) for k, v in substudy_limits.items()}}
        if not self.binding['substudy_limits']:
            raise ValueError('at least one frozen substudy required')
        with self._transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS totals (name TEXT PRIMARY KEY, attempts INTEGER NOT NULL CHECK(attempts>=0), go INTEGER NOT NULL CHECK(go>=0), ledgers INTEGER NOT NULL CHECK(ledgers>=0))')
            db.execute("CREATE TABLE IF NOT EXISTS records (token TEXT PRIMARY KEY, study TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('attempt','reserved','complete','failed','interrupted')), attempts INTEGER NOT NULL CHECK(attempts IN (0,1)), go INTEGER NOT NULL CHECK(go IN (0,1)), ledgers INTEGER NOT NULL CHECK(ledgers>=0), cache_key TEXT, cache_token TEXT, error TEXT)")
            db.execute('CREATE INDEX IF NOT EXISTS records_study ON records(study)')
            db.execute('CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, token TEXT NOT NULL, response TEXT NOT NULL)')
            existing = db.execute("SELECT value FROM metadata WHERE key='binding'").fetchone()
            if existing:
                if existing['value'] != canonical(self.binding):
                    raise ValueError('research budget contract differs; cannot resume')
            else:
                if db.execute('SELECT COUNT(*) FROM records').fetchone()[0]:
                    raise ValueError('nonempty budget has no contract')
                db.execute('INSERT INTO metadata VALUES (?,?)', ('binding', canonical(self.binding)))
                db.execute('INSERT INTO metadata VALUES (?,?)', ('halted', 'null'))
                for name in ('', *substudy_limits):
                    db.execute('INSERT INTO totals VALUES (?,0,0,0)', (name,))
            self._validate(db)

    @contextmanager
    def _transaction(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _validate(self, db):
        rows = db.execute('SELECT * FROM totals').fetchall()
        if {r['name'] for r in rows} != {'', *self.binding['substudy_limits']}:
            raise ValueError('invalid substudy budget state')
        sums = {r['study']: (r['a'], r['g'], r['l']) for r in db.execute('SELECT study,SUM(attempts) a,SUM(go) g,SUM(ledgers) l FROM records GROUP BY study')}
        if not set(sums) <= set(self.binding['substudy_limits']):
            raise ValueError('unregistered persisted substudy')
        total = tuple(sum(x[i] for x in sums.values()) for i in range(3))
        for r in rows:
            actual = (r['attempts'], r['go'], r['ledgers'])
            if actual != (sums.get(r['name'], (0, 0, 0)) if r['name'] else total):
                raise ValueError('budget counters do not match reservations')
            limits = self.binding['substudy_limits'][r['name']] if r['name'] else self.binding['global_limits']
            if any(actual[i] > limits[k] for i, k in enumerate(COUNTERS)):
                raise ValueError('persistent budget exceeds contract')
        bad = db.execute("SELECT COUNT(*) FROM cache c LEFT JOIN records r ON c.token=r.token WHERE r.token IS NULL OR r.status!='complete' OR r.cache_key!=c.key").fetchone()[0]
        if bad:
            raise ValueError('cache has no successful reservation')

    @staticmethod
    def _record(row):
        if row is None:
            return None
        output = {'study': row['study'], 'status': row['status'],
                  'charged': dict(zip(COUNTERS, (row['attempts'], row['go'], row['ledgers'])))}
        for key in ('cache_key', 'cache_token', 'error'):
            if row[key] is not None:
                output[key] = row[key]
        return output

    @staticmethod
    def _halted(db):
        return strict_loads(db.execute("SELECT value FROM metadata WHERE key='halted'").fetchone()[0])

    def _charge(self, db, study, values):
        halted = self._halted(db)
        if halted:
            raise BudgetHalted(halted)
        if study not in self.binding['substudy_limits']:
            raise ValueError('unregistered substudy')
        for name, limits in (('', self.binding['global_limits']), (study, self.binding['substudy_limits'][study])):
            r = db.execute('SELECT attempts,go,ledgers FROM totals WHERE name=?', (name,)).fetchone()
            for i, k in enumerate(COUNTERS):
                if r[i] + values[k] > limits[k]:
                    raise BudgetExhausted('frozen budget exhausted: ' + k)
        for name in ('', study):
            db.execute('UPDATE totals SET attempts=attempts+?,go=go+?,ledgers=ledgers+? WHERE name=?',
                       (*[values[k] for k in COUNTERS], name))

    def reserve_attempt(self, study, token):
        with self._transaction() as db:
            old = db.execute('SELECT * FROM records WHERE token=?', (token,)).fetchone()
            if old:
                if old['study'] != study or old['attempts'] != 1:
                    raise ValueError('attempt token differs')
                return self._record(old)
            self._charge(db, study, {'trial_attempts': 1, 'go_requests': 0, 'ledger_evaluations': 0})
            db.execute("INSERT INTO records(token,study,status,attempts,go,ledgers) VALUES (?,?,'attempt',1,0,0)", (token, study))
            return self._record(db.execute('SELECT * FROM records WHERE token=?', (token,)).fetchone())

    def reserve_evaluation(self, study, token, cache_key, ledgers, fixed=False):
        """Return {send,response,record}. Only send=True permits exactly one call."""
        if type(ledgers) is not int or ledgers <= 0 or not isinstance(cache_key, str) or not cache_key:
            raise ValueError('positive ledger charge and cache identity required')
        with self._transaction() as db:
            halted = self._halted(db)
            if halted:
                raise BudgetHalted(halted)
            old = db.execute('SELECT * FROM records WHERE token=?', (token,)).fetchone()
            if old is None:
                if not fixed:
                    raise ValueError('trial must reserve attempt before evaluation')
                if study not in self.binding['substudy_limits']:
                    raise ValueError('unregistered substudy')
                db.execute("INSERT INTO records(token,study,status,attempts,go,ledgers) VALUES (?,?,'attempt',0,0,0)", (token, study))
                old = db.execute('SELECT * FROM records WHERE token=?', (token,)).fetchone()
            if old['study'] != study:
                raise ValueError('request token study differs')
            if old['status'] != 'attempt':
                raise ValueError('request already consumed; no automatic retry')
            hit = db.execute('SELECT * FROM cache WHERE key=?', (cache_key,)).fetchone()
            if hit:
                db.execute("UPDATE records SET status='complete',cache_key=?,cache_token=? WHERE token=?", (cache_key, hit['token'], token))
                record = self._record(db.execute('SELECT * FROM records WHERE token=?', (token,)).fetchone())
                return {'send': False, 'response': strict_loads(hit['response']), 'record': record}
            self._charge(db, study, {'trial_attempts': 0, 'go_requests': 1, 'ledger_evaluations': ledgers})
            db.execute("UPDATE records SET status='reserved',go=1,ledgers=?,cache_key=? WHERE token=?", (ledgers, cache_key, token))
            return {'send': True, 'response': None, 'record': self._record(db.execute('SELECT * FROM records WHERE token=?', (token,)).fetchone())}

    def finish(self, token, response=None, error=None):
        encoded = canonical(response) if response is not None else None
        with self._transaction() as db:
            record = db.execute('SELECT * FROM records WHERE token=?', (token,)).fetchone()
            if not record or record['status'] not in {'attempt', 'reserved'}:
                raise ValueError('request already finished or unknown')
            if encoded is not None:
                if record['status'] != 'reserved' or error is not None:
                    raise ValueError('only reserved successful evaluations may enter cache')
                db.execute("UPDATE records SET status='complete' WHERE token=?", (token,))
                db.execute('INSERT OR IGNORE INTO cache VALUES (?,?,?)', (record['cache_key'], token, encoded))
            else:
                db.execute("UPDATE records SET status='failed',error=? WHERE token=?", (str(error or 'evaluation failed'), token))

    def recover(self, study):
        """Requires caller's substudy lock; consume unknown requests without retry."""
        with self._transaction() as db:
            changed = [r[0] for r in db.execute("SELECT token FROM records WHERE study=? AND status IN ('attempt','reserved') ORDER BY rowid", (study,))]
            db.execute("UPDATE records SET status='interrupted',error='unknown interrupted attempt; no replay' WHERE study=? AND status IN ('attempt','reserved')", (study,))
            return changed

    def halt(self, reason):
        with self._transaction() as db:
            db.execute("UPDATE metadata SET value=? WHERE key='halted'", (canonical(str(reason)),))

    def halted(self):
        with self._transaction() as db:
            return self._halted(db)

    def counts(self):
        with self._transaction() as db:
            rows = db.execute('SELECT * FROM totals').fetchall()
            by_name = {r['name']: dict(zip(COUNTERS, (r['attempts'], r['go'], r['ledgers']))) for r in rows}
            return {'counts': by_name.pop(''), 'substudy_counts': by_name}

    def records_for(self, study):
        with self._transaction() as db:
            return {r['token']: self._record(r) for r in db.execute('SELECT * FROM records WHERE study=? ORDER BY rowid', (study,))}

    def cached_response(self, cache_key):
        """Read one successful result without materializing the research cache."""
        with self._transaction() as db:
            r = db.execute('SELECT response FROM cache WHERE key=?', (cache_key,)).fetchone()
            return strict_loads(r[0]) if r is not None else None

    def snapshot(self, include_cache=True):
        with self._transaction() as db:
            rows = db.execute('SELECT * FROM totals').fetchall()
            counts = {r['name']: dict(zip(COUNTERS, (r['attempts'], r['go'], r['ledgers']))) for r in rows}
            records = {r['token']: self._record(r) for r in db.execute('SELECT * FROM records ORDER BY rowid')}
            cache = {r['key']: ({'token': r['token'], 'response': strict_loads(r['response'])} if include_cache else {'token': r['token']})
                     for r in db.execute(('SELECT * FROM cache ORDER BY rowid' if include_cache else 'SELECT key,token FROM cache ORDER BY rowid'))}
            return {'binding': deepcopy(self.binding), 'counts': counts.pop(''), 'substudy_counts': counts,
                    'records': records, 'cache': cache, 'halted': self._halted(db)}

    def record(self, token):
        with self._transaction() as db:
            return self._record(db.execute('SELECT * FROM records WHERE token=?', (token,)).fetchone())


def cache_identity(plan, config):
    return identity({'plan': plan, 'config': config})

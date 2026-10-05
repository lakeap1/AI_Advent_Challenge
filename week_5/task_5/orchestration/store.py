"""Durable run journal shared by three independent MCP processes."""
import json
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import time
from uuid import uuid4


class OrchestrationStore:
    def __init__(self, data_dir):
        self.root = Path(data_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'orchestration.sqlite3'
        self.results = self.root / 'orchestration' / 'results'
        self.results.mkdir(parents=True, exist_ok=True)
        if not self.results.resolve().is_relative_to(self.root):
            raise ValueError('Каталог результатов находится вне data_dir.')
        with self._db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS runs(
                id TEXT PRIMARY KEY, profile_id INTEGER NOT NULL, dialogue_id INTEGER NOT NULL,
                branch_id INTEGER NOT NULL, parent_request_id INTEGER NOT NULL,
                question TEXT NOT NULL, status TEXT NOT NULL, error TEXT,
                catalog TEXT NOT NULL, calls TEXT NOT NULL, model_steps TEXT NOT NULL,
                events TEXT NOT NULL, saved TEXT, verified INTEGER NOT NULL DEFAULT 0,
                started_at REAL NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS running_orchestration ON runs(profile_id,dialogue_id)
                WHERE status='running';
                CREATE TABLE IF NOT EXISTS artifacts(id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
                kind TEXT NOT NULL, data TEXT NOT NULL, FOREIGN KEY(run_id) REFERENCES runs(id));''')

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self, profile_id, dialogue_id, branch_id, question, parent_request_id):
        rid = uuid4().hex
        with self._db() as db:
            db.execute('INSERT INTO runs(id,profile_id,dialogue_id,branch_id,parent_request_id,question,status,catalog,calls,model_steps,events,started_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                       (rid,profile_id,dialogue_id,branch_id,parent_request_id,question,'running','[]','[]','[]','[]',time.time()))
        return self.get(rid)

    def get(self, rid):
        if not isinstance(rid, str) or len(rid) != 32 or any(c not in '0123456789abcdef' for c in rid):
            raise ValueError('Некорректный run_id.')
        with self._db() as db:
            row = db.execute('SELECT * FROM runs WHERE id=?', (rid,)).fetchone()
        if row is None:
            raise ValueError('Запуск не найден.')
        result = dict(row)
        for key in ('catalog','calls','model_steps','events','saved'):
            result[key] = json.loads(result[key]) if result[key] is not None else None
        result['verified'] = bool(result['verified'])
        return result

    def latest(self, profile_id, dialogue_id, branch_id):
        with self._db() as db:
            row = db.execute('SELECT id FROM runs WHERE profile_id=? AND dialogue_id=? AND branch_id=? ORDER BY started_at DESC LIMIT 1',
                             (profile_id,dialogue_id,branch_id)).fetchone()
        return self.get(row['id']) if row else None

    def runs(self, profile_id, dialogue_id, branch_id):
        with self._db() as db:
            rows = db.execute('SELECT id FROM runs WHERE profile_id=? AND dialogue_id=? AND branch_id=? ORDER BY started_at',
                              (profile_id,dialogue_id,branch_id)).fetchall()
        return [self.get(row['id']) for row in rows]

    def update(self, rid, **fields):
        allowed = {'catalog','calls','model_steps','events','saved','verified','status','error'}
        if not fields or not set(fields) <= allowed:
            raise ValueError('Недопустимое поле запуска.')
        values = [json.dumps(v, ensure_ascii=False) if k in ('catalog','calls','model_steps','events','saved') and v is not None else v for k,v in fields.items()]
        with self._db() as db:
            cursor = db.execute('UPDATE runs SET ' + ','.join(k+'=?' for k in fields) + ' WHERE id=?', (*values,rid))
            if cursor.rowcount != 1:
                raise ValueError('Запуск не найден.')

    def append(self, rid, field, item):
        if field not in ('calls','model_steps','events'):
            raise ValueError('Недопустимый журнал.')
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute(f'SELECT {field} FROM runs WHERE id=?', (rid,)).fetchone()
            if row is None:
                raise ValueError('Запуск не найден.')
            items = json.loads(row[field])
            items.append(item)
            db.execute(f'UPDATE runs SET {field}=? WHERE id=?', (json.dumps(items,ensure_ascii=False),rid))

    def update_call(self, rid, item):
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT calls FROM runs WHERE id=?', (rid,)).fetchone()
            if row is None:
                raise ValueError('Запуск не найден.')
            calls = json.loads(row['calls'])
            for index, call in enumerate(calls):
                if call['call_id'] == item['call_id']:
                    calls[index] = item
                    break
            else:
                calls.append(item)
            db.execute('UPDATE runs SET calls=? WHERE id=?', (json.dumps(calls,ensure_ascii=False),rid))

    def put_artifact(self, rid, kind, data):
        aid = uuid4().hex
        with self._db() as db:
            db.execute('INSERT INTO artifacts VALUES(?,?,?,?)', (aid,rid,kind,json.dumps(data,ensure_ascii=False)))
        return aid

    def artifact(self, rid, aid, kind):
        if not isinstance(aid, str):
            raise ValueError('Идентификатор материала повреждён.')
        with self._db() as db:
            row = db.execute('SELECT data FROM artifacts WHERE id=? AND run_id=? AND kind=?', (aid,rid,kind)).fetchone()
        if row is None:
            raise ValueError('Материал не принадлежит этому запуску или не готов.')
        return json.loads(row['data'])

    def recover(self):
        with self._db() as db:
            db.execute("UPDATE runs SET status='interrupted',error='Процесс прерван; автоматического повтора нет.' WHERE status='running'")

    def ensure_idle(self, profile_id, dialogue_id):
        with self._db() as db:
            row = db.execute("SELECT id FROM runs WHERE profile_id=? AND dialogue_id=? AND status='running'", (profile_id,dialogue_id)).fetchone()
        if row:
            raise ValueError('Дождитесь завершения текущего отчёта.')

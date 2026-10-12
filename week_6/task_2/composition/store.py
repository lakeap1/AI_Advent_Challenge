"""Durable run state plus one independent LLM receipt per run; no chat DB recovery."""
from contextlib import contextmanager
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

from .contracts import SearchInput, run_identifier
from agent.tokens import accounting


class CompositionStore:
    def __init__(self, data_dir):
        self.root = Path(data_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'composition.sqlite3'
        self.results = self.root / 'composition' / 'results'
        self.results.mkdir(parents=True, exist_ok=True)
        if not self.results.resolve().is_relative_to(self.root):
            raise ValueError('Каталог результатов находится вне каталога данных.')
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS runs(
                  id TEXT PRIMARY KEY, profile_id INTEGER NOT NULL, dialogue_id INTEGER NOT NULL,
                  question TEXT NOT NULL, query TEXT NOT NULL, started_at REAL NOT NULL,
                  status TEXT NOT NULL, stage TEXT NOT NULL, error TEXT,
                  materials TEXT, summary TEXT, saved TEXT, events TEXT NOT NULL, catalog TEXT);
                CREATE UNIQUE INDEX IF NOT EXISTS active_composition
                  ON runs(profile_id,dialogue_id) WHERE status='running';
                CREATE TABLE IF NOT EXISTS llm_calls(
                  run_id TEXT PRIMARY KEY REFERENCES runs(id), data TEXT NOT NULL);
            ''')
            if 'branch_id' not in {row['name'] for row in db.execute('PRAGMA table_info(runs)')}:
                db.execute('ALTER TABLE runs ADD COLUMN branch_id INTEGER NOT NULL DEFAULT 1')
            if 'parent_request_id' not in {row['name'] for row in db.execute('PRAGMA table_info(runs)')}:
                db.execute('ALTER TABLE runs ADD COLUMN parent_request_id INTEGER')
            if 'source' not in {row['name'] for row in db.execute('PRAGMA table_info(runs)')}:
                db.execute("ALTER TABLE runs ADD COLUMN source TEXT NOT NULL DEFAULT 'blender'")

    @contextmanager
    def db(self):
        with sqlite3.connect(self.path, timeout=10) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            yield db

    def create(self, profile_id, dialogue_id, question, query, branch_id=1, parent_request_id=None, *, source='blender'):
        # Validate source independently; empty text is retained for rejected input receipts.
        source = SearchInput(question='validation', query='validation', source=source).source
        rid = uuid4().hex
        with self.db() as db:
            try:
                db.execute('INSERT INTO runs(id,profile_id,dialogue_id,question,query,started_at,status,stage,events,branch_id,parent_request_id,source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                           (rid, profile_id, dialogue_id, question, query, time.time(), 'running', 'input', '[]', branch_id, parent_request_id, source))
            except sqlite3.IntegrityError:
                raise ValueError('В этом диалоге цепочка уже выполняется.') from None
        return self.get(rid)

    def get(self, rid):
        run_identifier(rid)
        with self.db() as db:
            row = db.execute('SELECT * FROM runs WHERE id=?', (rid,)).fetchone()
            call = db.execute('SELECT data FROM llm_calls WHERE run_id=?', (rid,)).fetchone()
        if row is None:
            raise ValueError('Запуск не найден.')
        result = dict(row)
        for key in ('materials', 'summary', 'saved', 'events', 'catalog'):
            result[key] = json.loads(result[key]) if result[key] is not None else None
        result['call'] = json.loads(call['data']) if call else None
        return result

    def put(self, rid, field, value):
        if field not in ('materials', 'summary', 'saved', 'catalog'):
            raise ValueError('Unsupported state field')
        with self.db() as db:
            db.execute(f'UPDATE runs SET {field}=? WHERE id=?', (json.dumps(value, ensure_ascii=False), rid))

    def event(self, rid, stage, status, detail=''):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT events FROM runs WHERE id=?', (rid,)).fetchone()
            events = json.loads(row['events'])
            events.append(dict(stage=stage, status=status, detail=detail, at=time.time()))
            final = status if status in ('success', 'error', 'rejected', 'interrupted') else 'running'
            db.execute('UPDATE runs SET stage=?,status=?,error=?,events=? WHERE id=?',
                       (stage, final, detail if final in ('error','rejected','interrupted') else None,
                        json.dumps(events, ensure_ascii=False), rid))

    def claim_call(self, rid, record):
        with self.db() as db:
            try:
                db.execute('INSERT INTO llm_calls VALUES(?,?)', (rid, json.dumps(record, ensure_ascii=False)))
            except sqlite3.IntegrityError:
                raise ValueError('Обработка этого запуска уже запрашивалась; повторный LLM-вызов запрещён.') from None

    def finish_call(self, rid, record):
        with self.db() as db:
            db.execute('UPDATE llm_calls SET data=? WHERE run_id=?', (json.dumps(record, ensure_ascii=False), rid))

    def calls(self, profile_id, dialogue_id):
        with self.db() as db:
            rows = db.execute('SELECT c.data FROM llm_calls c JOIN runs r ON r.id=c.run_id WHERE r.profile_id=? AND r.dialogue_id=? ORDER BY r.started_at',
                              (profile_id, dialogue_id)).fetchall()
        return [json.loads(row['data']) for row in rows]

    def latest(self, profile_id, dialogue_id):
        with self.db() as db:
            row = db.execute('SELECT id FROM runs WHERE profile_id=? AND dialogue_id=? ORDER BY started_at DESC LIMIT 1', (profile_id, dialogue_id)).fetchone()
        return self.get(row['id']) if row else None

    def runs(self, profile_id, dialogue_id):
        with self.db() as db:
            ids = [row['id'] for row in db.execute('SELECT id FROM runs WHERE profile_id=? AND dialogue_id=? ORDER BY started_at', (profile_id, dialogue_id))]
        return [self.get(rid) for rid in ids]

    def ensure_idle(self, profile_id, dialogue_id):
        if any(run['status'] == 'running' for run in self.runs(profile_id, dialogue_id)):
            raise ValueError('Дождитесь завершения памятки в этом диалоге.')

    def recover(self):
        with self.db() as db:
            ids = [r['id'] for r in db.execute("SELECT id FROM runs WHERE status='running'")]
        for rid in ids:
            self.event(rid, self.get(rid)['stage'], 'interrupted', 'Приложение перезапущено. Автоматического повтора нет; проверьте расход и каталог результатов.')

    def augment(self, state):
        if 'personalization' not in state or 'workspace' not in state:
            return state
        calls = self.calls(state['personalization']['selected_id'], state['workspace']['active_dialogue']['id'])
        imported = {r['metadata'].get('composition_run_id') for r in state.get('requests', [])}
        called = [c for c in calls if c['usage_status'] != 'not_requested' and c['run_id'] not in imported]
        base = state['summary']
        unknown = base['unknown_cost_requests'] + sum(c['cost_usd'] is None for c in called)
        total = Decimal(base['known_cost_usd']) + sum((Decimal(c['cost_usd']) for c in called if c['cost_usd'] is not None), Decimal(0))
        runs = self.runs(state['personalization']['selected_id'], state['workspace']['active_dialogue']['id'])
        for run in runs:
            if run['saved']:
                run['saved_path'] = str(self.results / run['saved']['filename'])
        return {**state, 'composition_runs': runs, 'composition_calls': calls,
                'token_accounting': accounting([*state.get('requests', []), *called]),
                'summary': dict(known_cost_usd=str(total), cost_complete=unknown == 0,
                    unknown_cost_requests=unknown, api_requests=base['api_requests'] + len(called))}

"""SQLite history and request receipts; unknown usage remains durable."""

import json
import sqlite3
from pathlib import Path


class LocalStore:
    def __init__(self, data_dir):
        directory = Path(data_dir)
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / 'local_chat.sqlite3'
        with self._connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS receipts (
                    id INTEGER PRIMARY KEY,
                    status TEXT NOT NULL,
                    error TEXT,
                    input_policy TEXT NOT NULL,
                    output_policy TEXT NOT NULL,
                    usage_status TEXT NOT NULL,
                    usage_json TEXT,
                    cost_usd TEXT,
                    pricing_json TEXT NOT NULL,
                    model TEXT NOT NULL,
                    service_mode TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY,
                    receipt_id INTEGER NOT NULL REFERENCES receipts(id),
                    role TEXT NOT NULL CHECK(role IN ('user','assistant')),
                    content TEXT NOT NULL
                );
            ''')

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def messages(self):
        with self._connect() as db:
            return [{'role': row['role'], 'content': row['content']}
                    for row in db.execute('SELECT role,content FROM messages ORDER BY id')]

    def receipts(self):
        with self._connect() as db:
            return [self._receipt(row) for row in db.execute('SELECT * FROM receipts ORDER BY id')]

    @staticmethod
    def _receipt(row):
        result = dict(row)
        result['usage'] = json.loads(result.pop('usage_json')) if result['usage_json'] else None
        result['pricing'] = json.loads(result.pop('pricing_json'))
        return result

    def blocked(self):
        with self._connect() as db:
            row = db.execute("SELECT status FROM receipts WHERE usage_status IN ('unknown','pending') LIMIT 1").fetchone()
            return row['status'] if row else None

    def record_rejection(self, model, service_mode, error, pricing):
        with self._connect() as db:
            cursor = db.execute('''INSERT INTO receipts
                (status,error,input_policy,output_policy,usage_status,model,service_mode,pricing_json)
                VALUES ('rejected',?,'rejected','not_run','not_requested',?,?,?)''',
                (error, model, service_mode, json.dumps(pricing, ensure_ascii=False)))
            receipt_id = cursor.lastrowid
        return self.receipt(receipt_id)

    def begin(self, model, service_mode, pricing):
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT 1 FROM receipts WHERE usage_status IN ('unknown','pending') LIMIT 1").fetchone()
            if row:
                return None
            cursor = db.execute('''INSERT INTO receipts
                (status,input_policy,output_policy,usage_status,model,service_mode,pricing_json)
                VALUES ('pending','accepted','pending','pending',?,?,?)''',
                (model, service_mode, json.dumps(pricing, ensure_ascii=False)))
            return cursor.lastrowid

    def finish(self, receipt_id, *, status, error, output_policy, usage, cost_usd, messages=()):
        usage_status = ('known' if usage is not None and
                        usage.get('input_tokens') is not None and
                        usage.get('output_tokens') is not None and
                        usage.get('total_tokens') is not None else 'unknown')
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('''UPDATE receipts SET status=?,error=?,output_policy=?,usage_status=?,
                usage_json=?,cost_usd=? WHERE id=?''',
                (status, error, output_policy, usage_status,
                 json.dumps(usage, ensure_ascii=False) if usage is not None else None,
                 cost_usd, receipt_id))
            for role, content in messages:
                db.execute('INSERT INTO messages(receipt_id,role,content) VALUES (?,?,?)',
                           (receipt_id, role, content))
        return self.receipt(receipt_id)

    def receipt(self, receipt_id):
        with self._connect() as db:
            row = db.execute('SELECT * FROM receipts WHERE id=?', (receipt_id,)).fetchone()
            return self._receipt(row)

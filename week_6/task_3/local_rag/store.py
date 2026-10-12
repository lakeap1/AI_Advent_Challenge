"""Durable receipts, published index, and separate answer history."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


class Store:
    def __init__(self, data_dir):
        self.path = Path(data_dir) / 'local_rag.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = RLock()
        with self._connection() as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS receipts (
                    id INTEGER PRIMARY KEY, created_at TEXT NOT NULL,
                    stage TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
                    status TEXT NOT NULL, metered INTEGER NOT NULL,
                    input_policy TEXT NOT NULL, output_policy TEXT,
                    usage TEXT, usage_status TEXT NOT NULL,
                    cost_usd REAL, elapsed_seconds REAL,
                    tariff TEXT NOT NULL, error_code TEXT
                );
                CREATE TABLE IF NOT EXISTS index_meta (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    id TEXT PRIMARY KEY, value TEXT NOT NULL, vector TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS history (
                    id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, value TEXT NOT NULL
                );
            ''')

    @contextmanager
    def _connection(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def start_receipt(self, stage, provider, model, input_policy, *, metered=True, tariff=None):
        with self.lock, self._connection() as conn:
            cursor = conn.execute('''INSERT INTO receipts
                (created_at, stage, provider, model, status, metered,
                 input_policy, usage_status, tariff)
                VALUES (?, ?, ?, ?, 'pending', ?, ?, 'pending', ?)''',
                (datetime.now(timezone.utc).isoformat(), stage, provider, model,
                 int(metered), _json(input_policy), _json(tariff or {})))
            return cursor.lastrowid

    def finish_receipt(self, receipt_id, *, status, output_policy, usage=None,
                       usage_status='complete', cost_usd=None, elapsed_seconds=None,
                       error_code=None):
        with self.lock, self._connection() as conn:
            conn.execute('''UPDATE receipts SET status=?, output_policy=?, usage=?,
                usage_status=?, cost_usd=?, elapsed_seconds=?, error_code=? WHERE id=?''',
                (status, _json(output_policy), _json(usage) if usage is not None else None,
                 usage_status, cost_usd, elapsed_seconds, error_code, receipt_id))
        return self.receipt(receipt_id)

    @staticmethod
    def _receipt(row):
        if row is None:
            return None
        value = dict(row)
        for key in ('input_policy', 'output_policy', 'usage', 'tariff'):
            value[key] = json.loads(value[key]) if value[key] is not None else None
        value['metered'] = bool(value['metered'])
        return value

    def receipt(self, receipt_id):
        with self.lock, self._connection() as conn:
            return self._receipt(conn.execute('SELECT * FROM receipts WHERE id=?', (receipt_id,)).fetchone())

    def receipts(self, limit=100):
        with self.lock, self._connection() as conn:
            rows = conn.execute('SELECT * FROM receipts ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
        return [self._receipt(row) for row in rows]

    def ledger(self):
        with self.lock, self._connection() as conn:
            rows = conn.execute('SELECT status, metered, usage_status, cost_usd FROM receipts').fetchall()
        incomplete = any(row['status'] == 'pending' or
                         (row['metered'] and (row['usage_status'] != 'complete' or row['cost_usd'] is None))
                         for row in rows)
        known = sum(row['cost_usd'] for row in rows if row['cost_usd'] is not None)
        return {'total_usd': known if not incomplete else None,
                'known_usd': known, 'incomplete': incomplete, 'receipt_count': len(rows)}

    def blocked(self):
        return self.ledger()['incomplete']

    def publish_index(self, metadata, rows):
        # Only a fully validated batch reaches this transaction.
        with self.lock, self._connection() as conn:
            conn.execute('DELETE FROM chunks')
            conn.executemany('INSERT INTO chunks (id, value, vector) VALUES (?, ?, ?)',
                             ((chunk['chunk_id'], _json(chunk), _json(vector)) for chunk, vector in rows))
            conn.execute('INSERT OR REPLACE INTO index_meta (singleton, value) VALUES (1, ?)', (_json(metadata),))

    def index(self):
        with self.lock, self._connection() as conn:
            row = conn.execute('SELECT value FROM index_meta WHERE singleton=1').fetchone()
        return json.loads(row['value']) if row else None

    def chunks(self):
        with self.lock, self._connection() as conn:
            rows = conn.execute('SELECT value, vector FROM chunks').fetchall()
        return [(json.loads(row['value']), json.loads(row['vector'])) for row in rows]

    def add_history(self, value):
        with self.lock, self._connection() as conn:
            conn.execute('INSERT INTO history (created_at, value) VALUES (?, ?)',
                         (datetime.now(timezone.utc).isoformat(), _json(value)))

    def history(self, limit=30):
        with self.lock, self._connection() as conn:
            rows = conn.execute('SELECT id, created_at, value FROM history ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
        return [{'id': row['id'], 'created_at': row['created_at'], **json.loads(row['value'])} for row in rows]

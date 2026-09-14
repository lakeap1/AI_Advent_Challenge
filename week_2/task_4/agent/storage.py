"""Транзакционное хранилище одного локального чата в SQLite."""

from contextlib import contextmanager
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
from threading import RLock
from uuid import uuid4


DEFAULT_DB_PATH = Path(__file__).resolve().parents[1] / 'data' / 'chat.sqlite3'


class StorageError(RuntimeError):
    """Историю не удалось прочитать или надёжно сохранить."""


class SQLiteStore:
    """Один экземпляр/процесс на БД; методы безопасны для потоков этого экземпляра."""

    def __init__(self, db_path=None):
        self._lock = RLock()
        self._connection = None
        path = DEFAULT_DB_PATH if db_path is None else db_path
        try:
            if str(path) != ':memory:':
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(str(path), timeout=5, check_same_thread=False)
            self._connection.row_factory = sqlite3.Row
            self._connection.execute('PRAGMA foreign_keys = ON')
            self._connection.execute('PRAGMA synchronous = FULL')
            with self._transaction() as connection:
                connection.execute('CREATE TABLE IF NOT EXISTS chat (id INTEGER PRIMARY KEY CHECK (id=1), chat_id TEXT NOT NULL)')
                connection.execute('''CREATE TABLE IF NOT EXISTS requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, status TEXT NOT NULL,
                    text TEXT NOT NULL, code TEXT NOT NULL, usage TEXT,
                    cost_usd TEXT, usage_status TEXT NOT NULL,
                    input_policy TEXT NOT NULL, output_policy TEXT NOT NULL,
                    metadata TEXT NOT NULL)''')
                connection.execute('''CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    request_id INTEGER NOT NULL REFERENCES requests(id))''')
                connection.execute("""CREATE TABLE IF NOT EXISTS context_memory (
                    id INTEGER PRIMARY KEY CHECK(id=1), mode TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '', covered_through INTEGER NOT NULL DEFAULT 0,
                    revisions INTEGER NOT NULL DEFAULT 0)""")
                connection.execute('INSERT OR IGNORE INTO chat (id, chat_id) VALUES (1, ?)', (str(uuid4()),))
                connection.execute('''UPDATE requests SET status='interrupted', code='interrupted',
                    text=?, usage=NULL, cost_usd=NULL, usage_status='unavailable'
                    WHERE status='pending' ''', (
                    'Запрос прерван до сохранения результата. Расход неизвестен; автоматический повтор не выполнялся.',))
        except (sqlite3.Error, OSError, StorageError) as exc:
            if self._connection is not None:
                self._connection.close()
            raise StorageError('Не удалось открыть или восстановить базу чата.') from exc

    @contextmanager
    def _transaction(self):
        with self._lock:
            try:
                with self._connection:
                    yield self._connection
            except sqlite3.Error as exc:
                raise StorageError('Не удалось прочитать или сохранить данные чата.') from exc

    @staticmethod
    def _values(record):
        return (
            record['status'], record['text'], record['code'],
            json.dumps(record['usage'], ensure_ascii=False) if record['usage'] is not None else None,
            record['cost_usd'], record['usage_status'],
            json.dumps(record['input_policy'], ensure_ascii=False),
            json.dumps(record['output_policy'], ensure_ascii=False),
            json.dumps(record['metadata'], ensure_ascii=False),
        )

    def begin(self, record, user_text=None):
        """Commit записи запроса и допустимого user до обращения к провайдеру."""
        with self._transaction() as connection:
            cursor = connection.execute('''INSERT INTO requests
                (status,text,code,usage,cost_usd,usage_status,input_policy,output_policy,metadata)
                VALUES (?,?,?,?,?,?,?,?,?)''', self._values(record))
            request_id = cursor.lastrowid
            if user_text is not None:
                connection.execute('INSERT INTO messages (role,content,request_id) VALUES (?,?,?)',
                                   ('user', user_text, request_id))
        return request_id

    def finish(self, request_id, record, assistant_text=None, memory=None):
        """Результат и принятый assistant сохраняются одной транзакцией."""
        with self._transaction() as connection:
            cursor = connection.execute('''UPDATE requests SET
                status=?,text=?,code=?,usage=?,cost_usd=?,usage_status=?,input_policy=?,output_policy=?,metadata=?
                WHERE id=? AND status='pending' ''', (*self._values(record), request_id))
            if cursor.rowcount != 1:
                raise StorageError('Начатый запрос отсутствует или уже завершён.')
            if memory is not None:
                connection.execute("UPDATE context_memory SET summary=?, covered_through=?, revisions=revisions+1 WHERE id=1",
                                   (memory['summary'], memory['covered_through']))
            if assistant_text is not None:
                connection.execute('INSERT INTO messages (role,content,request_id) VALUES (?,?,?)',
                                   ('assistant', assistant_text, request_id))

    def configure_context(self, mode):
        with self._transaction() as connection:
            connection.execute("INSERT OR IGNORE INTO context_memory(id,mode) VALUES(1,?)", (mode,))
            current = connection.execute("SELECT mode FROM context_memory WHERE id=1").fetchone()['mode']
            if current != mode:
                raise StorageError("Режим базы уже задан. Для другого режима используйте отдельную базу.")

    def memory(self):
        with self._transaction() as connection:
            row = connection.execute("SELECT mode,summary,covered_through,revisions FROM context_memory WHERE id=1").fetchone()
        return dict(row) if row else {'mode': 'full', 'summary': '', 'covered_through': 0, 'revisions': 0}

    def state(self):
        with self._transaction() as connection:
            # Read transaction gives a coherent snapshot of requests and messages.
            connection.execute('BEGIN')
            chat_id = connection.execute('SELECT chat_id FROM chat WHERE id=1').fetchone()['chat_id']
            messages = [dict(row) for row in connection.execute('SELECT * FROM messages ORDER BY id')]
            requests = [dict(row) for row in connection.execute('SELECT * FROM requests ORDER BY id')]
        try:
            for record in requests:
                for key in ('usage', 'input_policy', 'output_policy', 'metadata'):
                    record[key] = json.loads(record[key]) if record[key] is not None else None
            called = [record for record in requests if record['usage_status'] != 'not_requested']
            unknown = sum(record['cost_usd'] is None for record in called)
            total = sum((Decimal(record['cost_usd']) for record in called if record['cost_usd'] is not None), Decimal(0))
        except (ValueError, TypeError, ArithmeticError) as exc:
            raise StorageError('Некорректные данные в журнале чата.') from exc
        return {'chat_id': chat_id, 'messages': messages, 'requests': requests, 'summary': {
            'known_cost_usd': format(total, 'f'), 'cost_complete': unknown == 0,
            'unknown_cost_requests': unknown, 'api_requests': len(called),
        }}

    def close(self):
        with self._lock:
            try:
                self._connection.close()
            except sqlite3.Error as exc:
                raise StorageError('Не удалось закрыть базу чата.') from exc

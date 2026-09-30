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
                connection.execute('''CREATE TABLE IF NOT EXISTS retrievals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id INTEGER NOT NULL REFERENCES requests(id),
                    provider TEXT NOT NULL, query TEXT NOT NULL,
                    status TEXT NOT NULL, sources TEXT NOT NULL,
                    metadata TEXT NOT NULL, error TEXT NOT NULL)''')
                connection.execute("""CREATE TABLE IF NOT EXISTS context_memory (
                    id INTEGER PRIMARY KEY CHECK(id=1), mode TEXT NOT NULL,
                    facts TEXT NOT NULL DEFAULT '{}', active_branch INTEGER NOT NULL DEFAULT 1,
                    revisions INTEGER NOT NULL DEFAULT 0)""")
                connection.execute('CREATE TABLE IF NOT EXISTS branches (id INTEGER PRIMARY KEY, name TEXT NOT NULL)')
                connection.execute('CREATE TABLE IF NOT EXISTS branch_messages (branch_id INTEGER REFERENCES branches(id), message_id INTEGER REFERENCES messages(id), PRIMARY KEY(branch_id,message_id))')
                connection.execute('CREATE TABLE IF NOT EXISTS checkpoints (id INTEGER PRIMARY KEY, name TEXT NOT NULL, message_ids TEXT NOT NULL)')
                connection.execute("INSERT OR IGNORE INTO branches(id,name) VALUES(1,'Основная')")
                connection.execute('INSERT OR IGNORE INTO chat (id, chat_id) VALUES (1, ?)', (str(uuid4()),))
                connection.execute('''UPDATE requests SET status='interrupted', code='interrupted',
                    text=?
                    WHERE status='pending' ''', (
                    'Запрос прерван до принятия результата. Доступный расход сохранён; отсутствующие данные неизвестны. Автоматический повтор не выполнялся.',))
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
            record['status'], '' if record['status'] == 'ok' else record['text'], record['code'],
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
                self._append(connection, 'user', user_text, request_id)
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
                connection.execute("UPDATE context_memory SET facts=?, revisions=revisions+1 WHERE id=1",
                                   (json.dumps(memory, ensure_ascii=False),))
            if assistant_text is not None:
                self._append(connection, 'assistant', assistant_text, request_id)

    def configure_context(self, mode):
        with self._transaction() as connection:
            connection.execute("INSERT OR IGNORE INTO context_memory(id,mode) VALUES(1,?)", (mode,))
            current = connection.execute("SELECT mode FROM context_memory WHERE id=1").fetchone()['mode']
            if current != mode:
                raise StorageError("Режим базы уже задан. Для другого режима используйте отдельную базу.")

    def pending_metadata(self, request_id, metadata):
        """Зафиксировать состав фактического запроса до сетевого вызова."""
        with self._transaction() as connection:
            connection.execute("UPDATE requests SET metadata=? WHERE id=? AND status='pending'",
                               (json.dumps(metadata, ensure_ascii=False), request_id))

    def save_retrieval(self, request_id, result):
        """Persist each selected tool outcome independently of LLM usage."""
        with self._transaction() as connection:
            connection.execute('''INSERT INTO retrievals
                (request_id,provider,query,status,sources,metadata,error)
                VALUES (?,?,?,?,?,?,?)''',
                (request_id, result['provider'], result['query'], result['status'],
                 json.dumps(result['sources'], ensure_ascii=False),
                 json.dumps(result['metadata'], ensure_ascii=False), result['error']))

    def mark_requested(self, request_id):
        with self._transaction() as connection:
            connection.execute("UPDATE requests SET usage_status='unavailable' WHERE id=? AND status='pending'", (request_id,))

    def extraction_committed(self, request_id, metadata):
        """Добавить ссылки после принятия отложенных операций памяти."""
        with self._transaction() as connection:
            connection.execute("UPDATE requests SET metadata=? WHERE id=? AND status='ok'",
                               (json.dumps(metadata, ensure_ascii=False), request_id))

    def pending_result(self, request_id, record):
        """Сохранить известный расход генерации до отдельной проверки, без текста ответа."""
        with self._transaction() as connection:
            connection.execute('''UPDATE requests SET usage=?, cost_usd=?, usage_status=?, metadata=?
                WHERE id=? AND status='pending' ''',
                (json.dumps(record['usage']) if record['usage'] is not None else None,
                 record['cost_usd'], record['usage_status'], json.dumps(record['metadata'], ensure_ascii=False), request_id))

    @staticmethod
    def _append(connection, role, text, request_id):
        cursor = connection.execute('INSERT INTO messages(role,content,request_id) VALUES(?,?,?)', (role,text,request_id))
        branch = connection.execute('SELECT active_branch FROM context_memory WHERE id=1').fetchone()[0]
        connection.execute('INSERT INTO branch_messages VALUES(?,?)', (branch,cursor.lastrowid))

    def memory(self):
        with self._transaction() as connection:
            row = dict(connection.execute('SELECT * FROM context_memory WHERE id=1').fetchone())
        row['facts'] = json.loads(row['facts'])
        return row

    @staticmethod
    def _name(name):
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            raise ValueError('Название должно содержать от 1 до 80 символов.')
        try: name.encode("utf-8")
        except UnicodeEncodeError: raise ValueError("Некорректный Unicode в названии.") from None
        return name.strip()

    def checkpoint(self, name):
        name = self._name(name)
        with self._transaction() as connection:
            ids = [r[0] for r in connection.execute('SELECT message_id FROM branch_messages WHERE branch_id=(SELECT active_branch FROM context_memory WHERE id=1) ORDER BY message_id')]
            cursor = connection.execute('INSERT INTO checkpoints(name,message_ids) VALUES(?,?)', (name,json.dumps(ids)))
            return {'id': cursor.lastrowid, 'name': name}

    def branch(self, checkpoint_id, name):
        name = self._name(name)
        if type(checkpoint_id) is not int:
            raise ValueError('Неверный checkpoint.')
        with self._transaction() as connection:
            cp = connection.execute('SELECT message_ids FROM checkpoints WHERE id=?', (checkpoint_id,)).fetchone()
            if cp is None:
                raise ValueError('Checkpoint не найден.')
            cursor = connection.execute('INSERT INTO branches(name) VALUES(?)', (name,))
            branch_id = cursor.lastrowid
            connection.executemany('INSERT INTO branch_messages VALUES(?,?)', [(branch_id,i) for i in json.loads(cp[0])])
            connection.execute('UPDATE context_memory SET active_branch=? WHERE id=1', (branch_id,))
            return {'id': branch_id, 'name': name}

    def switch(self, branch_id):
        if type(branch_id) is not int:
            raise ValueError('Неверная ветка.')
        with self._transaction() as connection:
            if connection.execute('SELECT id FROM branches WHERE id=?', (branch_id,)).fetchone() is None:
                raise ValueError('Ветка не найдена.')
            connection.execute('UPDATE context_memory SET active_branch=? WHERE id=1', (branch_id,))

    def state(self):
        with self._transaction() as connection:
            # Read transaction gives a coherent snapshot of requests and messages.
            connection.execute('BEGIN')
            chat_id = connection.execute('SELECT chat_id FROM chat WHERE id=1').fetchone()['chat_id']
            messages = [dict(row) for row in connection.execute('SELECT m.* FROM messages m JOIN branch_messages b ON m.id=b.message_id WHERE b.branch_id=(SELECT active_branch FROM context_memory WHERE id=1) ORDER BY m.id')]
            branches = [dict(row) for row in connection.execute('SELECT * FROM branches ORDER BY id')]
            checkpoints = [dict(row) for row in connection.execute('SELECT id,name FROM checkpoints ORDER BY id')]
            active_branch = connection.execute('SELECT active_branch FROM context_memory WHERE id=1').fetchone()[0]
            requests = [dict(row) for row in connection.execute('SELECT * FROM requests ORDER BY id')]
            retrievals = [dict(row) for row in connection.execute('SELECT * FROM retrievals ORDER BY id')]
        try:
            for record in requests:
                for key in ('usage', 'input_policy', 'output_policy', 'metadata'):
                    record[key] = json.loads(record[key]) if record[key] is not None else None
            for record in retrievals:
                record['sources'] = json.loads(record['sources'])
                record['metadata'] = json.loads(record['metadata'])
            called = [record for record in requests if record['usage_status'] != 'not_requested']
            unknown = sum(record['cost_usd'] is None for record in called)
            total = sum((Decimal(record['cost_usd']) for record in called if record['cost_usd'] is not None), Decimal(0))
        except (ValueError, TypeError, ArithmeticError) as exc:
            raise StorageError('Некорректные данные в журнале чата.') from exc
        return {'branches': branches, 'checkpoints': checkpoints, 'active_branch': active_branch, 'chat_id': chat_id, 'messages': messages, 'requests': requests, 'retrievals': retrievals, 'summary': {
            'known_cost_usd': format(total, 'f'), 'cost_complete': unknown == 0,
            'unknown_cost_requests': unknown, 'api_requests': len(called),
        }}

    def close(self):
        with self._lock:
            try:
                self._connection.close()
            except sqlite3.Error as exc:
                raise StorageError('Не удалось закрыть базу чата.') from exc

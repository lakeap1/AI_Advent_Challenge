"""Задачи, диалоги и версионные записи явной памяти одного пользователя."""
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from threading import RLock

from .storage import StorageError


def text_field(value, name, limit, *, empty=False):
    if not isinstance(value, str) or (not empty and not value.strip()) or len(value.strip()) > limit:
        raise ValueError(f'{name}: требуется текст длиной до {limit} символов.')
    try:
        value.encode('utf-8')
    except UnicodeEncodeError:
        raise ValueError(f'{name}: некорректный Unicode.') from None
    return value.strip()


def positive_id(value):
    if type(value) is not int or value <= 0:
        raise ValueError('Некорректный идентификатор.')
    return value


class MemoryStore:
    def __init__(self, path):
        self._lock = RLock()
        self._transaction_depth = 0
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(str(path), check_same_thread=False)
            self._db.row_factory = sqlite3.Row
            self._db.execute('PRAGMA foreign_keys=ON')
            self._db.execute('PRAGMA synchronous=FULL')
            with self.transaction() as db:
                db.executescript('''
                    CREATE TABLE IF NOT EXISTS tasks(id INTEGER PRIMARY KEY, name TEXT NOT NULL, project TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS dialogues(id INTEGER PRIMARY KEY, name TEXT NOT NULL,
                        task_id INTEGER NOT NULL REFERENCES tasks(id), mode TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS settings(id INTEGER PRIMARY KEY CHECK(id=1), active_dialogue INTEGER REFERENCES dialogues(id));
                    CREATE TABLE IF NOT EXISTS memory_entries(id INTEGER PRIMARY KEY, layer TEXT NOT NULL,
                        scope TEXT NOT NULL, owner TEXT NOT NULL, key TEXT NOT NULL, revision INTEGER,
                        active INTEGER NOT NULL DEFAULT 1, locked INTEGER NOT NULL DEFAULT 0,
                        UNIQUE(layer,scope,owner,key));
                    CREATE TABLE IF NOT EXISTS memory_versions(revision INTEGER PRIMARY KEY,
                        entry_id INTEGER NOT NULL REFERENCES memory_entries(id), data TEXT NOT NULL);
                ''')
        except (sqlite3.Error, OSError) as exc:
            raise StorageError('Не удалось открыть базу памяти.') from exc

    @contextmanager
    def transaction(self):
        with self._lock:
            try:
                outer = self._transaction_depth == 0
                self._transaction_depth += 1
                with self._db if outer else nullcontext():
                    yield self._db
            except sqlite3.Error as exc:
                raise StorageError('Не удалось сохранить память.') from exc
            finally:
                self._transaction_depth -= 1

    @staticmethod
    def _mode(mode):
        if mode not in ('sliding', 'facts', 'branching'):
            raise ValueError('Неизвестная стратегия контекста.')
        return mode

    def _task(self, db, task_id):
        row = db.execute('SELECT * FROM tasks WHERE id=?', (positive_id(task_id),)).fetchone()
        if row is None:
            raise ValueError('Задача не найдена.')
        return dict(row)

    def create_task(self, name, project='', mode='sliding'):
        name = text_field(name, 'Название', 80)
        project = text_field(project, 'Проект', 80, empty=True)
        self._mode(mode)
        with self.transaction() as db:
            task_id = db.execute('INSERT INTO tasks(name,project) VALUES(?,?)', (name, project)).lastrowid
            return self._dialogue(db, name, task_id, mode)

    def _dialogue(self, db, name, task_id, mode):
        self._task(db, task_id)
        cursor = db.execute('INSERT INTO dialogues(name,task_id,mode) VALUES(?,?,?)', (name, task_id, mode))
        db.execute('INSERT OR REPLACE INTO settings(id,active_dialogue) VALUES(1,?)', (cursor.lastrowid,))
        return dict(id=cursor.lastrowid, name=name, task_id=task_id, mode=mode)

    def create_dialogue(self, name, task_id, mode):
        name = text_field(name, 'Название диалога', 80)
        self._mode(mode)
        with self.transaction() as db:
            return self._dialogue(db, name, task_id, mode)

    def open_dialogue(self, dialogue_id):
        with self.transaction() as db:
            if not db.execute('SELECT id FROM dialogues WHERE id=?', (positive_id(dialogue_id),)).fetchone():
                raise ValueError('Диалог не найден.')
            db.execute('UPDATE settings SET active_dialogue=? WHERE id=1', (dialogue_id,))

    def workspace(self):
        with self.transaction() as db:
            dialogues = [dict(r) for r in db.execute('SELECT * FROM dialogues ORDER BY id')]
            tasks = [dict(r) for r in db.execute('SELECT * FROM tasks ORDER BY id')]
            row = db.execute('SELECT active_dialogue FROM settings WHERE id=1').fetchone()
            active = next((d for d in dialogues if row and d['id'] == row[0]), None)
        return dict(user_id='local', dialogues=dialogues, tasks=tasks, active_dialogue=active,
                    layers=self.layers(active['task_id']) if active else dict(working=[], long_term=[]))

    def _validated(self, db, task_id, data):
        task = self._task(db, task_id)
        layer, category, scope = (data.get(k) for k in ('layer', 'category', 'scope'))
        if layer == 'working' and category == 'context' and scope == 'task':
            owner = str(task_id)
        elif layer == 'long_term' and category in ('profile', 'decision', 'knowledge') and scope in ('user', 'project'):
            owner = 'local' if scope == 'user' else task['project']
            if not owner:
                raise ValueError('Для области project задайте проект при создании задачи.')
        else:
            raise ValueError('Недопустимое сочетание слоя, категории и области.')
        return dict(layer=layer, category=category, scope=scope, owner=owner,
                    key=text_field(data.get('key'), 'Ключ', 80),
                    value=text_field(data.get('value'), 'Значение', 1000),
                    reason=text_field(data.get('reason'), 'Причина', 300),
                    evidence=text_field(data.get('evidence', ''), 'Цитата', 1000, empty=True))

    def _save(self, db, item, source, locked, *, active=True):
        identity = tuple(item[k] for k in ('layer', 'scope', 'owner', 'key'))
        row = db.execute('SELECT * FROM memory_entries WHERE layer=? AND scope=? AND owner=? AND key=?', identity).fetchone()
        if row is None:
            eid = db.execute('INSERT INTO memory_entries(layer,scope,owner,key) VALUES(?,?,?,?)', identity).lastrowid
        else:
            eid = row['id']
        record = {**item, 'id': eid, 'source': source, 'locked': bool(locked), 'active': bool(active),
                  'created_at': datetime.now(timezone.utc).isoformat(), 'user_id': 'local'}
        revision = db.execute('INSERT INTO memory_versions(entry_id,data) VALUES(?,?)',
                              (eid, json.dumps(record, ensure_ascii=False))).lastrowid
        db.execute('UPDATE memory_entries SET revision=?,active=?,locked=? WHERE id=?', (revision, active, locked, eid))
        count = db.execute('SELECT count(*) FROM memory_entries WHERE layer=? AND scope=? AND owner=? AND active=1', identity[:3]).fetchone()[0]
        if count > 24:
            raise ValueError('В одной области допускается максимум 24 активных записи. Удалите ненужные.')
        return {**record, 'revision': revision}

    def save(self, task_id, data):
        with self.transaction() as db:
            return self._save(db, self._validated(db, task_id, data), 'manual', True)

    def _accessible(self, db, task_id, entry_id):
        task = self._task(db, task_id)
        row = db.execute('SELECT v.data,e.revision FROM memory_entries e JOIN memory_versions v ON e.revision=v.revision WHERE e.id=?', (positive_id(entry_id),)).fetchone()
        if row is None:
            raise ValueError('Запись не найдена.')
        item = {**json.loads(row['data']), 'revision': row['revision']}
        owners = {'task': str(task_id), 'user': 'local', 'project': task['project']}
        if item['owner'] != owners[item['scope']]:
            raise ValueError('Запись относится к другой задаче или проекту.')
        return item

    def deactivate(self, task_id, entry_id):
        with self.transaction() as db:
            item = self._accessible(db, task_id, entry_id)
            return self._save(db, item, 'manual', True, active=False)

    def unlock(self, task_id, entry_id):
        with self.transaction() as db:
            item = self._accessible(db, task_id, entry_id)
            return self._save(db, item, 'manual', False, active=item['active'])

    def move(self, task_id, entry_id, target):
        with self.transaction() as db:
            item = self._accessible(db, task_id, entry_id)
            destination = self._validated(db, task_id, {**item, **target})
            result = self._save(db, destination, 'manual', True)
            if result['id'] != entry_id:
                self._save(db, item, 'manual', True, active=False)
            return result

    def layers(self, task_id):
        with self.transaction() as db:
            task = self._task(db, task_id)
            rows = db.execute('''SELECT v.data,e.revision FROM memory_entries e
                JOIN memory_versions v ON v.revision=e.revision WHERE e.active=1 AND
                ((e.scope='task' AND e.owner=?) OR (e.scope='user' AND e.owner='local') OR
                 (e.scope='project' AND e.owner=? AND e.owner<>'')) ORDER BY e.id''', (str(task_id), task['project'])).fetchall()
        layers = dict(working=[], long_term=[])
        for row in rows:
            item = {**json.loads(row['data']), 'revision': row['revision']}
            layers[item['layer']].append(item)
        return layers

    def resolve(self, references):
        with self.transaction() as db:
            result = []
            for ref in references:
                row = db.execute('SELECT data FROM memory_versions WHERE entry_id=? AND revision=?', (ref['id'], ref['revision'])).fetchone()
                if row is None:
                    raise StorageError('Версия записи памяти не найдена.')
                result.append({**json.loads(row[0]), 'revision': ref['revision']})
            return result

    def apply_operations(self, task_id, operations, source):
        with self.transaction() as db:
            # Validate every item before the first write, then commit the complete batch.
            items = [(op, self._validated(db, task_id, op)) for op in operations]
            changed, skipped = [], []
            for op, item in items:
                row = db.execute('SELECT * FROM memory_entries WHERE layer=? AND scope=? AND owner=? AND key=?',
                                 tuple(item[k] for k in ('layer', 'scope', 'owner', 'key'))).fetchone()
                if row and row['locked']:
                    skipped.append(dict(id=row['id'], key=item['key'], reason='Закреплено пользователем'))
                    continue
                if op['op'] == 'delete' and row is None:
                    continue
                changed.append(self._save(db, {**item, 'source_ref': source}, 'auto', False, active=op['op'] != 'delete'))
            return changed, skipped

    def close(self):
        with self._lock:
            self._db.close()

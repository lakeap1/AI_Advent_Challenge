"""Каталог локальных профилей; каждый владеет своим рабочим пространством."""
from contextlib import contextmanager
from pathlib import Path
import sqlite3
from threading import RLock

from agent.memory import positive_id, text_field
from agent.personalization import ProfileMemoryStore, validate_preferences
from agent.storage import StorageError
from workspace import Workspace


class ProfileWorkspace:
    def __init__(self, data_dir, transport=None):
        self.lock = RLock()
        self.data_dir = Path(data_dir)
        self.transport = transport
        self._workspaces = {}
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(str(self.data_dir / 'profiles.sqlite3'), check_same_thread=False)
            self._db.row_factory = sqlite3.Row
            self._db.execute('PRAGMA foreign_keys=ON')
            with self._transaction() as db:
                db.executescript('''CREATE TABLE IF NOT EXISTS profiles(
                    id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
                    CREATE TABLE IF NOT EXISTS selection(id INTEGER PRIMARY KEY CHECK(id=1),
                    profile_id INTEGER NOT NULL REFERENCES profiles(id));''')
            if not self._db.execute('SELECT 1 FROM profiles').fetchone():
                self.create_profile(dict(name='Основной'))
        except (OSError, sqlite3.Error) as exc:
            raise StorageError('Не удалось открыть каталог профилей.') from exc

    @contextmanager
    def _transaction(self):
        with self.lock:
            try:
                with self._db:
                    yield self._db
            except sqlite3.Error as exc:
                raise StorageError('Не удалось сохранить каталог профилей.') from exc

    def _selected(self):
        with self._transaction() as db:
            return dict(db.execute('SELECT p.* FROM profiles p JOIN selection s ON p.id=s.profile_id WHERE s.id=1').fetchone())

    def _workspace(self, profile_id=None):
        pid = self._selected()['id'] if profile_id is None else profile_id
        if pid not in self._workspaces:
            self._workspaces[pid] = Workspace(self.data_dir / 'profiles' / str(pid), self.transport,
                                              memory_class=ProfileMemoryStore)
        return self._workspaces[pid]

    @property
    def memory(self):
        return self._workspace().memory

    def agent(self):
        agent = self._workspace().agent()
        agent.profile_id = self._selected()['id']
        return agent

    def state(self):
        with self.lock:
            profile = self._selected()
            with self._transaction() as db:
                profiles = [dict(row) for row in db.execute('SELECT * FROM profiles ORDER BY id')]
            return {**self._workspace().state(), 'personalization': dict(
                profiles=profiles, selected_id=profile['id'], profile={**profile, **self.memory.profile()})}

    def create_profile(self, data):
        name = text_field(data.get('name'), 'Название профиля', 80)
        values = validate_preferences(data)
        with self._transaction() as db:
            if db.execute('SELECT 1 FROM profiles WHERE name=?', (name,)).fetchone():
                raise ValueError('Профиль с таким названием уже существует.')
            pid = db.execute('INSERT INTO profiles(name) VALUES(?)', (name,)).lastrowid
            workspace = self._workspace(pid)
            task_id = workspace.memory.workspace()['active_dialogue']['task_id']
            workspace.memory.edit_profile(task_id, values)
            db.execute('INSERT OR REPLACE INTO selection VALUES(1,?)', (pid,))
        return pid

    def select_profile(self, profile_id):
        positive_id(profile_id)
        with self._transaction() as db:
            if not db.execute('SELECT 1 FROM profiles WHERE id=?', (profile_id,)).fetchone():
                raise ValueError('Профиль не найден.')
            self._workspace(profile_id)
            db.execute('UPDATE selection SET profile_id=? WHERE id=1', (profile_id,))

    def edit_profile(self, data):
        with self.lock:
            task = self.memory.workspace()['active_dialogue']['task_id']
            return self.memory.edit_profile(task, data)

    def close(self):
        with self.lock:
            for workspace in self._workspaces.values():
                workspace.close()
            self._db.close()

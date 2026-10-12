"""Dialogue ledger adapter for local retrieval stages beside their answer."""

import json

from agent.conversation_state import empty_state
from agent.storage import SQLiteStore


class RagSQLiteStore(SQLiteStore):
    def __init__(self, db_path):
        super().__init__(db_path)
        with self._transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS dialogue_kind (
                id INTEGER PRIMARY KEY CHECK(id=1), kind TEXT NOT NULL
                CHECK(kind IN ('ordinary','formal')))""")
            db.execute("""CREATE TABLE IF NOT EXISTS conversation_branches (
                branch_id INTEGER PRIMARY KEY REFERENCES branches(id),
                state_json TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS conversation_snapshots (
                message_id INTEGER PRIMARY KEY REFERENCES messages(id),
                state_json TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS conversation_checkpoints (
                checkpoint_id INTEGER PRIMARY KEY REFERENCES checkpoints(id),
                state_json TEXT NOT NULL)""")
            db.execute('INSERT OR IGNORE INTO conversation_branches VALUES(1,?)',
                       (json.dumps(empty_state(), ensure_ascii=False),))
        self.rag_mode = None
        self.parent_request_id = None
        self.parent_metadata = None

    def dialogue_kind(self):
        with self._transaction() as db:
            row = db.execute('SELECT kind FROM dialogue_kind WHERE id=1').fetchone()
        return row['kind'] if row else None

    def set_dialogue_kind(self, kind):
        if kind not in ('ordinary', 'formal'):
            raise ValueError('Неизвестный вид диалога.')
        with self._transaction() as db:
            db.execute('INSERT OR REPLACE INTO dialogue_kind(id,kind) VALUES(1,?)', (kind,))

    def conversation_state(self):
        with self._transaction() as db:
            branch = db.execute('SELECT active_branch FROM context_memory WHERE id=1').fetchone()[0]
            row = db.execute('SELECT state_json FROM conversation_branches WHERE branch_id=?',
                             (branch,)).fetchone()
        return json.loads(row['state_json']) if row else empty_state()

    def save_conversation_state(self, state, *, expected_revision, request_id):
        """CAS state and user-turn snapshot in one SQLite transaction."""
        with self._transaction() as db:
            db.execute('BEGIN IMMEDIATE')
            branch = db.execute('SELECT active_branch FROM context_memory WHERE id=1').fetchone()[0]
            row = db.execute('SELECT state_json FROM conversation_branches WHERE branch_id=?',
                             (branch,)).fetchone()
            before = json.loads(row['state_json']) if row else empty_state()
            if before['revision'] != expected_revision:
                raise ValueError('Состояние диалога изменилось во время подготовки.')
            serialized = json.dumps(state, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
            db.execute('INSERT OR REPLACE INTO conversation_branches VALUES(?,?)', (branch, serialized))
            message = db.execute("SELECT id FROM messages WHERE request_id=? AND role='user'",
                                 (request_id,)).fetchone()
            if message:
                db.execute('INSERT OR REPLACE INTO conversation_snapshots VALUES(?,?)',
                           (message['id'], serialized))

    def checkpoint(self, name, *, message_id=None):
        name = self._name(name)
        with self._transaction() as db:
            db.execute('BEGIN IMMEDIATE')
            branch = db.execute('SELECT active_branch FROM context_memory WHERE id=1').fetchone()[0]
            ids = [row[0] for row in db.execute(
                'SELECT message_id FROM branch_messages WHERE branch_id=? ORDER BY message_id', (branch,))]
            if message_id is not None:
                if type(message_id) is not int or message_id not in ids:
                    raise ValueError('Сообщение не принадлежит активной ветке.')
                ids = ids[:ids.index(message_id) + 1]
            checkpoint_id = db.execute('INSERT INTO checkpoints(name,message_ids) VALUES(?,?)',
                (name, json.dumps(ids))).lastrowid
            if message_id is None:
                state = db.execute('SELECT state_json FROM conversation_branches WHERE branch_id=?',
                    (branch,)).fetchone()['state_json']
            else:
                state_row = db.execute('SELECT state_json FROM conversation_snapshots WHERE message_id<=? '
                    'AND message_id IN (SELECT message_id FROM branch_messages WHERE branch_id=?) '
                    'ORDER BY message_id DESC LIMIT 1', (message_id, branch)).fetchone()
                state = state_row['state_json'] if state_row else json.dumps(empty_state(), ensure_ascii=False)
            db.execute('INSERT INTO conversation_checkpoints VALUES(?,?)', (checkpoint_id, state))
        return {'id': checkpoint_id, 'name': name}

    def branch(self, checkpoint_id, name):
        name = self._name(name)
        if type(checkpoint_id) is not int:
            raise ValueError('Неверный checkpoint.')
        with self._transaction() as db:
            db.execute('BEGIN IMMEDIATE')
            cp = db.execute('SELECT message_ids FROM checkpoints WHERE id=?', (checkpoint_id,)).fetchone()
            if cp is None:
                raise ValueError('Checkpoint не найден.')
            state_row = db.execute('SELECT state_json FROM conversation_checkpoints WHERE checkpoint_id=?',
                                   (checkpoint_id,)).fetchone()
            state = state_row['state_json'] if state_row else json.dumps(empty_state(), ensure_ascii=False)
            branch_id = db.execute('INSERT INTO branches(name) VALUES(?)', (name,)).lastrowid
            db.executemany('INSERT INTO branch_messages VALUES(?,?)',
                           [(branch_id, mid) for mid in json.loads(cp['message_ids'])])
            db.execute('INSERT INTO conversation_branches VALUES(?,?)', (branch_id, state))
            db.execute('UPDATE context_memory SET active_branch=? WHERE id=1', (branch_id,))
        return {'id': branch_id, 'name': name}

    def state(self):
        state = super().state()
        state['dialogue_kind'] = self.dialogue_kind() or 'formal'
        state['conversation_state'] = self.conversation_state()
        return state

    def begin(self, record, user_text=None):
        if record['metadata'].get('kind') == 'answer' and self.rag_mode is not None:
            record['metadata']['rag'] = {
                'mode': self.rag_mode,
                'status': ('pending' if record['status'] == 'pending' else 'not_requested')
                    if self.rag_mode != 'plain' else 'disabled',
                'sources': [], 'used_sources': [], 'grounding': None,
                'context': '', 'context_budget': None,
                'original_query': user_text, 'search_query': user_text,
                'candidates': [], 'counts': {'candidates': 0, 'passed': 0, 'selected': 0},
                'rewrite_request_id': None, 'embedding_request_id': None,
                'preparation_request_id': None,
                'filter_request_id': None, 'base_generation_payload': None,
                'base_generation_sha256': None,
            }
            if record['status'] == 'pending':
                record['usage_status'] = 'not_requested'
            request_id = super().begin(record, user_text)
            self.parent_request_id = request_id
            self.parent_metadata = record['metadata']
            return request_id
        return super().begin(record, user_text)

    def mark_requested(self, request_id):
        # guarded.run_guarded marks the answer before tool_loop; query retrieval
        # must finish before the generation call becomes billable.
        if self.rag_mode is not None and request_id == self.parent_request_id:
            return
        super().mark_requested(request_id)

    def mark_generation_requested(self, request_id):
        super().mark_requested(request_id)

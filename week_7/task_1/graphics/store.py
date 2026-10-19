"""Owned SQLite state, point-in-time branches, FSM, and durable call receipts."""

import copy
import hashlib
import hmac
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

from .context import validate_explicit


def _id() -> str:
    return uuid.uuid4().hex


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _read(value):
    return json.loads(value)


def _safe_turn_details(details):
    """Persist identifiers only; draft text must never enter a readable receipt."""
    if not isinstance(details, dict):
        return {}
    result = {}
    for key in ("call_ids", "source_ids", "check_codes"):
        value = details.get(key)
        if isinstance(value, list) and all(isinstance(item, str) and len(item) <= 128 for item in value):
            result[key] = value
    return result


_STEPS = {
    "new": {"input_check"},
    "input_check": {"context", "input_rejected", "error"},
    "context": {"sources", "needs_clarification", "compressed", "error"},
    "sources": {"draft", "needs_clarification", "error"},
    "draft": {"program_check", "error"},
    "program_check": {"semantic_check", "rejected", "error"},
    "semantic_check": {"accepted", "rejected", "error"},
}
_TERMINAL = {"input_rejected", "needs_clarification", "rejected", "error", "accepted", "compressed"}
_PROFILE_FIELDS = {"role", "level", "goal", "depth"}
_BRANCH_FIELDS = {"title", "summary", "summary_through", "working_state", "conditions"}
_CALL_STATES = {
    "preparation": {"context"},
    "summary": {"context"},
    "relevance": {"sources"},
    "filter": {"sources"},
    "embedding": {"sources"},
    "generator": {"draft"},
    "validator": {"semantic_check"},
}
_SAFE_TERMINAL_ERRORS = {
    "input_rejected": "Вопрос не прошёл проверку",
    "needs_clarification": "Нужны дополнительные сведения",
    "rejected": "Ответ не прошёл проверку",
    "error": "Не удалось завершить запрос",
    "accepted": None,
    "compressed": None,
}


class Store:
    def __init__(self, path: str | Path, *, explicit_budget: int = 4000):
        self.path = Path(path)
        self.explicit_budget = explicit_budget
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE,
                    salt BLOB NOT NULL, password_hash BLOB NOT NULL, profile TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, key TEXT NOT NULL,
                    value TEXT NOT NULL, UNIQUE(user_id,key)
                );
                CREATE TABLE IF NOT EXISTS chats (
                    id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, title TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS branches (
                    id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, user_id INTEGER NOT NULL,
                    title TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '',
                    summary_through INTEGER NOT NULL DEFAULT 0,
                    working_state TEXT NOT NULL DEFAULT '{}', conditions TEXT NOT NULL DEFAULT '{}',
                    revision INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY, branch_id TEXT NOT NULL, user_id INTEGER NOT NULL,
                    ordinal INTEGER NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
                    sources TEXT NOT NULL, snapshot TEXT NOT NULL, turn_id TEXT,
                    UNIQUE(branch_id,ordinal)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_published_answer_per_turn
                    ON messages(turn_id) WHERE role='assistant' AND turn_id IS NOT NULL;
                CREATE TABLE IF NOT EXISTS turns (
                    id TEXT PRIMARY KEY, branch_id TEXT NOT NULL, user_id INTEGER NOT NULL,
                    question TEXT NOT NULL, status TEXT NOT NULL, details TEXT NOT NULL,
                    error TEXT, events TEXT NOT NULL, accepted_snapshot TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_turn_per_branch
                    ON turns(branch_id) WHERE status NOT IN
                    ('input_rejected','needs_clarification','rejected','error','accepted','compressed');
                CREATE TABLE IF NOT EXISTS calls (
                    id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, branch_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL, turn_id TEXT NOT NULL, stage TEXT NOT NULL,
                    model TEXT NOT NULL, status TEXT NOT NULL, usage TEXT,
                    cost_estimated_usd REAL, metadata TEXT NOT NULL,
                    input_policy TEXT, output_policy TEXT,
                    request_dispatched INTEGER NOT NULL DEFAULT 0
                );
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(turns)")}
            if "accepted_snapshot" not in columns:
                db.execute("ALTER TABLE turns ADD COLUMN accepted_snapshot TEXT")
            call_columns = {row["name"] for row in db.execute("PRAGMA table_info(calls)")}
            if "request_dispatched" not in call_columns:
                db.execute("ALTER TABLE calls ADD COLUMN request_dispatched INTEGER NOT NULL DEFAULT 0")
            active_index = db.execute("SELECT sql FROM sqlite_master WHERE name='one_active_turn_per_branch'").fetchone()
            if active_index is not None and "compressed" not in active_index["sql"]:
                db.execute("DROP INDEX one_active_turn_per_branch")
                db.execute("CREATE UNIQUE INDEX one_active_turn_per_branch ON turns(branch_id) "
                           "WHERE status NOT IN ('input_rejected','needs_clarification','rejected',"
                           "'error','accepted','compressed')")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _hash_password(password: str, salt: bytes) -> bytes:
        return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)

    def create_user(self, username: str, password: str) -> int:
        if not isinstance(username, str) or not username.strip() or not isinstance(password, str) or len(password) < 8:
            raise ValueError("Нужны имя пользователя и пароль не короче 8 символов")
        salt = os.urandom(16)
        digest = self._hash_password(password, salt)
        with self._connect() as db:
            try:
                row = db.execute("INSERT INTO users(username,salt,password_hash,profile) VALUES(?,?,?,?)",
                                 (username.strip(), salt, digest, _json({})))
            except sqlite3.IntegrityError as exc:
                raise ValueError("Имя пользователя занято") from exc
            return row.lastrowid

    def authenticate(self, username: str, password: str) -> int | None:
        with self._connect() as db:
            row = db.execute("SELECT id,salt,password_hash FROM users WHERE username=?", (username,)).fetchone()
        if row is None or not isinstance(password, str):
            return None
        digest = self._hash_password(password, row["salt"])
        return row["id"] if hmac.compare_digest(digest, row["password_hash"]) else None

    def _require_user(self, db, user_id):
        if db.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone() is None:
            raise PermissionError("Доступ запрещён")

    def _require_branch(self, db, user_id, branch_id):
        row = db.execute("SELECT * FROM branches WHERE id=?", (branch_id,)).fetchone()
        if row is None:
            raise PermissionError("Доступ запрещён")
        return row

    def get_profile(self, user_id: int) -> dict:
        with self._connect() as db:
            row = db.execute("SELECT profile FROM users WHERE id=?", (user_id,)).fetchone()
        if row is None:
            raise PermissionError("Доступ запрещён")
        return _read(row["profile"])

    def _admit_explicit(self, db, user_id, *, branch=None, profile=None, memories=None):
        """Called inside the same BEGIN IMMEDIATE transaction as each mutation."""
        if profile is None:
            profile = _read(db.execute("SELECT profile FROM users WHERE id=?", (user_id,)).fetchone()["profile"])
        if memories is None:
            memories = [dict(row) for row in db.execute(
                "SELECT id,key,value FROM memories WHERE user_id=? ORDER BY key", (user_id,))]
        if branch is not None:
            branches = [branch]
        else:
            branches = [{"conditions": _read(row["conditions"]),
                         "working_state": _read(row["working_state"])}
                        for row in db.execute("SELECT conditions,working_state FROM branches WHERE user_id=?", (user_id,))]
            branches.append({})  # A future new chat must also remain usable.
        for proposed in branches:
            validate_explicit(proposed, profile, memories, self.explicit_budget)

    def update_profile(self, user_id: int, changes: dict) -> dict:
        if not isinstance(changes, dict) or not set(changes) <= _PROFILE_FIELDS:
            raise ValueError("Недопустимое поле профиля")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._require_user(db, user_id)
            old = _read(db.execute("SELECT profile FROM users WHERE id=?", (user_id,)).fetchone()["profile"])
            old.update(changes)
            self._admit_explicit(db, user_id, profile=old)
            db.execute("UPDATE users SET profile=? WHERE id=?", (_json(old), user_id))
        return old

    def list_memories(self, user_id: int) -> list[dict]:
        with self._connect() as db:
            self._require_user(db, user_id)
            rows = db.execute("SELECT id,key,value FROM memories WHERE user_id=? ORDER BY key", (user_id,)).fetchall()
        return [dict(row) for row in rows]

    def remember(self, user_id: int, key: str, value: str) -> dict:
        if not isinstance(key, str) or not key.strip() or not isinstance(value, str) or not value.strip():
            raise ValueError("Пустую память сохранить нельзя")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._require_user(db, user_id)
            existing = db.execute("SELECT id FROM memories WHERE user_id=? AND key=?", (user_id, key.strip())).fetchone()
            memory_id = existing["id"] if existing else _id()
            proposed = {"id": memory_id, "key": key.strip(), "value": value.strip()}
            memories = [dict(row) for row in db.execute(
                "SELECT id,key,value FROM memories WHERE user_id=? AND key!=? ORDER BY key", (user_id, key.strip()))]
            self._admit_explicit(db, user_id, memories=sorted(memories + [proposed], key=lambda m: m["key"]))
            db.execute("INSERT INTO memories(id,user_id,key,value) VALUES(?,?,?,?) "
                       "ON CONFLICT(user_id,key) DO UPDATE SET value=excluded.value",
                       (memory_id, user_id, key.strip(), value.strip()))
        return {"id": memory_id, "key": key.strip(), "value": value.strip()}

    def forget(self, user_id: int, memory_id: str) -> None:
        with self._connect() as db:
            self._require_user(db, user_id)
            result = db.execute("DELETE FROM memories WHERE id=? AND user_id=?", (memory_id, user_id))
            if result.rowcount != 1:
                raise PermissionError("Доступ запрещён")

    def create_chat(self, user_id: int, title: str = "Новый разговор") -> dict:
        if not isinstance(title, str) or not title.strip():
            raise ValueError("Название чата пустое")
        chat_id, branch_id = _id(), _id()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._require_user(db, user_id)
            self._admit_explicit(db, user_id, branch={})
            db.execute("INSERT INTO chats(id,user_id,title) VALUES(?,?,?)", (chat_id, user_id, title.strip()))
            db.execute("INSERT INTO branches(id,chat_id,user_id,title) VALUES(?,?,?,?)",
                       (branch_id, chat_id, user_id, title.strip()))
        return {"id": chat_id, "branch_id": branch_id, "title": title.strip()}

    def list_chats(self, user_id: int) -> list[dict]:
        with self._connect() as db:
            self._require_user(db, user_id)
            rows = db.execute("SELECT c.id,c.title,(SELECT b.id FROM branches b WHERE b.chat_id=c.id "
                              "ORDER BY rowid LIMIT 1) AS branch_id FROM chats c WHERE c.user_id=? ORDER BY c.rowid DESC",
                              (user_id,)).fetchall()
        return [dict(row) for row in rows]

    def _branch_dict(self, db, row):
        messages = db.execute("SELECT id,role,content,sources,snapshot,turn_id FROM messages "
                              "WHERE branch_id=? AND user_id=? ORDER BY ordinal", (row["id"], row["user_id"])).fetchall()
        return {"id": row["id"], "chat_id": row["chat_id"], "title": row["title"],
                "summary": row["summary"], "summary_through": row["summary_through"],
                "working_state": _read(row["working_state"]), "conditions": _read(row["conditions"]),
                "revision": row["revision"], "messages": [
                    {"id": m["id"], "role": m["role"], "content": m["content"],
                     "sources": _read(m["sources"]), "snapshot": _read(m["snapshot"]),
                     "turn_id": m["turn_id"]} for m in messages]}

    def get_branch(self, user_id: int, branch_id: str) -> dict:
        with self._connect() as db:
            row = self._require_branch(db, user_id, branch_id)
            return self._branch_dict(db, row)

    def update_branch(self, user_id: int, branch_id: str, changes: dict,
                      turn_id: str | None = None) -> dict:
        if not isinstance(changes, dict) or not set(changes) <= _BRANCH_FIELDS:
            raise ValueError("Недопустимое поле ветки")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._require_branch(db, user_id, branch_id)
            active = db.execute("SELECT id FROM turns WHERE branch_id=? AND user_id=? AND status NOT IN "
                                "('input_rejected','needs_clarification','rejected','error','accepted','compressed')",
                                (branch_id, user_id)).fetchone()
            if active is not None and turn_id is None:
                raise RuntimeError("Ветка занята текущим ответом")
            if (turn_id is not None and (active is None or active["id"] != turn_id)):
                raise PermissionError("Доступ запрещён")
            current = self._branch_dict(db, row)
            current.update(copy.deepcopy(changes))
            if not isinstance(current["summary"], str) or not isinstance(current["title"], str):
                raise ValueError("Некорректное текстовое поле")
            if not isinstance(current["conditions"], dict) or not isinstance(current["working_state"], dict):
                raise ValueError("Состояние ветки должно быть объектом")
            if not isinstance(current["summary_through"], int) or not 0 <= current["summary_through"] <= len(current["messages"]):
                raise ValueError("Некорректная граница сводки")
            self._admit_explicit(db, user_id, branch=current)
            db.execute("UPDATE branches SET title=?,summary=?,summary_through=?,working_state=?,conditions=?,revision=revision+1 "
                       "WHERE id=? AND user_id=?", (current["title"], current["summary"],
                       current["summary_through"], _json(current["working_state"]),
                       _json(current["conditions"]), branch_id, user_id))
        return self.get_branch(user_id, branch_id)

    def append_message(self, user_id: int, branch_id: str, role: str, content: str,
                       sources=None, turn_id: str | None = None) -> dict:
        if role not in {"user", "assistant"} or not isinstance(content, str) or not content.strip():
            raise ValueError("Некорректная реплика")
        if sources is None:
            sources = []
        if not isinstance(sources, list):
            raise ValueError("Источники должны быть списком")
        if role == "assistant":
            raise PermissionError("Публикация ответа требует атомарного принятия")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._require_branch(db, user_id, branch_id)
            if turn_id:
                turn = db.execute("SELECT status FROM turns WHERE id=? AND user_id=? AND branch_id=?",
                                  (turn_id, user_id, branch_id)).fetchone()
                if turn is None:
                    raise PermissionError("Доступ запрещён")
            count = db.execute("SELECT COUNT(*) FROM messages WHERE branch_id=?", (branch_id,)).fetchone()[0]
            snapshot = {"title": row["title"], "summary": row["summary"],
                        "summary_through": row["summary_through"],
                        "working_state": _read(row["working_state"]),
                        "conditions": _read(row["conditions"])}
            message_id = _id()
            try:
                db.execute("INSERT INTO messages(id,branch_id,user_id,ordinal,role,content,sources,snapshot,turn_id) "
                           "VALUES(?,?,?,?,?,?,?,?,?)", (message_id, branch_id, user_id, count,
                           role, content.strip(), _json(sources), _json(snapshot), turn_id))
            except sqlite3.IntegrityError as exc:
                raise RuntimeError("Ответ этого хода уже опубликован") from exc
            db.execute("UPDATE branches SET revision=revision+1 WHERE id=?", (branch_id,))
        return {"id": message_id, "role": role, "content": content.strip(),
                "sources": copy.deepcopy(sources), "snapshot": snapshot, "turn_id": turn_id}

    def publish_answer(self, user_id: int, branch_id: str, turn_id: str,
                       content: str, sources=None) -> dict:
        """Atomically accept and publish after the service's program/semantic gates.

        Keeping the active-turn lock until commit prevents a following question
        from building context before this answer exists. No HTTP route exposes it.
        """
        if not isinstance(content, str) or not content.strip() or (sources is not None and not isinstance(sources, list)):
            raise ValueError("Некорректный ответ")
        sources = sources if sources is not None else []
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            branch = self._require_branch(db, user_id, branch_id)
            turn = db.execute("SELECT * FROM turns WHERE id=? AND user_id=? AND branch_id=?",
                              (turn_id, user_id, branch_id)).fetchone()
            if turn is None:
                raise PermissionError("Доступ запрещён")
            if turn["status"] != "semantic_check":
                raise ValueError("Ответ не готов к принятию или уже опубликован")
            snapshot = {"title": branch["title"], "summary": branch["summary"],
                        "summary_through": branch["summary_through"],
                        "working_state": _read(branch["working_state"]),
                        "conditions": _read(branch["conditions"])}
            message_id = _id()
            count = db.execute("SELECT COUNT(*) FROM messages WHERE branch_id=?", (branch_id,)).fetchone()[0]
            db.execute("UPDATE turns SET status='accepted',error=NULL,events=?,accepted_snapshot=? WHERE id=?",
                       (_json(_read(turn["events"]) + ["accepted"]), _json(snapshot), turn_id))
            db.execute("INSERT INTO messages(id,branch_id,user_id,ordinal,role,content,sources,snapshot,turn_id) "
                       "VALUES(?,?,?,?,?,?,?,?,?)", (message_id, branch_id, user_id, count, "assistant",
                       content.strip(), _json(sources), _json(snapshot), turn_id))
            db.execute("UPDATE branches SET revision=revision+1 WHERE id=?", (branch_id,))
        return {"id": message_id, "role": "assistant", "content": content.strip(),
                "sources": copy.deepcopy(sources), "snapshot": snapshot, "turn_id": turn_id}

    def fork_branch(self, user_id: int, branch_id: str, message_id: str) -> dict:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            parent = self._require_branch(db, user_id, branch_id)
            anchor = db.execute("SELECT ordinal,snapshot FROM messages WHERE id=? AND branch_id=? AND user_id=?",
                                (message_id, branch_id, user_id)).fetchone()
            if anchor is None:
                raise PermissionError("Доступ запрещён")
            snap = _read(anchor["snapshot"])
            self._admit_explicit(db, user_id, branch=snap)
            new_id = _id()
            db.execute("INSERT INTO branches(id,chat_id,user_id,title,summary,summary_through,working_state,conditions) "
                       "VALUES(?,?,?,?,?,?,?,?)", (new_id, parent["chat_id"], user_id, snap["title"],
                       snap["summary"], min(snap["summary_through"], anchor["ordinal"] + 1),
                       _json(snap["working_state"]), _json(snap["conditions"])))
            prefix = db.execute("SELECT ordinal,role,content,sources,snapshot FROM messages "
                                "WHERE branch_id=? AND user_id=? AND ordinal<=? ORDER BY ordinal",
                                (branch_id, user_id, anchor["ordinal"])).fetchall()
            for message in prefix:
                db.execute("INSERT INTO messages(id,branch_id,user_id,ordinal,role,content,sources,snapshot) "
                           "VALUES(?,?,?,?,?,?,?,?)", (_id(), new_id, user_id, message["ordinal"],
                           message["role"], message["content"], message["sources"], message["snapshot"]))
        return self.get_branch(user_id, new_id)

    def begin_turn(self, user_id: int, branch_id: str, question: str) -> dict:
        if not isinstance(question, str) or not question.strip():
            raise ValueError("Вопрос пустой")
        turn_id = _id()
        with self._connect() as db:
            self._require_branch(db, user_id, branch_id)
            try:
                db.execute("INSERT INTO turns(id,branch_id,user_id,question,status,details,events) "
                           "VALUES(?,?,?,?,?,?,?)", (turn_id, branch_id, user_id, question.strip(),
                           "new", _json({}), _json(["new"])))
            except sqlite3.IntegrityError as exc:
                raise RuntimeError("В этой ветке уже готовится ответ") from exc
        return {"id": turn_id, "status": "new"}

    def _turn(self, db, turn_id):
        row = db.execute("SELECT * FROM turns WHERE id=?", (turn_id,)).fetchone()
        if row is None:
            raise PermissionError("Доступ запрещён")
        return row

    def move_turn(self, turn_id: str, state: str, details=None) -> dict:
        if state in _TERMINAL:
            raise ValueError("Используйте end_turn для терминального исхода")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = self._turn(db, turn_id)
            if state not in _STEPS.get(old["status"], set()):
                raise ValueError("Недопустимый переход состояния")
            events = _read(old["events"]) + [state]
            db.execute("UPDATE turns SET status=?,details=?,events=? WHERE id=?",
                       (state, _json(_safe_turn_details(details)), _json(events), turn_id))
        return {"id": turn_id, "status": state}

    def end_turn(self, turn_id: str, state: str, error: str | None = None) -> dict:
        if state not in _TERMINAL:
            raise ValueError("Нужен терминальный исход")
        if state == "accepted":
            raise ValueError("Принятие требует атомарной публикации ответа")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = self._turn(db, turn_id)
            if state not in _STEPS.get(old["status"], set()):
                raise ValueError("Недопустимый переход состояния")
            events = _read(old["events"]) + [state]
            db.execute("UPDATE turns SET status=?,error=?,events=? WHERE id=?",
                       (state, _SAFE_TERMINAL_ERRORS[state], _json(events), turn_id))
        return {"id": turn_id, "status": state}

    def get_turn(self, user_id: int, turn_id: str) -> dict:
        with self._connect() as db:
            row = db.execute("SELECT id,branch_id,question,status,details,error,events FROM turns "
                             "WHERE id=? AND user_id=?", (turn_id, user_id)).fetchone()
        if row is None:
            raise PermissionError("Доступ запрещён")
        return {"id": row["id"], "branch_id": row["branch_id"], "question": row["question"],
                "status": row["status"], "details": _read(row["details"]),
                "error": row["error"], "events": _read(row["events"])}

    def start_call(self, user_id: int, branch_id: str, turn_id: str, stage: str,
                   model: str, metadata=None, reserve_usd: float = 0,
                   budget_usd: float = 1.0) -> str:
        call_id = _id()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            branch = self._require_branch(db, user_id, branch_id)
            turn = db.execute("SELECT status FROM turns WHERE id=? AND user_id=? AND branch_id=?",
                              (turn_id, user_id, branch_id)).fetchone()
            if turn is None:
                raise PermissionError("Доступ запрещён")
            if stage not in _CALL_STATES or turn["status"] not in _CALL_STATES[stage]:
                raise ValueError("Платный вызов недопустим на этом этапе")
            if stage == "embedding" and model != "qwen3-embedding:0.6b":
                raise ValueError("Стадия embeddings разрешена только локальной Qwen")
            if reserve_usd < 0 or budget_usd <= 0:
                raise ValueError("Некорректный бюджет")
            self._check_budget_in_transaction(db, reserve_usd, budget_usd)
            db.execute("INSERT INTO calls(id,user_id,branch_id,chat_id,turn_id,stage,model,status,metadata) "
                       "VALUES(?,?,?,?,?,?,?,?,?)", (call_id, user_id, branch_id, branch["chat_id"],
                       turn_id, stage, model, "pending", _json(metadata or {})))
        return call_id

    def mark_call_dispatched(self, call_id: str) -> None:
        """Irreversibly mark admission to the network boundary before transport.

        A lost response can never be downgraded to a free, unsent attempt.
        """
        with self._connect() as db:
            changed = db.execute(
                "UPDATE calls SET request_dispatched=1 WHERE id=? AND status='pending' "
                "AND request_dispatched=0", (call_id,)
            ).rowcount
            if changed != 1:
                raise ValueError("Квитанция уже отправлена или завершена")

    def finish_call(self, call_id: str, usage, cost_estimated_usd,
                    status: str = "complete", metadata=None) -> None:
        if status not in {"complete", "unknown", "rejected", "not_sent"}:
            raise ValueError("Некорректный статус вызова")
        if status == "not_sent" and not (
            usage is None and cost_estimated_usd == 0.0
            and isinstance(metadata, dict) and metadata.get("api_request_sent") is False
        ):
            raise ValueError("Неотправленная попытка требует подтверждения и не имеет model usage")
        if status == "complete" and (not isinstance(usage, dict) or cost_estimated_usd is None):
            raise ValueError("Полный вызов требует usage и стоимость")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status,metadata,request_dispatched FROM calls WHERE id=?", (call_id,)).fetchone()
            if row is None or row["status"] != "pending":
                raise ValueError("Квитанция не ожидает завершения")
            if status == "not_sent" and row["request_dispatched"]:
                raise ValueError("Отправленный вызов не может стать неотправленной попыткой")
            merged = _read(row["metadata"])
            merged.update(metadata or {})
            db.execute("UPDATE calls SET status=?,usage=?,cost_estimated_usd=?,metadata=? WHERE id=?",
                       (status, _json(usage) if usage is not None else None,
                        cost_estimated_usd, _json(merged), call_id))

    def set_call_policy(self, call_id: str, input_policy=None, output_policy=None) -> None:
        with self._connect() as db:
            row = db.execute("SELECT id FROM calls WHERE id=?", (call_id,)).fetchone()
            if row is None:
                raise ValueError("Квитанция не найдена")
            db.execute("UPDATE calls SET input_policy=COALESCE(?,input_policy), "
                       "output_policy=COALESCE(?,output_policy) WHERE id=?",
                       (_json(input_policy) if input_policy is not None else None,
                        _json(output_policy) if output_policy is not None else None, call_id))

    def ledger(self, user_id: int, chat_id: str | None = None) -> dict:
        with self._connect() as db:
            self._require_user(db, user_id)
            if chat_id is not None:
                if db.execute("SELECT 1 FROM chats WHERE id=? AND user_id=?", (chat_id, user_id)).fetchone() is None:
                    raise PermissionError("Доступ запрещён")
            sql = "SELECT * FROM calls WHERE user_id=?"
            args = (user_id,)
            if chat_id is not None:
                sql += " AND chat_id=?"
                args += (chat_id,)
            rows = db.execute(sql + " ORDER BY rowid", args).fetchall()
        calls = [{"id": r["id"], "branch_id": r["branch_id"], "chat_id": r["chat_id"],
                  "turn_id": r["turn_id"], "stage": r["stage"], "model": r["model"],
                  "status": r["status"], "usage": _read(r["usage"]) if r["usage"] else None,
                  "request_dispatched": bool(r["request_dispatched"]),
                  "cost_estimated_usd": r["cost_estimated_usd"], "metadata": _read(r["metadata"]),
                  "input_policy": _read(r["input_policy"]) if r["input_policy"] else None,
                  "output_policy": _read(r["output_policy"]) if r["output_policy"] else None}
                 for r in rows]
        complete = all(c["status"] == "not_sent" or (
            c["status"] in {"complete", "rejected"} and c["cost_estimated_usd"] is not None
            and c["usage"] is not None) for c in calls)
        known_cost = sum(c["cost_estimated_usd"] or 0.0 for c in calls)
        return {"calls": calls, "complete": complete,
                "known_cost_estimated_usd": known_cost,
                "cost_estimated_usd": known_cost if complete else None,
                "input_tokens": sum(c["usage"].get("input_tokens", 0) for c in calls if c["usage"]),
                "output_tokens": sum(c["usage"].get("output_tokens", 0) for c in calls if c["usage"]),
                "total_tokens": sum(c["usage"].get("total_tokens", 0) for c in calls if c["usage"])}

    def check_budget(self, user_id: int, reserve_usd: float = 0, budget_usd: float = 1.0) -> None:
        if reserve_usd < 0 or budget_usd <= 0:
            raise ValueError("Некорректный бюджет")
        with self._connect() as db:
            self._require_user(db, user_id)
            self._check_budget_in_transaction(db, reserve_usd, budget_usd)

    @staticmethod
    def _check_budget_in_transaction(db, reserve_usd: float, budget_usd: float) -> None:
        receipts = db.execute("SELECT status,usage,cost_estimated_usd FROM calls").fetchall()
        if any(r["status"] != "not_sent" and (
               r["status"] not in {"complete", "rejected"} or r["usage"] is None
               or r["cost_estimated_usd"] is None) for r in receipts):
            raise RuntimeError("Стоимость предыдущего вызова неизвестна; платные вызовы остановлены")
        spent = sum(r["cost_estimated_usd"] for r in receipts)
        if spent + reserve_usd > budget_usd:
            raise RuntimeError("Достигнут лимит расходов")

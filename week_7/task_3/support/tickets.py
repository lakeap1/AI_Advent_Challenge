"""Actor-scoped support tickets with atomic, replayable SQLite receipts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import tempfile
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .domain import Actor, SupportError


_INITIALIZE_LOCK = threading.RLock()
_ACTOR_RE = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_KEY_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
_TABLES = frozenset({
    "support_metadata",
    "support_tickets",
    "support_messages",
    "support_operations",
})
_COLUMNS = {
    "support_metadata": ("key", "value"),
    "support_tickets": ("id", "actor_id", "title", "status", "created_at", "updated_at"),
    "support_messages": ("sequence", "id", "ticket_id", "body", "created_at"),
    "support_operations": (
        "operation_id", "actor_id", "request_key", "operation", "ticket_id",
        "fingerprint", "committed_at", "result_json",
    ),
}
_SCHEMA = """
CREATE TABLE support_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE support_tickets (
    id TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open', 'closed_by_user')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX support_tickets_actor ON support_tickets(actor_id, created_at DESC, id DESC);
CREATE TABLE support_messages (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    ticket_id TEXT NOT NULL REFERENCES support_tickets(id),
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX support_messages_ticket ON support_messages(ticket_id, sequence);
CREATE TABLE support_operations (
    operation_id TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL,
    request_key TEXT NOT NULL,
    operation TEXT NOT NULL,
    ticket_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    committed_at TEXT NOT NULL,
    result_json TEXT NOT NULL,
    UNIQUE(actor_id, request_key)
);
INSERT INTO support_metadata(key, value) VALUES ('schema_version', '1');
INSERT INTO support_metadata(key, value) VALUES ('schema_owner', 'ai-advent-day33-support');
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value: str, name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise SupportError("invalid_argument", name + " must be text")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise SupportError("invalid_argument", name + " is not valid UTF-8") from error
    trimmed = value.strip()
    if not 1 <= len(trimmed) <= maximum:
        raise SupportError("invalid_argument", name + " length is invalid")
    return trimmed


def _actor_id(actor: Actor) -> str:
    if not isinstance(actor, Actor) or not isinstance(actor.user_id, str):
        raise SupportError("invalid_argument", "Invalid actor")
    if _ACTOR_RE.fullmatch(actor.user_id) is None:
        raise SupportError("invalid_argument", "Invalid actor")
    return actor.user_id


def _request_key(value: str) -> str:
    if not isinstance(value, str) or _KEY_RE.fullmatch(value) is None:
        raise SupportError("invalid_argument", "Invalid request key")
    return value


def _ticket_id(value: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise SupportError("invalid_argument", "Invalid ticket ID")
    return value


def _fingerprint(operation: str, arguments: dict) -> str:
    payload = json.dumps(
        {"operation": operation, "arguments": arguments},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class TicketStore:
    def __init__(self, path: str | Path):
        try:
            self.path = Path(path)
        except (TypeError, ValueError) as error:
            raise SupportError("invalid_argument", "Invalid storage path") from error
        with _INITIALIZE_LOCK:
            if self.path.exists():
                self._inspect_existing_readonly()

    def _inspect_existing_readonly(self) -> None:
        try:
            with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
                self._check_schema(connection, allow_empty=False)
        except SupportError:
            raise
        except (sqlite3.Error, OSError, ValueError) as error:
            raise SupportError("storage_unavailable", "Support storage is unavailable") from error

    @staticmethod
    def _check_schema(connection: sqlite3.Connection, *, allow_empty: bool) -> bool:
        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        if not tables and allow_empty:
            return False
        if tables != _TABLES:
            raise SupportError("storage_unavailable", "Support storage is incompatible")
        version = connection.execute(
            "SELECT value FROM support_metadata WHERE key = 'schema_version'"
        ).fetchone()
        if version is None or version[0] != "1":
            raise SupportError("storage_unavailable", "Support storage is incompatible")
        owner = connection.execute(
            "SELECT value FROM support_metadata WHERE key = 'schema_owner'"
        ).fetchone()
        if owner is None or owner[0] != "ai-advent-day33-support":
            raise SupportError("storage_unavailable", "Support storage is incompatible")
        for table, expected_columns in _COLUMNS.items():
            columns = tuple(row[1] for row in connection.execute(
                "PRAGMA table_info(" + table + ")"
            ))
            if columns != expected_columns:
                raise SupportError("storage_unavailable", "Support storage is incompatible")
        return True

    def _initialize_new(self) -> None:
        try:
            descriptor, temp_name = tempfile.mkstemp(
                prefix="." + self.path.name + ".",
                suffix=".tmp.sqlite3",
                dir=self.path.parent,
            )
        except OSError as error:
            raise SupportError("storage_unavailable", "Support storage is unavailable") from error
        temp_path = Path(temp_name)
        try:
            os.close(descriptor)
            connection = sqlite3.connect(temp_path, timeout=10, isolation_level=None)
            try:
                connection.execute("BEGIN IMMEDIATE")
                for statement in _SCHEMA.split(";"):
                    if statement.strip():
                        connection.execute(statement)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()
            try:
                os.link(temp_path, self.path)
            except FileExistsError:
                self._inspect_existing_readonly()
        except SupportError:
            raise
        except (sqlite3.Error, OSError) as error:
            raise SupportError("storage_unavailable", "Support storage is unavailable") from error
        finally:
            temp_path.unlink(missing_ok=True)

    def _connect(self) -> sqlite3.Connection:
        with _INITIALIZE_LOCK:
            connection = None
            try:
                if self.path.exists():
                    self._inspect_existing_readonly()
                else:
                    self._initialize_new()
                connection = sqlite3.connect(
                    self.path.resolve().as_uri() + "?mode=rw",
                    uri=True,
                    timeout=10,
                    isolation_level=None,
                )
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys = ON")
                self._check_schema(connection, allow_empty=False)
                return connection
            except SupportError:
                if connection is not None:
                    connection.close()
                raise
            except (sqlite3.Error, OSError) as error:
                if connection is not None:
                    connection.close()
                raise SupportError("storage_unavailable", "Support storage is unavailable") from error
    def _with_connection(self, action: Callable[[sqlite3.Connection], object]):
        connection = self._connect()
        try:
            return action(connection)
        except SupportError:
            raise
        except sqlite3.Error as error:
            raise SupportError("storage_unavailable", "Support storage is unavailable") from error
        finally:
            connection.close()

    @staticmethod
    def _snapshot(connection: sqlite3.Connection, actor_id: str, ticket_id: str) -> dict:
        row = connection.execute(
            "SELECT id, title, status, created_at, updated_at "
            "FROM support_tickets WHERE id = ? AND actor_id = ?",
            (ticket_id, actor_id),
        ).fetchone()
        if row is None:
            raise SupportError("not_found", "Ticket not found")
        messages = connection.execute(
            "SELECT id, body, created_at FROM support_messages "
            "WHERE ticket_id = ? ORDER BY sequence",
            (ticket_id,),
        ).fetchall()
        return {
            "id": row["id"],
            "title": row["title"],
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "messages": [dict(message) for message in messages],
        }

    @staticmethod
    def _replay(connection: sqlite3.Connection, actor_id: str, key: str, fingerprint: str):
        row = connection.execute(
            "SELECT fingerprint, result_json FROM support_operations "
            "WHERE actor_id = ? AND request_key = ?",
            (actor_id, key),
        ).fetchone()
        if row is None:
            return None
        if row["fingerprint"] != fingerprint:
            raise SupportError("idempotency_conflict", "Request key has different arguments")
        return json.loads(row["result_json"])

    @staticmethod
    def _save_operation(
        connection: sqlite3.Connection,
        actor_id: str,
        key: str,
        operation: str,
        ticket_id: str,
        fingerprint: str,
        snapshot: dict,
    ) -> dict:
        receipt = {
            "operation_id": uuid.uuid4().hex,
            "request_key": key,
            "operation": operation,
            "ticket_id": ticket_id,
            "committed_at": _utc_now(),
        }
        result = {"ticket": snapshot, "receipt": receipt}
        connection.execute(
            "INSERT INTO support_operations "
            "(operation_id, actor_id, request_key, operation, ticket_id, fingerprint, committed_at, result_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                receipt["operation_id"], actor_id, key, operation, ticket_id,
                fingerprint, receipt["committed_at"],
                json.dumps(result, ensure_ascii=False, separators=(",", ":")),
            ),
        )
        return result

    def _mutate(
        self,
        actor_id: str,
        key: str,
        operation: str,
        arguments: dict,
        apply: Callable[[sqlite3.Connection, str], tuple[str, dict]],
    ) -> dict:
        fingerprint = _fingerprint(operation, arguments)

        def action(connection: sqlite3.Connection) -> dict:
            connection.execute("BEGIN IMMEDIATE")
            try:
                replay = self._replay(connection, actor_id, key, fingerprint)
                if replay is not None:
                    connection.commit()
                    return replay
                ticket_id, snapshot = apply(connection, actor_id)
                result = self._save_operation(
                    connection, actor_id, key, operation, ticket_id, fingerprint, snapshot
                )
                connection.commit()
                return result
            except BaseException:
                connection.rollback()
                raise

        return self._with_connection(action)

    def lookup_receipt(self, actor: Actor, operation: str, arguments: dict) -> dict | None:
        """Read an original receipt without creating storage or executing a mutation."""
        actor_id = _actor_id(actor)
        if operation not in {"create", "append", "close"}:
            raise SupportError("invalid_argument", "Invalid ticket operation")
        arguments = dict(arguments)
        key = _request_key(arguments.pop("request_key"))
        fingerprint = _fingerprint(operation, arguments)
        try:
            with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
                connection.row_factory = sqlite3.Row
                self._check_schema(connection, allow_empty=False)
                result = self._replay(connection, actor_id, key, fingerprint)
                if result is None:
                    return None
                # No historical ticket text or current mutable snapshot crosses this boundary.
                return {field: result["receipt"][field] for field in (
                    "operation_id", "request_key", "operation", "ticket_id", "committed_at")}
        except SupportError:
            raise
        except (sqlite3.Error, OSError, ValueError) as error:
            raise SupportError("storage_unavailable", "Support storage is unavailable") from error

    def create(self, actor: Actor, title: str, description: str, request_key: str) -> dict:
        actor_id = _actor_id(actor)
        original_title = title
        original_description = description
        title = _text(title, "title", 160)
        description = _text(description, "description", 8000)
        key = _request_key(request_key)

        def apply(connection: sqlite3.Connection, owner: str) -> tuple[str, dict]:
            ticket_id = uuid.uuid4().hex
            now = _utc_now()
            connection.execute(
                "INSERT INTO support_tickets(id, actor_id, title, status, created_at, updated_at) "
                "VALUES (?, ?, ?, 'open', ?, ?)",
                (ticket_id, owner, title, now, now),
            )
            connection.execute(
                "INSERT INTO support_messages(id, ticket_id, body, created_at) VALUES (?, ?, ?, ?)",
                (uuid.uuid4().hex, ticket_id, description, now),
            )
            return ticket_id, self._snapshot(connection, owner, ticket_id)

        return self._mutate(actor_id, key, "create", {
            "title": original_title, "description": original_description,
        }, apply)

    def get(self, actor: Actor, ticket_id: str) -> dict:
        actor_id = _actor_id(actor)
        ticket_id = _ticket_id(ticket_id)

        def action(connection: sqlite3.Connection) -> dict:
            connection.execute("BEGIN")
            try:
                snapshot = self._snapshot(connection, actor_id, ticket_id)
                connection.commit()
                return snapshot
            except BaseException:
                connection.rollback()
                raise

        return self._with_connection(action)
    def list(self, actor: Actor, limit: int = 20) -> list[dict]:
        actor_id = _actor_id(actor)
        if type(limit) is not int or not 1 <= limit <= 50:
            raise SupportError("invalid_argument", "Invalid list limit")

        def action(connection: sqlite3.Connection) -> list[dict]:
            rows = connection.execute(
                "SELECT id, title, status, created_at, updated_at "
                "FROM support_tickets WHERE actor_id = ? "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                (actor_id, limit),
            ).fetchall()
            return [dict(row) for row in rows]

        return self._with_connection(action)

    def append(self, actor: Actor, ticket_id: str, message: str, request_key: str) -> dict:
        actor_id = _actor_id(actor)
        ticket_id = _ticket_id(ticket_id)
        original_message = message
        message = _text(message, "message", 8000)
        key = _request_key(request_key)

        def apply(connection: sqlite3.Connection, owner: str) -> tuple[str, dict]:
            current = self._snapshot(connection, owner, ticket_id)
            if current["status"] != "open":
                raise SupportError("closed", "Ticket is closed")
            if len(current["messages"]) >= 100:
                raise SupportError("limit_exceeded", "Ticket message limit reached")
            now = _utc_now()
            connection.execute(
                "INSERT INTO support_messages(id, ticket_id, body, created_at) VALUES (?, ?, ?, ?)",
                (uuid.uuid4().hex, ticket_id, message, now),
            )
            connection.execute(
                "UPDATE support_tickets SET updated_at = ? WHERE id = ? AND actor_id = ?",
                (now, ticket_id, owner),
            )
            return ticket_id, self._snapshot(connection, owner, ticket_id)

        return self._mutate(actor_id, key, "append", {
            "ticket_id": ticket_id, "message": original_message,
        }, apply)

    def close(self, actor: Actor, ticket_id: str, request_key: str) -> dict:
        actor_id = _actor_id(actor)
        ticket_id = _ticket_id(ticket_id)
        key = _request_key(request_key)

        def apply(connection: sqlite3.Connection, owner: str) -> tuple[str, dict]:
            current = self._snapshot(connection, owner, ticket_id)
            if current["status"] != "open":
                raise SupportError("closed", "Ticket is closed")
            now = _utc_now()
            connection.execute(
                "UPDATE support_tickets SET status = 'closed_by_user', updated_at = ? "
                "WHERE id = ? AND actor_id = ?",
                (now, ticket_id, owner),
            )
            return ticket_id, self._snapshot(connection, owner, ticket_id)

        return self._mutate(actor_id, key, "close", {"ticket_id": ticket_id}, apply)

    def health(self) -> dict:
        try:
            with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
                self._check_schema(connection, allow_empty=False)
                connection.execute("SELECT value FROM support_metadata WHERE key = 'schema_version'").fetchone()
                return {"storage": "ok", "checked_at": _utc_now()}
        except SupportError:
            raise
        except (sqlite3.Error, OSError, ValueError) as error:
            raise SupportError("storage_unavailable", "Support storage is unavailable") from error

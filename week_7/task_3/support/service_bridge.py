"""Trusted Day31 Store adapter for the optional Day33 MCP integration.

The host binds one authenticated user/branch/turn before starting the child.
The main Store remains the only profile and memory database.
"""

from __future__ import annotations

import importlib
import json
import re
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .domain import SupportError


_BINDING_KEYS = {"app_root", "db_path", "branch_id", "turn_id", "explicit_budget"}
_SERVICE_COLUMNS = {
    "users": {"id", "username", "salt", "password_hash", "profile"},
    "memories": {"id", "user_id", "key", "value"},
    "chats": {"id", "user_id", "title"},
    "branches": {"id", "user_id", "chat_id", "title", "summary", "summary_through",
                 "working_state", "conditions", "revision"},
    "messages": {"id", "branch_id", "user_id", "ordinal", "role", "content",
                 "sources", "snapshot", "turn_id"},
    "turns": {"id", "branch_id", "user_id", "question", "status", "details", "error", "events"},
    "calls": {"id", "user_id", "branch_id", "chat_id", "turn_id", "stage", "model",
              "status", "usage", "cost_estimated_usd", "metadata", "input_policy", "output_policy"},
}
_ID = re.compile(r"[0-9a-f]{32}\Z")


def validate_binding(binding: dict) -> dict:
    if not isinstance(binding, dict) or set(binding) != _BINDING_KEYS:
        raise ValueError("Invalid service binding")
    try:
        app_root = Path(binding["app_root"]).resolve(strict=True)
        db_path = Path(binding["db_path"]).resolve()
    except (OSError, TypeError, ValueError) as error:
        raise ValueError("Invalid service binding paths") from error
    if (not app_root.is_dir() or not (app_root / "graphics" / "store.py").is_file()
            or not (app_root / "graphics" / "context.py").is_file()):
        raise ValueError("Invalid service app root")
    if (not isinstance(binding["branch_id"], str) or _ID.fullmatch(binding["branch_id"]) is None
            or not isinstance(binding["turn_id"], str) or _ID.fullmatch(binding["turn_id"]) is None):
        raise ValueError("Invalid service branch or turn")
    budget = binding["explicit_budget"]
    if type(budget) is not int or budget <= 0 or budget > 1_000_000:
        raise ValueError("Invalid explicit budget")
    return {"app_root": str(app_root), "db_path": str(db_path),
            "branch_id": binding["branch_id"], "turn_id": binding["turn_id"],
            "explicit_budget": budget}


def _verify_main_database(path: Path) -> None:
    """Read-only preflight: Store.__init__ must never create a foreign main DB."""
    if not path.is_file() or path.is_symlink():
        raise SupportError("storage_unavailable", "Main service storage is unavailable")
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if not set(_SERVICE_COLUMNS) <= tables:
                raise SupportError("storage_unavailable", "Main service storage is incompatible")
            for table, required in _SERVICE_COLUMNS.items():
                columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
                if not required <= columns:
                    raise SupportError("storage_unavailable", "Main service storage is incompatible")
    except SupportError:
        raise
    except (sqlite3.Error, OSError, ValueError) as error:
        raise SupportError("storage_unavailable", "Main service storage is unavailable") from error


class ServiceBridge:
    def __init__(self, binding: dict, actor_id: str):
        binding = validate_binding(binding)
        if not isinstance(actor_id, str) or not actor_id.isascii() or not actor_id.isdecimal():
            raise SupportError("invalid_argument", "Invalid service actor")
        self.user_id = int(actor_id)
        if self.user_id <= 0:
            raise SupportError("invalid_argument", "Invalid service actor")
        self.branch_id = binding["branch_id"]
        self.turn_id = binding["turn_id"]
        self.db_path = Path(binding["db_path"])
        self.app_root = Path(binding["app_root"])
        _verify_main_database(self.db_path)
        if str(self.app_root) not in sys.path:
            sys.path.insert(0, str(self.app_root))
        store_module = importlib.import_module("graphics.store")
        self._budget_error = importlib.import_module("graphics.context").ExplicitBudgetError
        self._conflict_error = store_module.SupportIdempotencyConflict
        self.store = store_module.Store(self.db_path, explicit_budget=binding["explicit_budget"])

    def _check_owner(self) -> None:
        with self.store._connect() as db:
            self.store._require_support_context(db, self.user_id, self.branch_id, self.turn_id)

    def call(self, name: str, arguments: dict) -> dict:
        self._check_owner()
        try:
            if name == "support_user_get":
                return {"profile": self.store.get_profile(self.user_id),
                        "memories": self.store.list_memories(self.user_id)}
            if name == "support_profile_update":
                return self.store.support_update_profile(self.user_id, self.branch_id, self.turn_id,
                                                         arguments["changes"], arguments["request_key"])
            if name == "support_memory_set":
                return self.store.support_memory_set(self.user_id, self.branch_id, self.turn_id,
                                                     arguments["key"], arguments["value"],
                                                     arguments["request_key"])
            if name == "support_memory_delete":
                return self.store.support_memory_delete(self.user_id, self.branch_id, self.turn_id,
                                                        arguments["memory_id"], arguments["request_key"])
            if name == "support_diagnostics":
                return self.diagnostics()
        except self._conflict_error as error:
            raise SupportError("idempotency_conflict", "Request key has different arguments") from error
        except self._budget_error as error:
            raise SupportError("limit_exceeded", "Explicit context budget exceeded") from error
        except PermissionError as error:
            raise SupportError("not_found", "Owned context is unavailable") from error
        except ValueError as error:
            raise SupportError("invalid_argument", "Invalid support operation") from error
        raise SupportError("invalid_argument", "Unknown support tool")

    def diagnostics(self) -> dict:
        self._check_owner()
        with self.store._connect() as db:
            turn = db.execute("SELECT status FROM turns WHERE id=? AND user_id=? AND branch_id=?",
                              (self.turn_id, self.user_id, self.branch_id)).fetchone()
            own_calls = db.execute("SELECT status,count(*) AS n FROM calls WHERE user_id=? AND branch_id=? "
                                   "AND turn_id=? GROUP BY status",
                                   (self.user_id, self.branch_id, self.turn_id)).fetchall()
            previous = db.execute("SELECT id,status FROM turns WHERE user_id=? AND branch_id=? "
                                  "AND id!=? ORDER BY rowid DESC LIMIT 3",
                                  (self.user_id, self.branch_id, self.turn_id)).fetchall()
            recent_turns = []
            for row in previous:
                grouped = db.execute("SELECT stage,status,count(*) AS n FROM calls WHERE user_id=? "
                                     "AND branch_id=? AND turn_id=? GROUP BY stage,status "
                                     "ORDER BY stage,status",
                                     (self.user_id, self.branch_id, row["id"])).fetchall()
                recent_turns.append({"id": row["id"], "status": row["status"],
                                     "calls": [{"stage": item["stage"], "status": item["status"],
                                                "count": item["n"]} for item in grouped]})
        return {
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "main_storage": {"status": "ok", "scope": "main_store"},
            "current_turn": {"status": turn["status"]},
            "own_calls": {row["status"]: row["n"] for row in own_calls},
            "recent_turns": recent_turns,
            "rag": {"status": "unknown", "index_file": "present" if
                    (self.db_path.parent / "retrieval.sqlite3").is_file() else "missing"},
            "llm": {"status": "unknown"},
        }


def binding_from_environment(raw: str, actor_id: str) -> ServiceBridge:
    try:
        binding = json.loads(raw)
    except (TypeError, ValueError) as error:
        raise ValueError("Invalid service binding") from error
    return ServiceBridge(binding, actor_id)

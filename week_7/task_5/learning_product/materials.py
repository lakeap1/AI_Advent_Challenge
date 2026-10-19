"""Export an accepted reply into its owner's guarded learning workspace.

This uses task4's Workspace checks and its repository lock so exports cannot
race task4's confirmed multi-file apply. The inherited-message lookup reads
task1's existing SQLite schema because forked copies omit their turn_id.
"""

import hashlib
import json
import os
import sqlite3
from contextlib import closing

from learning_files.repository import _file_lock
from learning_files.workspace import MAX_DOCUMENTS, _safe_file, text_bytes


class MaterialConflict(Exception):
    pass


class MaterialLimit(Exception):
    pass


def _accepted_turn(store, owner, branch, message, question):
    turn_id = message.get("turn_id")
    if turn_id is None:
        # Forks copy messages with fresh ids and no turn_id. Find the original
        # published reply in this owner's chat, then verify its real turn.
        with closing(sqlite3.connect(store.path)) as db:
            rows = db.execute(
                "SELECT m.turn_id,m.sources FROM messages m JOIN branches b ON b.id=m.branch_id "
                "WHERE m.user_id=? AND b.chat_id=? AND m.role='assistant' "
                "AND m.turn_id IS NOT NULL AND m.content=?",
                (owner, branch["chat_id"], message["content"]),
            ).fetchall()
        for candidate, raw_sources in rows:
            if json.loads(raw_sources) != message["sources"]:
                continue
            turn = store.get_turn(owner, candidate)
            if turn["status"] == "accepted" and turn["question"] == question:
                return turn
        raise PermissionError("message unavailable")
    turn = store.get_turn(owner, turn_id)
    if (turn["status"] != "accepted" or turn["branch_id"] != branch["id"]
            or turn["question"] != question):
        raise PermissionError("message unavailable")
    return turn


def accepted_question(store, owner, branch, index):
    message = branch["messages"][index]
    if message["role"] != "assistant":
        raise PermissionError("message unavailable")
    question = next((previous["content"] for previous in reversed(branch["messages"][:index])
                     if previous["role"] == "user"), None)
    if question is None:
        raise PermissionError("message unavailable")
    _accepted_turn(store, owner, branch, message, question)
    return question


def can_save_material(store, owner, branch, index):
    try:
        accepted_question(store, owner, branch, index)
    except PermissionError:
        return False
    return True


def _material(question, message):
    def line(value):
        return " ".join(str(value).split())

    sources = []
    for number, source in enumerate(message["sources"], 1):
        if not isinstance(source, dict):
            sources.append(f"{number}. {line(source)}")
            continue
        sources.append(f"{number}. {line(source.get('title') or 'Источник')}")
        for key, label in (("url", "Ссылка"), ("path", "Документ"),
                           ("section", "Раздел"), ("version", "Версия"),
                           ("pipeline", "Рендеринг")):
            if source.get(key):
                sources.append(f"   {label}: {line(source[key])}")
    if not sources:
        sources.append("Источники для этого ответа не указаны.")
    return ("# Сохранённое объяснение\n\n## Вопрос\n\n" + question +
            "\n\n## Ответ\n\n" + message["content"] +
            "\n\n## Использованные источники\n\n" + "\n".join(sources) + "\n")


def save_material(store, repository, owner, branch_id, message_id):
    branch = store.get_branch(owner, branch_id)
    messages = branch["messages"]
    for index, message in enumerate(messages):
        if message["id"] == message_id:
            break
    else:
        raise PermissionError("message unavailable")
    question = accepted_question(store, owner, branch, index)
    payload = text_bytes(_material(question, message))
    path = "outputs/answer-" + hashlib.sha256(message_id.encode("utf-8")).hexdigest()[:24] + ".md"
    sha256 = hashlib.sha256(payload).hexdigest()
    workspace = repository.workspace(owner)
    with _file_lock(repository.lock_path):
        target = workspace._checked_path(path)
        if _safe_file(target):
            if workspace.read(path)["content"].encode("utf-8") != payload:
                raise MaterialConflict("Материал изменён; файл не перезаписан")
            created = False
        else:
            if len(workspace.inventory()) >= MAX_DOCUMENTS:
                raise MaterialLimit("Достигнут предел: 24 файла")
            target = workspace._checked_path(path, writable=True, create=True)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            handle = os.open(target, flags, 0o600)
            try:
                with os.fdopen(handle, "wb") as output:
                    output.write(payload)
                    output.flush()
                    os.fsync(output.fileno())
            except BaseException:
                target.unlink(missing_ok=True)
                raise
            created = True
    return {"path": path, "sha256": sha256, "bytes": len(payload), "created": created}

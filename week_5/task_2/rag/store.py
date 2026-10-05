"""Durable request and stage ledger. Every network call is pending before dispatch."""
import json
import os
import sqlite3
from pathlib import Path
from threading import RLock


def _encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class RagStoreBusy(OSError):
    """Another process or service instance owns the writable RAG ledger."""


class RagStore:
    def __init__(self, data_dir):
        directory = Path(data_dir)
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "rag.sqlite3"
        self.lock = RLock()
        self._lease = self._acquire_lease(directory / "rag.sqlite3.owner.lock")
        try:
            self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
            self.db.row_factory = sqlite3.Row
            with self.lock, self.db:
                self.db.execute("CREATE TABLE IF NOT EXISTS requests (request_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, record_json TEXT NOT NULL)")
                self.db.execute("CREATE INDEX IF NOT EXISTS requests_session_idx ON requests(session_id)")
                # The exclusive OS lease proves no live owner can still finish
                # these pending calls. A stale lock file by itself proves nothing.
                for row in self.db.execute("SELECT request_id,record_json FROM requests").fetchall():
                    record = json.loads(row["record_json"])
                    if record["status"] == "pending":
                        record["status"] = "interrupted"
                        record["code"] = "interrupted"
                        record["text"] = "Запрос был прерван. Неизвестный расход отмечен как неполный."
                        for stage in record["stages"]:
                            if stage["status"] == "pending":
                                stage["status"] = "interrupted"
                                stage["output_policy"] = {"passed": False, "code": "interrupted"}
                        self.db.execute("UPDATE requests SET record_json=? WHERE request_id=?", (_encode(record), row["request_id"]))
        except BaseException:
            if hasattr(self, "db"):
                self.db.close()
            self._release_lease()
            raise

    @staticmethod
    def _acquire_lease(path):
        handle = open(path, "a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except OSError as error:
            handle.close()
            raise RagStoreBusy("RAG store already in use") from error

    def _release_lease(self):
        handle, self._lease = self._lease, None
        if handle is None:
            return
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def save(self, record):
        with self.lock, self.db:
            self.db.execute("INSERT INTO requests(request_id,session_id,record_json) VALUES(?,?,?) ON CONFLICT(request_id) DO UPDATE SET record_json=excluded.record_json",
                            (record["request_id"], record["session_id"], _encode(record)))

    def state(self, session_id):
        with self.lock:
            rows = [json.loads(row[0]) for row in self.db.execute("SELECT record_json FROM requests WHERE session_id=? ORDER BY rowid", (session_id,))]
        stages = [stage for request in rows for stage in request["stages"]]
        known = sum(float(stage["cost_usd"]) for stage in stages if stage["cost_usd"] is not None)
        complete = all(not stage["api_called"] or stage["cost_usd"] is not None for stage in stages)
        return {"session_id": session_id, "requests": rows,
                "cumulative": {"known_cost_usd": known, "complete": complete,
                               "unknown_calls": sum(bool(stage["api_called"] and stage["cost_usd"] is None) for stage in stages)}}

    def close(self):
        with self.lock:
            try:
                self.db.close()
            finally:
                self._release_lease()

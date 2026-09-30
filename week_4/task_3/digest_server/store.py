"""SQLite state for digest schedules, runs, observations, and immutable digests."""

from __future__ import annotations

import json
import os
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

class ProcessLock:
    def __init__(self, path: Path):
        self.path = path
        self._file: Any = None

    def __enter__(self) -> "ProcessLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self._file.seek(0)
                self._file.write(b"\0")
                self._file.flush()
                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._file.close()
            self._file = None
            raise RuntimeError(f"Digest data directory is already in use: {self.path.parent}") from exc
        return self

    def __exit__(self, *_: object) -> None:
        if self._file is None:
            return
        if os.name == "nt":
            import msvcrt
            self._file.seek(0)
            msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        self._file.close()
        self._file = None


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}


def _normalize_legacy_digest(payload: dict[str, Any]) -> dict[str, Any]:
    questions = payload.get("questions", [])
    for question in questions:
        question.setdefault("source", "blender")
        question.setdefault("excerpt", "")
    if "source_counts" not in payload:
        payload["source_counts"] = {"blender": len(questions)}
    if "source_status" not in payload:
        payload["source_status"] = {"blender": "partial" if payload.get("partial") else "ok"}
    payload.setdefault("source_errors", {})
    payload.setdefault("highlights", [
        {"source": "blender", "title": q["title"], "url": q["url"]}
        for q in questions[:3] if "title" in q and "url" in q])
    return payload


class DigestStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS schedule (
                id INTEGER PRIMARY KEY CHECK (id=1), enabled INTEGER NOT NULL DEFAULT 0,
                interval_seconds INTEGER NOT NULL DEFAULT 21600,
                window_hours INTEGER NOT NULL DEFAULT 24, tag TEXT NOT NULL DEFAULT '',
                next_due REAL, demo INTEGER NOT NULL DEFAULT 0, demo_expires_at REAL,
                backoff_until REAL
            );
            INSERT OR IGNORE INTO schedule (id) VALUES (1);
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, status TEXT NOT NULL,
                started_at REAL NOT NULL, finished_at REAL, error TEXT,
                window_start REAL NOT NULL, window_end REAL NOT NULL,
                input_policy TEXT NOT NULL, output_policy TEXT,
                partial INTEGER, quota_remaining INTEGER
            );
            CREATE TABLE IF NOT EXISTS observations (
                run_id INTEGER NOT NULL REFERENCES runs(id), question_id INTEGER NOT NULL,
                title TEXT NOT NULL, url TEXT NOT NULL, answer_count INTEGER NOT NULL,
                tags_json TEXT NOT NULL, created_at REAL NOT NULL,
                PRIMARY KEY (run_id, question_id)
            );
            CREATE TABLE IF NOT EXISTS digests (
                run_id INTEGER PRIMARY KEY REFERENCES runs(id), payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS daily_publications (
                digest_date TEXT PRIMARY KEY, run_id INTEGER NOT NULL UNIQUE REFERENCES digests(run_id)
            );
            CREATE TABLE IF NOT EXISTS daily_analysis (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL UNIQUE REFERENCES daily_publications(run_id),
                payload_json TEXT NOT NULL
            );
        """)
        self._migrate()

    def _migrate(self) -> None:
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            schedule_columns = _columns(self.db, "schedule")
            if "mode" not in schedule_columns:
                self.db.execute("ALTER TABLE schedule ADD COLUMN mode TEXT NOT NULL DEFAULT 'cron'")
                self.db.execute("UPDATE schedule SET mode='legacy' WHERE enabled=1")
            if "sources_json" not in schedule_columns:
                self.db.execute("ALTER TABLE schedule ADD COLUMN sources_json TEXT NOT NULL DEFAULT '[\"blender\",\"computergraphics\",\"graphicdesign\"]'")
            if "scheduled_slot" not in _columns(self.db, "runs"):
                self.db.execute("ALTER TABLE runs ADD COLUMN scheduled_slot REAL")
            if "retryable" not in _columns(self.db, "runs"):
                self.db.execute("ALTER TABLE runs ADD COLUMN retryable INTEGER NOT NULL DEFAULT 0")
            if "source" not in _columns(self.db, "observations"):
                self.db.execute("""CREATE TABLE observations_new (
                    run_id INTEGER NOT NULL REFERENCES runs(id), source TEXT NOT NULL,
                    question_id INTEGER NOT NULL, title TEXT NOT NULL, url TEXT NOT NULL,
                    answer_count INTEGER NOT NULL, tags_json TEXT NOT NULL,
                    created_at REAL NOT NULL, excerpt TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (run_id, source, question_id))""")
                self.db.execute("""INSERT INTO observations_new
                    (run_id, source, question_id, title, url, answer_count, tags_json, created_at)
                    SELECT run_id, 'blender', question_id, title, url, answer_count, tags_json,
                    created_at FROM observations""")
                self.db.execute("DROP TABLE observations")
                self.db.execute("ALTER TABLE observations_new RENAME TO observations")
            self.db.execute("DROP INDEX IF EXISTS one_success_per_slot")
            for row in self.db.execute("SELECT run_id, payload_json FROM digests"):
                payload = json.loads(row["payload_json"])
                source_status = payload.get("source_status", {})
                if isinstance(source_status, dict) and any(
                        status in ("error", "skipped_backoff") for status in source_status.values()):
                    self.db.execute("UPDATE runs SET retryable=1 WHERE id=? AND status='partial'", (row["run_id"],))
                if ("source_counts" not in payload or "source_errors" not in payload
                        or "highlights" not in payload
                        or any("source" not in q for q in payload.get("questions", []))):
                    self.db.execute("UPDATE digests SET payload_json=? WHERE run_id=?",
                                    (json.dumps(_normalize_legacy_digest(payload), ensure_ascii=False), row["run_id"]))
            self.db.execute("""CREATE UNIQUE INDEX one_success_per_slot ON runs(scheduled_slot)
                WHERE scheduled_slot IS NOT NULL AND (status IN ('success','empty')
                    OR (status='partial' AND retryable=0))""")

    def close(self) -> None:
        self.db.close()

    def schedule(self) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM schedule WHERE id=1").fetchone()
        assert row is not None
        return {"enabled": bool(row["enabled"]), "mode": "cron", "timezone": "Asia/Omsk",
                "hours": [0, 6, 12, 18], "window_hours": 24,
                "publication_hour": 0, "publication_timezone": "Asia/Omsk",
                "publication_window": "previous_calendar_day",
                "sources": json.loads(row["sources_json"]), "next_due": row["next_due"],
                "backoff_until": row["backoff_until"]}

    def configure(self, sources: list[str], next_due: float) -> None:
        with self.db:
            current = self.schedule()
            due = current["next_due"] if current["enabled"] and current["next_due"] is not None else next_due
            self.db.execute("""UPDATE schedule SET enabled=1, mode='cron', window_hours=24,
                sources_json=?, next_due=?, demo=0, demo_expires_at=NULL WHERE id=1""",
                (json.dumps(sources), due))

    def pause(self) -> None:
        with self.db:
            self.db.execute("UPDATE schedule SET enabled=0, next_due=NULL WHERE id=1")

    def defer(self, next_due: float, backoff_until: float | None) -> None:
        with self.db:
            self.db.execute("UPDATE schedule SET next_due=?, backoff_until=? WHERE id=1",
                            (next_due, backoff_until))

    def record_backoff(self, until: float) -> None:
        with self.db:
            self.db.execute("UPDATE schedule SET backoff_until=MAX(COALESCE(backoff_until, 0), ?) WHERE id=1",
                            (until,))

    def recover(self, now: float, slot: float) -> None:
        with self.db:
            for row in self.db.execute("SELECT id, payload_json FROM daily_analysis"):
                analysis = json.loads(row["payload_json"])
                if analysis["status"] == "pending":
                    analysis.update(status="interrupted", finished_at=now)
                    analysis["metadata"]["error_code"] = "interrupted"
                    self.db.execute("UPDATE daily_analysis SET payload_json=? WHERE id=?",
                                    (json.dumps(analysis, ensure_ascii=False), row["id"]))
            self.db.execute("""UPDATE runs SET status='interrupted', finished_at=?,
                error='Сервер остановился до завершения сбора', output_policy='not_checked'
                WHERE status='running'""", (now,))
            # Legacy interval schedules are converted to a due catch-up once.
            row = self.db.execute("SELECT mode, enabled FROM schedule WHERE id=1").fetchone()
            if row["enabled"] and row["mode"] != "cron":
                self.db.execute("UPDATE schedule SET mode='cron', next_due=? WHERE id=1", (slot,))
            schedule = self.schedule()
            if schedule["enabled"]:
                incomplete = self.db.execute("""SELECT finished_at FROM runs
                    WHERE scheduled_slot=? AND status='partial' AND retryable=1
                    ORDER BY id DESC LIMIT 1""", (slot,)).fetchone()
                if incomplete and incomplete["finished_at"] is not None:
                    retry_due = max(incomplete["finished_at"] + 300, schedule["backoff_until"] or 0)
                    if schedule["next_due"] is None or schedule["next_due"] > retry_due:
                        self.db.execute("UPDATE schedule SET next_due=? WHERE id=1", (retry_due,))

    def has_completed_slot(self, slot: float) -> bool:
        return self.db.execute("""SELECT 1 FROM runs WHERE scheduled_slot=?
            AND (status IN ('success','empty') OR (status='partial' AND retryable=0))
            LIMIT 1""", (slot,)).fetchone() is not None

    def published_digest(self, digest_date: str) -> dict[str, Any] | None:
        row = self.db.execute("""SELECT p.run_id, d.payload_json FROM daily_publications p
            JOIN digests d ON d.run_id=p.run_id WHERE p.digest_date=?""", (digest_date,)).fetchone()
        return self._attached(row) if row else None

    def _attached(self, row: sqlite3.Row) -> dict[str, Any]:
        digest = json.loads(row["payload_json"])
        analysis = self.db.execute("SELECT payload_json FROM daily_analysis WHERE run_id=?",
                                   (row["run_id"],)).fetchone()
        if analysis:
            digest["analysis"] = json.loads(analysis["payload_json"])
        return digest

    def claim_analysis(self, run_id: int, started: float, metadata: dict[str, Any],
                       input_name: str, output_name: str) -> dict[str, Any] | None:
        """Durably reserve the only analysis attempt before any model I/O."""
        record = {"status": "pending", "text": "", "usage": None,
                  "usage_status": "unavailable", "cost_usd": None,
                  "input_policy": {"name": input_name, "status": "not_checked"},
                  "output_policy": {"name": output_name, "status": "not_checked"},
                  "metadata": metadata, "started_at": started, "finished_at": None,
                  "attempt_id": None}
        with self.db:
            exists = self.db.execute("SELECT 1 FROM daily_analysis WHERE run_id=?", (run_id,)).fetchone()
            if exists:
                return None
            cur = self.db.execute("INSERT INTO daily_analysis(run_id,payload_json) VALUES (?,?)",
                                  (run_id, json.dumps(record, ensure_ascii=False)))
            record["attempt_id"] = cur.lastrowid
            self.db.execute("UPDATE daily_analysis SET payload_json=? WHERE id=?",
                            (json.dumps(record, ensure_ascii=False), cur.lastrowid))
        return record

    def finish_analysis(self, run_id: int, finished: float, result: dict[str, Any]) -> None:
        with self.db:
            row = self.db.execute("SELECT id, payload_json FROM daily_analysis WHERE run_id=?",
                                  (run_id,)).fetchone()
            if row is None:
                raise ValueError("Analysis claim missing")
            previous = json.loads(row["payload_json"])
            if previous["status"] != "pending":
                raise ValueError("Analysis attempt already finished")
            record = {**previous, **result, "started_at": previous["started_at"],
                      "finished_at": finished, "attempt_id": previous["attempt_id"]}
            updated = self.db.execute("UPDATE daily_analysis SET payload_json=? WHERE id=?",
                                      (json.dumps(record, ensure_ascii=False), row["id"]))
            if updated.rowcount != 1:
                raise ValueError("Analysis attempt disappeared")

    def analysis_summary(self) -> dict[str, Any]:
        rows = self.db.execute("SELECT payload_json FROM daily_analysis").fetchall()
        records = [json.loads(row["payload_json"]) for row in rows]
        called = [item for item in records if item["usage_status"] != "not_requested"]
        unknown = sum(item["cost_usd"] is None for item in called)
        known = sum((Decimal(item["cost_usd"]) for item in called if item["cost_usd"] is not None), Decimal(0))
        return {"known_cost_usd": format(known, "f"), "cost_complete": unknown == 0,
                "unknown_cost_requests": unknown, "api_requests": len(called)}

    def daily_retry_after(self, start: float, end: float) -> float | None:
        row = self.db.execute("""SELECT status, finished_at FROM runs
            WHERE window_start=? AND window_end=? AND started_at>=?
            ORDER BY id DESC LIMIT 1""", (start, end, end)).fetchone()
        day_retry = (float(row["finished_at"]) + 300
                     if row and row["status"] in ("error", "interrupted", "partial")
                     and row["finished_at"] is not None else 0)
        # The calendar target changes at midnight, but an immediately preceding failed
        # request still consumes the provider retry budget for at least five minutes.
        last = self.db.execute("""SELECT status, retryable, finished_at FROM runs
            ORDER BY id DESC LIMIT 1""").fetchone()
        global_retry = (float(last["finished_at"]) + 300
                        if last and (last["status"] in ("error", "interrupted")
                                     or last["status"] == "partial" and last["retryable"])
                        and last["finished_at"] is not None else 0)
        return max(day_retry, global_retry) or None

    def start_run(self, started: float, window_start: float, window_end: float,
                  scheduled_slot: float | None = None) -> int:
        with self.db:
            cur = self.db.execute("""INSERT INTO runs
                (status, started_at, window_start, window_end, input_policy, scheduled_slot)
                VALUES ('running', ?, ?, ?, 'passed', ?)""",
                (started, window_start, window_end, scheduled_slot))
            return int(cur.lastrowid)

    def finish_success(self, run_id: int, finished: float, digest: dict[str, Any],
                       questions: list[dict[str, Any]], quota_remaining: int | None,
                       *, retryable: bool = False, next_due: float | None = None,
                       backoff_until: float | None = None, scheduled: bool = False,
                       publication_date: str | None = None) -> None:
        status = "partial" if digest["partial"] else "empty" if not questions else "success"
        with self.db:
            self.db.executemany("""INSERT INTO observations
                (run_id, source, question_id, title, url, answer_count, tags_json, created_at, excerpt)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [(run_id, q["source"], q["question_id"], q["title"], q["url"],
                  q["answer_count"], json.dumps(q["tags"], ensure_ascii=False),
                  q["created_at"], q["excerpt"]) for q in questions])
            self.db.execute("INSERT INTO digests (run_id, payload_json) VALUES (?, ?)",
                            (run_id, json.dumps(digest, ensure_ascii=False)))
            self.db.execute("""UPDATE runs SET status=?, finished_at=?, output_policy='passed',
                partial=?, quota_remaining=?, retryable=? WHERE id=?""",
                (status, finished, int(digest["partial"]), quota_remaining, int(retryable), run_id))
            if publication_date is not None:
                if retryable:
                    raise ValueError("Retryable collection cannot be published")
                self.db.execute("INSERT INTO daily_publications (digest_date, run_id) VALUES (?, ?)",
                                (publication_date, run_id))
            if scheduled:
                self.db.execute("UPDATE schedule SET next_due=?, backoff_until=? WHERE id=1",
                                (next_due, backoff_until))

    def finish_error(self, run_id: int, finished: float, error: str, output_policy: str,
                     *, next_due: float | None = None, backoff_until: float | None = None,
                     scheduled: bool = False) -> None:
        with self.db:
            self.db.execute("""UPDATE runs SET status='error', finished_at=?, error=?,
                output_policy=? WHERE id=?""", (finished, error[:500], output_policy, run_id))
            if scheduled:
                self.db.execute("UPDATE schedule SET next_due=?, backoff_until=? WHERE id=1",
                                (next_due, backoff_until))

    def state(self) -> dict[str, Any]:
        rows = self.db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 20").fetchall()
        runs = [{key: (bool(row[key]) if key == "partial" and row[key] is not None else row[key])
                 for key in row.keys()} for row in rows]
        latest = self.db.execute("""SELECT p.run_id, d.payload_json FROM daily_publications p
            JOIN digests d ON d.run_id=p.run_id ORDER BY p.run_id DESC LIMIT 1""").fetchone()
        return {"schedule": self.schedule(), "runs": runs,
                "latest": self._attached(latest) if latest else None,
                "analysis_summary": self.analysis_summary()}

    def list_digests(self, after_run_id: int, limit: int) -> dict[str, Any]:
        rows = self.db.execute("""SELECT p.run_id, d.payload_json FROM daily_publications p
            JOIN digests d ON d.run_id=p.run_id WHERE p.run_id>?
            ORDER BY p.run_id ASC LIMIT ?""", (after_run_id, limit + 1)).fetchall()
        page = rows[:limit]
        return {"digests": [self._attached(row) for row in page],
                "next_cursor": page[-1]["run_id"] if page else after_run_id,
                "has_more": len(rows) > limit}

    def get_digest(self, run_id: int) -> dict[str, Any] | None:
        row = self.db.execute("SELECT run_id, payload_json FROM digests WHERE run_id=?", (run_id,)).fetchone()
        return self._attached(row) if row else None

"""Durable private proposals and guarded, explicitly confirmed file writes."""

import contextlib
import difflib
import json
import os
import sqlite3
import stat
import tempfile
import time
import uuid
from pathlib import Path

from .workspace import MAX_DOCUMENTS, Workspace, _safe_directory, _safe_file, digest, text_bytes, valid_path, valid_user


@contextlib.contextmanager
def _file_lock(path):
    """One advisory lock shared by repository processes on Windows and POSIX."""
    with open(path, "a+b") as lock:
        deadline = time.monotonic() + 10
        while True:
            try:
                lock.seek(0)
                if os.name == "nt":
                    import msvcrt
                    if lock.seek(0, os.SEEK_END) == 0:
                        lock.write(b"0")
                        lock.flush()
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise OSError("workspace lock unavailable") from None
                time.sleep(0.02)
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


class FileRepository:
    def __init__(self, root):
        self.root = Path(root)
        if not _safe_directory(self.root):
            self.root.mkdir(mode=0o700, parents=True)
        self.db_path = self.root / "learning_files.sqlite3"
        self.lock_path = self.root / ".learning_files.lock"
        _safe_file(self.db_path)
        _safe_file(self.lock_path)
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, branch_id TEXT NOT NULL,
                request_id TEXT NOT NULL, goal TEXT NOT NULL, status TEXT NOT NULL,
                message TEXT NOT NULL, summary TEXT NOT NULL, sources TEXT NOT NULL,
                changes TEXT NOT NULL, saved TEXT NOT NULL, turn_id TEXT NOT NULL DEFAULT '',
                UNIQUE(user_id, request_id))""")
            if "turn_id" not in {row["name"] for row in db.execute("PRAGMA table_info(runs)")}:
                db.execute("ALTER TABLE runs ADD COLUMN turn_id TEXT NOT NULL DEFAULT ''")

    @contextlib.contextmanager
    def _db(self):
        with _file_lock(self.lock_path):
            self._cleanup_staging()
            db = sqlite3.connect(self.db_path, timeout=10)
            db.row_factory = sqlite3.Row
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()

    def _cleanup_staging(self):
        staging = self.root / ".staging"
        if not _safe_directory(staging):
            return
        for entry in staging.iterdir():
            if not entry.name.startswith(".pending-"):
                raise PermissionError("unexpected staging entry")
            metadata = entry.lstat()
            if not stat.S_ISREG(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
                raise PermissionError("unsafe staging entry")
            entry.unlink()

    def workspace(self, user):
        return Workspace(self.root, valid_user(user))

    @staticmethod
    def _public(row):
        pending = row["status"] == "pending_publish"
        return {"id": row["id"], "branch_id": row["branch_id"],
                "request_id": row["request_id"], "goal": row["goal"],
                "status": row["status"], "message": row["message"],
                "summary": "" if pending else row["summary"],
                "sources": [] if pending else json.loads(row["sources"]),
                "changes": [] if pending else json.loads(row["changes"]),
                "saved": json.loads(row["saved"])}

    @staticmethod
    def _owned(db, user, run_id):
        valid_user(user)
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("invalid run")
        row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None or row["user_id"] != user:
            raise PermissionError("run unavailable")
        return row

    def get(self, user, run_id):
        with self._db() as db:
            row = self._owned(db, user, run_id)
            if row["status"] == "partial":
                self._reconcile_partial(db, self.workspace(user), row)
                row = self._owned(db, user, run_id)
            return self._public(row)

    def find_request(self, user, request_id):
        valid_user(user)
        if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
            raise ValueError("invalid request")
        with self._db() as db:
            row = db.execute("SELECT * FROM runs WHERE user_id=? AND request_id=?", (user, request_id)).fetchone()
            if row is not None and row["status"] == "partial":
                self._reconcile_partial(db, self.workspace(user), row)
                row = self._owned(db, user, row["id"])
            return self._public(row) if row is not None else None

    def latest(self, user, branch):
        """Read the newest owned branch run, including an interrupted request."""
        valid_user(user)
        if not isinstance(branch, str) or not branch.strip() or len(branch) > 128:
            raise ValueError("invalid branch")
        with self._db() as db:
            row = db.execute("SELECT * FROM runs WHERE user_id=? AND branch_id=? ORDER BY rowid DESC LIMIT 1",
                             (user, branch)).fetchone()
            if row is not None and row["status"] == "partial":
                self._reconcile_partial(db, self.workspace(user), row)
                row = self._owned(db, user, row["id"])
            return self._public(row) if row is not None else None

    def start(self, user, branch, request_id, goal):
        valid_user(user)
        if not isinstance(branch, str) or not branch.strip() or len(branch) > 128:
            raise ValueError("invalid branch")
        if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
            raise ValueError("invalid request")
        if not isinstance(goal, str) or not goal.strip() or len(goal) > 4000:
            raise ValueError("invalid goal")
        with self._db() as db:
            row = db.execute("SELECT * FROM runs WHERE user_id=? AND request_id=?", (user, request_id)).fetchone()
            if row is not None:
                if row["branch_id"] != branch or row["goal"] != goal:
                    raise ValueError("request reused with different branch or goal")
                if row["status"] == "partial":
                    self._reconcile_partial(db, self.workspace(user), row)
                    row = self._owned(db, user, row["id"])
                return {"fresh": False, "run": self._public(row)}
            run_id = uuid.uuid4().hex
            db.execute("INSERT INTO runs(id,user_id,branch_id,request_id,goal,status,message,summary,sources,changes,saved) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (run_id, user, branch, request_id, goal, "working", "", "", "[]", "[]", "[]"))
            return {"fresh": True, "run": self._public(self._owned(db, user, run_id))}

    def seed(self, user, files):
        ws = self.workspace(user)
        if not isinstance(files, dict) or len(files) > MAX_DOCUMENTS:
            raise ValueError("invalid seed")
        for path, content in files.items():
            valid_path(path, seed=True)
            text_bytes(content)
        added = []
        with self._db():
            existing = len(ws.inventory())
            missing = [path for path in files if not _safe_file(ws._checked_path(path))]
            if existing + len(missing) > MAX_DOCUMENTS:
                raise ValueError("too many documents")
            for path in missing:
                target = ws._checked_path(path, seed=True, create=True)
                try:
                    with self._staged_bytes(text_bytes(files[path])) as staged:
                        os.link(staged, target)
                except FileExistsError:
                    _safe_file(target)
                    continue
                if ws.read(path)["content"] != files[path]:
                    raise OSError("seed read-back mismatch")
                added.append(path)
        return added

    def build(self, user, changes, sources):
        ws = self.workspace(user)
        if not isinstance(changes, list) or not 1 <= len(changes) <= 3:
            raise ValueError("invalid changes")
        if not isinstance(sources, list) or not 1 <= len(sources) <= 4:
            raise ValueError("invalid sources")
        seen = set()
        snapshots = []
        for supplied in sources:
            if not isinstance(supplied, dict) or set(supplied) != {"path", "content", "sha256"}:
                raise ValueError("invalid source")
            path = supplied["path"]
            valid_path(path)
            if path in seen:
                raise ValueError("duplicate source")
            seen.add(path)
            actual = ws.read(path)
            if actual != supplied:
                raise ValueError("source snapshot mismatch")
            snapshots.append(actual)
        previews = []
        for supplied in changes:
            if not isinstance(supplied, dict) or set(supplied) != {"path", "content"}:
                raise ValueError("invalid change")
            path, content = supplied["path"], supplied["content"]
            valid_path(path, writable=True)
            text_bytes(content)
            if any(item["path"] == path for item in previews):
                raise ValueError("duplicate change")
            target = ws._checked_path(path)
            base = ws.read(path) if _safe_file(target) else None
            before = base["content"] if base else None
            diff = "".join(
                line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
                for line in difflib.unified_diff(
                    (before or "").splitlines(keepends=True), content.splitlines(keepends=True),
                    fromfile=path if base else "/dev/null", tofile=path)
            )
            previews.append({"path": path, "before": before, "content": content,
                             "base_sha256": base["sha256"] if base else None, "diff": diff})
        if len(ws.inventory()) + sum(item["before"] is None for item in previews) > MAX_DOCUMENTS:
            raise ValueError("too many documents")
        return {"sources": snapshots, "changes": previews}

    def _verify_proposal(self, user, proposal, summary):
        if not isinstance(summary, str) or not summary.strip() or len(summary) > 4000:
            raise ValueError("invalid summary")
        if not isinstance(proposal, dict) or set(proposal) != {"sources", "changes"}:
            raise ValueError("invalid proposal")
        if not isinstance(proposal["changes"], list) or not isinstance(proposal["sources"], list):
            raise ValueError("invalid proposal")
        raw_changes = []
        for item in proposal["changes"]:
            if not isinstance(item, dict) or set(item) != {"path", "before", "content", "base_sha256", "diff"}:
                raise ValueError("invalid proposal")
            raw_changes.append({"path": item["path"], "content": item["content"]})
        if self.build(user, raw_changes, proposal["sources"]) != proposal:
            raise ValueError("proposal mismatch")

    def accept(self, user, run_id, proposal, summary):
        # Standalone repository callers retain the accepted Task 1 contract.
        with self._db() as db:
            row = self._owned(db, user, run_id)
            if row["status"] != "working":
                raise ValueError("run is not working")
            self._verify_proposal(user, proposal, summary)
            db.execute("UPDATE runs SET status='validated', summary=?, sources=?, changes=? WHERE id=?",
                       (summary, json.dumps(proposal["sources"]), json.dumps(proposal["changes"]), run_id))
            return self._public(self._owned(db, user, run_id))

    def stage(self, user, run_id, proposal, summary, turn_id):
        """Persist a verified but inapplicable proposal before Store publication."""
        if not isinstance(turn_id, str) or not turn_id or len(turn_id) > 128:
            raise ValueError("invalid turn")
        with self._db() as db:
            row = self._owned(db, user, run_id)
            if row["status"] != "working":
                raise ValueError("run is not working")
            self._verify_proposal(user, proposal, summary)
            db.execute("UPDATE runs SET status='pending_publish',turn_id=?,summary=?,sources=?,changes=? WHERE id=?",
                       (turn_id, summary, json.dumps(proposal["sources"]),
                        json.dumps(proposal["changes"]), run_id))
            return self._public(self._owned(db, user, run_id))

    def finalize(self, user, run_id, store):
        """Recover only from the accepted owned common-Store turn; no model call."""
        with self._db() as db:
            row = self._owned(db, user, run_id)
            if row["status"] != "pending_publish":
                return self._public(row)
            turn_id, branch_id, goal = row["turn_id"], row["branch_id"], row["goal"]
        turn = store.get_turn(user, turn_id)
        if (turn.get("id") != turn_id or turn.get("branch_id") != branch_id
                or turn.get("question") != goal.strip()):
            raise PermissionError("turn does not match proposal")
        with self._db() as db:
            row = self._owned(db, user, run_id)
            if row["status"] != "pending_publish" or row["turn_id"] != turn_id:
                return self._public(row)
            if turn["status"] == "accepted":
                db.execute("UPDATE runs SET status='validated' WHERE id=?", (run_id,))
            elif turn["status"] in {"error", "rejected", "input_rejected", "needs_clarification"}:
                db.execute("UPDATE runs SET status='error',message='Публикация ответа не завершена',"
                           "summary='',sources='[]',changes='[]' WHERE id=?", (run_id,))
            return self._public(self._owned(db, user, run_id))

    def fail(self, user, run_id, status, message):
        if status not in ("rejected", "error") or not isinstance(message, str) or len(message) > 1000:
            raise ValueError("invalid failure")
        with self._db() as db:
            row = self._owned(db, user, run_id)
            if row["status"] != "working":
                raise ValueError("run is not working")
            db.execute("UPDATE runs SET status=?, message=?, sources='[]', changes='[]', summary='' WHERE id=?",
                       (status, message, run_id))
            return self._public(self._owned(db, user, run_id))

    @contextlib.contextmanager
    def _staged_bytes(self, payload):
        staging = self.root / ".staging"
        if not _safe_directory(staging):
            staging.mkdir(mode=0o700)
        descriptor, temp_path = tempfile.mkstemp(prefix=".pending-", dir=staging)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600) if hasattr(os, "fchmod") else None
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            yield temp_path
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    def _write_change(self, workspace, change):
        path = change["path"]
        if change["base_sha256"] == digest(change["content"]):
            return {"path": path, "sha256": change["base_sha256"]}
        target = workspace._checked_path(path, writable=True, create=True)
        with self._staged_bytes(text_bytes(change["content"])) as staged:
            if change["base_sha256"] is None:
                os.link(staged, target)  # exclusive creation; never replace a newly appeared file
            else:
                os.replace(staged, target)
        actual = workspace.read(path)
        if actual["content"] != change["content"] or actual["sha256"] != digest(change["content"]):
            raise OSError("read-back mismatch")
        return {"path": path, "sha256": actual["sha256"]}

    def apply(self, user, run_id):
        ws = self.workspace(user)
        with self._db() as db:
            row = self._owned(db, user, run_id)
            run = self._public(row)
            if run["status"] == "applied":
                if all(self._matches(ws, item["path"], item["sha256"]) for item in run["saved"]):
                    return run
                db.execute("UPDATE runs SET status='conflict', message='Saved file changed' WHERE id=?", (run_id,))
                return self._public(self._owned(db, user, run_id))
            if run["status"] == "partial":
                self._reconcile_partial(db, ws, row)
                return self._public(self._owned(db, user, run_id))
            if run["status"] != "validated":
                return run
            try:
                total = len(ws.inventory()) + sum(change["base_sha256"] is None for change in run["changes"])
            except (ValueError, PermissionError, OSError):
                total = MAX_DOCUMENTS + 1
            if total > MAX_DOCUMENTS:
                db.execute("UPDATE runs SET status='conflict', message='Document limit changed' WHERE id=?", (run_id,))
                return self._public(self._owned(db, user, run_id))
            for source in run["sources"]:
                if not self._matches(ws, source["path"], source["sha256"]):
                    db.execute("UPDATE runs SET status='conflict', message='Source changed' WHERE id=?", (run_id,))
                    return self._public(self._owned(db, user, run_id))
            for change in run["changes"]:
                if change["base_sha256"] is None:
                    if _safe_file(ws._checked_path(change["path"])):
                        db.execute("UPDATE runs SET status='conflict', message='Target appeared' WHERE id=?", (run_id,))
                        return self._public(self._owned(db, user, run_id))
                elif not self._matches(ws, change["path"], change["base_sha256"]):
                    db.execute("UPDATE runs SET status='conflict', message='Target changed' WHERE id=?", (run_id,))
                    return self._public(self._owned(db, user, run_id))
            # A process can die after any atomic file write. This durable marker prevents
            # restart from treating an interrupted batch as an untouched proposal.
            db.execute("UPDATE runs SET status='partial', message='Write interrupted' WHERE id=?", (run_id,))
            db.commit()
            saved = []
            try:
                for change in run["changes"]:
                    saved.append(self._write_change(ws, change))
                    db.execute("UPDATE runs SET saved=? WHERE id=?", (json.dumps(saved), run_id))
                    db.commit()
            except (OSError, ValueError):
                # Reconcile a file written after the latest durable receipt.
                self._reconcile_partial(db, ws, self._owned(db, user, run_id))
                db.execute("UPDATE runs SET message='Write failed' WHERE id=?", (run_id,))
                return self._public(self._owned(db, user, run_id))
            db.execute("UPDATE runs SET status='applied', message='', saved=? WHERE id=?", (json.dumps(saved), run_id))
            return self._public(self._owned(db, user, run_id))

    @staticmethod
    def _matches(workspace, path, expected):
        try:
            return workspace.read(path)["sha256"] == expected
        except (FileNotFoundError, ValueError, PermissionError, OSError):
            return False

    def _reconcile_partial(self, db, workspace, row):
        """Recover writes completed between atomic replace and SQLite receipt."""
        run = self._public(row)
        recorded = {item["path"]: item for item in run["saved"]}
        recovered = []
        for change in run["changes"]:
            path = change["path"]
            if path in recorded:
                recovered.append(recorded[path])
            elif change["base_sha256"] != digest(change["content"]) and self._matches(workspace, path, digest(change["content"])):
                recovered.append({"path": path, "sha256": digest(change["content"])})
        if recovered != run["saved"]:
            db.execute("UPDATE runs SET saved=? WHERE id=?", (json.dumps(recovered), row["id"]))

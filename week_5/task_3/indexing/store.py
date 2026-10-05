"""SQLite lifecycle: pending work is private until a complete pair is committed."""
import json
import os
import sqlite3
import threading
import uuid


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class IndexStore:
    def __init__(self, path):
        self.path = str(path)
        self._lock = threading.RLock()
        self._lease = None
        self._db = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        self._db.row_factory = sqlite3.Row
        with self._lock, self._db:
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY, status TEXT NOT NULL, corpus_hash TEXT,
                    contract_json TEXT, corpus_json TEXT, metrics_json TEXT,
                    error TEXT, started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS documents (
                    run_id TEXT NOT NULL, file TEXT NOT NULL, document_json TEXT NOT NULL,
                    PRIMARY KEY (run_id, file));
                CREATE TABLE IF NOT EXISTS batches (
                    batch_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, strategy TEXT,
                    status TEXT NOT NULL, api_called INTEGER NOT NULL,
                    input_tokens INTEGER, total_tokens INTEGER, cost_usd REAL,
                    record_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS chunks (
                    run_id TEXT NOT NULL, strategy TEXT NOT NULL, ordinal INTEGER NOT NULL,
                    chunk_id TEXT NOT NULL, text TEXT NOT NULL, embedding_json TEXT NOT NULL,
                    chunk_json TEXT NOT NULL,
                    PRIMARY KEY (run_id, strategy, ordinal));
                CREATE INDEX IF NOT EXISTS batches_run_idx ON batches(run_id);
                CREATE INDEX IF NOT EXISTS chunks_run_idx ON chunks(run_id, strategy, ordinal);
            """)
            if self._db.execute("SELECT 1 FROM runs WHERE status='running' LIMIT 1").fetchone():
                if self._acquire_lease():
                    try:
                        self._db.execute("UPDATE runs SET status='interrupted', error='Process stopped during build' WHERE status='running'")
                        for row in self._db.execute("SELECT batch_id, record_json FROM batches WHERE status='pending'").fetchall():
                            record = json.loads(row["record_json"])
                            record["status"] = "interrupted"
                            record["input_tokens"] = None
                            record["total_tokens"] = None
                            record["cost_usd"] = None
                            record["output_policy"] = {"passed": False, "code": "interrupted"}
                            self._db.execute("UPDATE batches SET status='interrupted', input_tokens=NULL, total_tokens=NULL, cost_usd=NULL, record_json=? WHERE batch_id=?", (_json(record), row["batch_id"]))
                    finally:
                        self._release_lease()

    def _acquire_lease(self):
        if self._lease is not None or self.path == ":memory:":
            return True
        handle = open(self.path + ".build.lock", "a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.seek(0)
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._lease = handle
        return True

    def _release_lease(self):
        handle = self._lease
        if handle is None:
            return
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
        self._lease = None

    def close(self):
        with self._lock:
            self._release_lease()
            self._db.close()

    def _active_id(self):
        row = self._db.execute("SELECT value FROM metadata WHERE key='active_run_id'").fetchone()
        return row["value"] if row else None

    def active_contract(self):
        with self._lock:
            active = self._active_id()
            if not active:
                return None
            row = self._db.execute("SELECT corpus_hash, contract_json FROM runs WHERE run_id=? AND status='complete'", (active,)).fetchone()
            return (row["corpus_hash"], json.loads(row["contract_json"])) if row else None

    def start_run(self, corpus, contract):
        run_id = uuid.uuid4().hex
        with self._lock, self._db:
            if self._db.execute("SELECT 1 FROM runs WHERE status='running' LIMIT 1").fetchone():
                raise ValueError("An indexing build is already running")
            self._db.execute("INSERT INTO runs(run_id,status,corpus_hash,contract_json,corpus_json) VALUES(?,?,?,?,?)",
                             (run_id, "running", corpus["corpus_hash"] if corpus else None, _json(contract), _json(corpus) if corpus else None))
        return run_id

    def claim_run(self, corpus, contract, force=False):
        """Check activity/reuse and reserve a build in one SQLite write transaction."""
        with self._lock:
            if self._lease is not None:
                raise ValueError("An indexing build is already running")
            if not self._acquire_lease():
                raise ValueError("An indexing build is already running")
            try:
                self._db.execute("BEGIN IMMEDIATE")
                if self._db.execute("SELECT 1 FROM runs WHERE status='running' LIMIT 1").fetchone():
                    raise ValueError("An indexing build is already running")
                active = self._active_id()
                if active and not force:
                    row = self._db.execute("SELECT corpus_hash,contract_json,status FROM runs WHERE run_id=?", (active,)).fetchone()
                    if row and row["status"] == "complete" and row["corpus_hash"] == corpus["corpus_hash"] and json.loads(row["contract_json"]) == contract:
                        self._db.commit()
                        self._release_lease()
                        return None
                run_id = uuid.uuid4().hex
                self._db.execute("INSERT INTO runs(run_id,status,corpus_hash,contract_json,corpus_json) VALUES(?,?,?,?,?)",
                                 (run_id, "running", corpus["corpus_hash"], _json(contract), _json(corpus)))
                self._db.commit()
                return run_id
            except BaseException:
                self._db.rollback()
                self._release_lease()
                raise

    def reject_input(self, code, contract, message):
        run_id = self.start_run(None, contract)
        row = {"batch_id": uuid.uuid4().hex, "run_id": run_id, "strategy": None,
               "status": "input_rejected", "api_called": False, "input_policy": {"passed": False, "code": code},
               "output_policy": {"passed": None, "code": "not_called"}, "policy": "input_rejected",
               "input_tokens": None, "output_tokens": 0, "output_tokens_status": "not_applicable",
               "cached_input_tokens": None, "cached_input_tokens_status": "not_reported",
               "reasoning_tokens": None, "reasoning_tokens_status": "not_applicable",
               "total_tokens": None, "cost_usd": None, "provider_usage": None,
               "model": contract["model"], "tariff": contract["tariff"]}
        self.start_batch(row)
        self.finish_batch(row)
        self.fail_run(run_id, message)

    def save_documents(self, run_id, documents):
        with self._lock, self._db:
            self._db.executemany("INSERT INTO documents(run_id,file,document_json) VALUES(?,?,?)",
                                 [(run_id, doc["file"], _json(doc)) for doc in documents])

    def start_batch(self, record):
        with self._lock, self._db:
            self._db.execute("INSERT INTO batches(batch_id,run_id,strategy,status,api_called,input_tokens,total_tokens,cost_usd,record_json) VALUES(?,?,?,?,?,?,?,?,?)",
                             (record["batch_id"], record["run_id"], record["strategy"], "pending", int(record["api_called"]), None, None, None, _json(record)))

    def finish_batch(self, record):
        with self._lock, self._db:
            self._db.execute("UPDATE batches SET status=?,api_called=?,input_tokens=?,total_tokens=?,cost_usd=?,record_json=? WHERE batch_id=?",
                             (record["status"], int(record["api_called"]), record["input_tokens"], record["total_tokens"], record["cost_usd"], _json(record), record["batch_id"]))

    def save_chunks(self, run_id, strategy, offset, chunks):
        with self._lock, self._db:
            self._db.executemany("INSERT INTO chunks(run_id,strategy,ordinal,chunk_id,text,embedding_json,chunk_json) VALUES(?,?,?,?,?,?,?)",
                                 [(run_id, strategy, offset+i, chunk["chunk_id"], chunk["text"], _json(chunk["embedding"]), _json(chunk))
                                  for i, chunk in enumerate(chunks)])

    def publish(self, run_id, metrics):
        with self._lock, self._db:
            row = self._db.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if not row or row["status"] != "running":
                raise ValueError("Build is not active")
            self._db.execute("UPDATE runs SET status='complete',metrics_json=? WHERE run_id=?", (_json(metrics), run_id))
            self._db.execute("INSERT INTO metadata(key,value) VALUES('active_run_id',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (run_id,))
        self._release_lease()

    def fail_run(self, run_id, message):
        with self._lock, self._db:
            self._db.execute("UPDATE runs SET status='failed',error=? WHERE run_id=? AND status='running'", (message, run_id))
        self._release_lease()

    def ledger(self):
        with self._lock:
            rows = self._db.execute("SELECT record_json FROM batches ORDER BY rowid").fetchall()
            return [json.loads(row["record_json"]) for row in rows]

    def _cumulative(self, rows):
        known = sum(row["cost_usd"] for row in rows if row["cost_usd"] is not None)
        complete = all(not row["api_called"] or row["cost_usd"] is not None for row in rows)
        return {"known_cost_usd": known, "complete": complete}

    def _run_rows(self):
        return [dict(row) for row in self._db.execute("SELECT run_id,status,corpus_hash,error,started_at FROM runs ORDER BY rowid").fetchall()]

    def _active_data(self, include_vectors=True):
        active = self._active_id()
        if not active:
            return None, [], {}
        row = self._db.execute("SELECT corpus_json,contract_json,metrics_json FROM runs WHERE run_id=? AND status='complete'", (active,)).fetchone()
        if not row:
            return None, [], {}
        documents = [json.loads(item["document_json"]) for item in self._db.execute("SELECT document_json FROM documents WHERE run_id=? ORDER BY rowid", (active,)).fetchall()]
        metrics = json.loads(row["metrics_json"])
        indexes = {}
        for strategy in ("fixed", "structural"):
            chunks = [json.loads(item["chunk_json"]) for item in self._db.execute("SELECT chunk_json FROM chunks WHERE run_id=? AND strategy=? ORDER BY ordinal", (active, strategy)).fetchall()] if include_vectors else []
            indexes[strategy] = {"run_id": active, "corpus_hash": json.loads(row["corpus_json"])["corpus_hash"],
                                 "chunks": chunks, "metrics": metrics[strategy]}
        return row, documents, indexes

    def export(self):
        with self._lock:
            active, documents, indexes = self._active_data()
            ledger = self.ledger()
            contract = json.loads(active["contract_json"]) if active else {}
            comparison = json.loads(active["metrics_json"]) if active else {}
            return {"schema_version": 1, "model": contract.get("model"), "dimensions": contract.get("dimensions"),
                    "corpus": json.loads(active["corpus_json"]) if active else None,
                    "documents": documents, "indexes": indexes, "comparison": comparison,
                    "ledger": ledger, "runs": self._run_rows(), "cumulative": self._cumulative(ledger)}

    def summary(self):
        with self._lock:
            active, _, indexes = self._active_data(include_vectors=False)
            runs = self._run_rows()
            ledger = self.ledger()
            current = runs[-1] if runs else None
            return {"status": current["status"] if current else "empty",
                    "active_run_id": self._active_id(), "error": current["error"] if current else None,
                    "corpus": json.loads(active["corpus_json"]) if active else None,
                    "comparison": json.loads(active["metrics_json"]) if active else {},
                    "runs": runs, "cumulative": self._cumulative(ledger),
                    "progress": {"batches": len(ledger), "current_strategy": ledger[-1]["strategy"] if ledger else None}}

    def chunks(self, strategy, offset=0, limit=20):
        if strategy not in ("fixed", "structural") or type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid chunk query")
        with self._lock:
            active = self._active_id()
            if not active:
                return []
            rows = self._db.execute("SELECT chunk_json FROM chunks WHERE run_id=? AND strategy=? ORDER BY ordinal LIMIT ? OFFSET ?", (active, strategy, limit, offset)).fetchall()
            return [json.loads(row["chunk_json"]) for row in rows]

"""Isolated, durable review claims and Day 31 call receipts."""

import json
import math
import os
import sqlite3
from pathlib import Path

from graphics.retrieval import Retriever
from .review_store import ReviewStore


_REVIEW_STEPS = {
    "preparing": {"retrieving"},
    "retrieving": {"filtering"},
    "filtering": {"analyzing"},
    "analyzing": {"program_check"},
    "program_check": {"semantic_check"},
    "semantic_check": set(),
}
_CALL_REVIEW_STATES = {"filter": "filtering", "generator": "analyzing",
                       "validator": "semantic_check"}
_REVIEW_TERMINALS = {
    "preparing": {"incomplete", "error"},
    "retrieving": {"incomplete", "error"},
    "filtering": {"incomplete", "rejected", "error"},
    "analyzing": {"error"},
    "program_check": {"rejected", "error"},
    "semantic_check": {"accepted", "incomplete", "rejected", "error"},
}


class ReviewLedger:
    """One review database and budget shared by every run in its directory."""

    def __init__(self, settings, data_dir, *, embedding_transport=None):
        self.settings = settings
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = ReviewStore(self.data_dir / "review.sqlite3")
        self.embedding_transport = embedding_transport or self._embedding_transport
        with self.store._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS review_runs (
                run_key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                state TEXT NOT NULL, chat_id TEXT, branch_id TEXT, turn_id TEXT,
                events TEXT NOT NULL, result TEXT)""")
            db.execute("""CREATE TABLE IF NOT EXISTS review_call_claims (
                run_key TEXT NOT NULL, stage TEXT NOT NULL,
                PRIMARY KEY(run_key,stage))""")
            row = db.execute("SELECT id FROM users WHERE username='__review_runtime__'").fetchone()
        if row is None:
            try:
                self.user_id = self.store.create_user("__review_runtime__", os.urandom(24).hex())
            except ValueError:
                with self.store._connect() as db:
                    row = db.execute("SELECT id FROM users WHERE username='__review_runtime__'").fetchone()
                self.user_id = row["id"]
        else:
            self.user_id = row["id"]
        self.active_key = None

    def claim(self, run_key, fingerprint):
        if (not isinstance(run_key, str) or not run_key or len(run_key) > 128
                or not all(ch.isalnum() or ch in "-_.:" for ch in run_key)):
            raise ValueError("invalid_run_key")
        if not isinstance(fingerprint, str) or len(fingerprint) != 64:
            raise ValueError("invalid_fingerprint")
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT fingerprint,state,result FROM review_runs WHERE run_key=?",
                             (run_key,)).fetchone()
            if row is not None:
                if row["fingerprint"] != fingerprint:
                    return {"kind": "mismatch"}
                if row["result"] is not None:
                    return {"kind": "stored", "result": json.loads(row["result"])}
                return {"kind": "pending", "state": row["state"]}
            db.execute("INSERT INTO review_runs(run_key,fingerprint,state,events) VALUES(?,?,?,?)",
                       (run_key, fingerprint, "preparing", json.dumps(["preparing"])))
        return {"kind": "new"}

    def begin(self, run_key):
        chat = self.store.create_chat(self.user_id, "Служебный контур ревью")
        turn = self.store.begin_turn(self.user_id, chat["branch_id"], "Служебный запуск ревью")
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("UPDATE review_runs SET chat_id=?,branch_id=?,turn_id=? "
                                 "WHERE run_key=? AND state='preparing' AND turn_id IS NULL AND result IS NULL",
                                 (chat["id"], chat["branch_id"], turn["id"], run_key)).rowcount
            if changed != 1:
                raise RuntimeError("review_claim_lost")
        self.active_key = run_key

    def _row(self, run_key=None):
        key = run_key or self.active_key
        with self.store._connect() as db:
            row = db.execute("SELECT * FROM review_runs WHERE run_key=?", (key,)).fetchone()
        if row is None:
            raise ValueError("unknown_run")
        return dict(row)

    def previous_terminal_run_key(self, current_run_key):
        """Only the latest completed claim may supply a local diagnostic index."""
        with self.store._connect() as db:
            row = db.execute(
                "SELECT run_key FROM review_runs WHERE run_key<>? AND result IS NOT NULL "
                "ORDER BY rowid DESC LIMIT 1", (current_run_key,)).fetchone()
        return row["run_key"] if row is not None else None

    def advance(self, run_key, store_state):
        row = self._row(run_key)
        if row["result"] is not None or row["turn_id"] is None:
            raise RuntimeError("review_not_active")
        self.store.move_turn(row["turn_id"], store_state)

    def event(self, run_key, state):
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state,events,turn_id FROM review_runs WHERE run_key=? AND result IS NULL",
                             (run_key,)).fetchone()
            if row is None or row["turn_id"] is None:
                raise RuntimeError("review_not_active")
            if state not in _REVIEW_STEPS.get(row["state"], set()):
                raise ValueError("invalid_review_transition")
            events = json.loads(row["events"])
            events.append(state)
            db.execute("UPDATE review_runs SET state=?,events=? WHERE run_key=?",
                       (state, json.dumps(events), run_key))

    def finish(self, run_key, result):
        if result.get("status") not in {"accepted", "incomplete", "rejected", "error"}:
            raise ValueError("invalid_terminal_status")
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state,events,result,turn_id FROM review_runs WHERE run_key=?",
                             (run_key,)).fetchone()
            if row is None or row["result"] is not None:
                raise RuntimeError("review_not_active")
            if result["status"] not in _REVIEW_TERMINALS.get(row["state"], set()):
                raise ValueError("invalid_review_terminal_transition")
            if result["status"] == "accepted":
                validators = db.execute(
                    "SELECT status,usage,cost_estimated_usd,output_policy FROM calls "
                    "WHERE turn_id=? AND stage='validator'", (row["turn_id"],)).fetchall()
                if (len(validators) != 1 or validators[0]["status"] != "complete"
                        or validators[0]["usage"] is None
                        or validators[0]["cost_estimated_usd"] is None
                        or validators[0]["output_policy"] is None
                        or json.loads(validators[0]["output_policy"]).get("ok") is not True
                        or result.get("semantic_validation") != "accepted"
                        or result.get("review_accepted") is not True):
                    raise ValueError("review_accept_requires_validated_receipt")
            events = json.loads(row["events"]) + [result["status"]]
            result = {**result, "events": events}
            db.execute("UPDATE review_runs SET state=?,events=?,result=? WHERE run_key=?",
                       (result["status"], json.dumps(events),
                        json.dumps(result, ensure_ascii=False, allow_nan=False), run_key))
        return result

    def claim_stage(self, run_key, stage):
        expected = _CALL_REVIEW_STATES.get(stage)
        if expected is None:
            raise ValueError("invalid_review_call_stage")
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state,result,turn_id FROM review_runs WHERE run_key=?",
                             (run_key,)).fetchone()
            if row is None or row["result"] is not None or row["turn_id"] is None:
                raise RuntimeError("review_not_active")
            if row["state"] != expected:
                raise ValueError("review_call_out_of_order")
            try:
                db.execute("INSERT INTO review_call_claims(run_key,stage) VALUES(?,?)",
                           (run_key, stage))
            except sqlite3.IntegrityError:
                raise RuntimeError("review_stage_already_called") from None

    def usage(self, run_key):
        row = self._row(run_key)
        instance = self.store.ledger(self.user_id)
        all_calls = instance["calls"]
        calls = [call for call in all_calls if call["turn_id"] == row["turn_id"]]
        complete = all(c["status"] == "not_sent" or (
            c["status"] in {"complete", "rejected"} and c["usage"] is not None
            and c["cost_estimated_usd"] is not None) for c in calls)
        known = sum(c["cost_estimated_usd"] or 0.0 for c in calls)
        return {"calls": calls, "complete": complete,
                "known_cost_estimated_usd": known,
                "cost_estimated_usd": known if complete else None,
                "input_tokens": sum(c["usage"].get("input_tokens", 0) for c in calls if c["usage"]),
                "output_tokens": sum(c["usage"].get("output_tokens", 0) for c in calls if c["usage"]),
                "total_tokens": sum(c["usage"].get("total_tokens", 0) for c in calls if c["usage"]),
                "instance_accounting": {
                    "complete": instance["complete"],
                    "known_cost_estimated_usd": instance["known_cost_estimated_usd"],
                    "cost_estimated_usd": instance["cost_estimated_usd"],
                    "budget_usd": self.settings.budget_usd,
                    "unknown_call_ids": [c["id"] for c in all_calls if c["status"] != "not_sent" and
                                         (c["status"] not in {"complete", "rejected"} or c["usage"] is None or
                                          c["cost_estimated_usd"] is None)],
                    "operator_resolutions": self.store.operator_resolutions()}}

    def _embedding_transport(self, texts, query):
        retriever = Retriever(self.settings)
        digest_before = retriever._model_digest()
        vectors, receipt = retriever._embed(texts, query=query)
        observed = dict(receipt)
        observed["tag_digest_before"] = digest_before
        try:
            observed["tag_digest_after"] = retriever._model_digest()
        except Exception:
            observed["tag_digest_after"] = None
        # /api/tags brackets the call; it does not attest which digest served
        # /api/embed. Keep that observation boundary explicit in the receipt.
        observed["digest_observation"] = "stable_tags_only"
        observed["model_digest"] = digest_before
        return vectors, observed

    def embed(self, texts, *, query):
        if (not isinstance(texts, list) or not texts or len(texts) > 64
                or any(not isinstance(t, str) or not t or len(t.encode("utf-8")) > 16000 for t in texts)
                or type(query) is not bool):
            raise ValueError("invalid_embedding_input")
        row = self._row()
        if row["result"] is not None or row["state"] != "retrieving":
            raise RuntimeError("review_embedding_out_of_order")
        call_id = self.store.start_call(self.user_id, row["branch_id"], row["turn_id"],
                                        "embedding", self.settings.embedding_model,
                                        {"stage": "local_embedding", "text_count": len(texts),
                                         "service_mode": "local_embedding", "bundle_debit_usd": None},
                                        budget_usd=self.settings.budget_usd)
        self.store.set_call_policy(call_id, input_policy={"ok": True, "code": "bounded_embedding_input"})
        try:
            self.store.mark_call_dispatched(call_id)
            vectors, receipt = self.embedding_transport(texts, query)
        except Exception:
            self.store.finish_call(call_id, None, None, status="unknown")
            self.store.set_call_policy(call_id, output_policy={"ok": False, "code": "transport_unknown"})
            raise RuntimeError("embedding_result_unknown") from None
        count = receipt.get("prompt_eval_count") if isinstance(receipt, dict) else None
        if type(count) is not int or count < 0:
            self.store.finish_call(call_id, None, None, status="unknown",
                                   metadata={"embedding": receipt if isinstance(receipt, dict) else {}})
            self.store.set_call_policy(call_id, output_policy={"ok": False, "code": "usage_unknown"})
            raise ValueError("embedding_usage_unknown")
        usage = {"input_tokens": count, "output_tokens": 0, "total_tokens": count,
                 "cached_input_tokens": None, "reasoning_tokens": None}
        self.store.finish_call(call_id, usage, 0.0, metadata={"embedding": receipt})
        identity_valid = (receipt.get("model") == self.settings.embedding_model
                          and isinstance(receipt.get("model_digest"), str)
                          and bool(receipt["model_digest"])
                          and type(receipt.get("dimensions")) is int
                          and receipt["dimensions"] == self.settings.embedding_dimensions)
        if "tag_digest_before" in receipt or "tag_digest_after" in receipt:
            identity_valid = (identity_valid
                              and receipt.get("tag_digest_before") == receipt["model_digest"]
                              and receipt.get("tag_digest_after") == receipt["model_digest"])
        if "response_model_digest" in receipt:
            identity_valid = (identity_valid
                              and receipt["response_model_digest"] == receipt["model_digest"])
        valid = (identity_valid and isinstance(vectors, list) and len(vectors) == len(texts)
                 and all(isinstance(v, list) and len(v) == self.settings.embedding_dimensions
                         and all(type(x) in (float, int) and math.isfinite(x) for x in v)
                         for v in vectors))
        self.store.set_call_policy(call_id, output_policy={"ok": valid,
                                                         "code": "vectors_valid" if valid else
                                                         "embedding_identity_or_vectors_invalid"})
        if not valid:
            raise ValueError("invalid_embedding_vectors")
        return vectors, receipt

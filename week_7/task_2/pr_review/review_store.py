"""Review-only operator resolution without rewriting lost call receipts."""

import json
import re
from datetime import datetime, timezone

from graphics.store import Store


_RESOLUTION = "ack_local_embedding_usage_loss"
_BUDGET_QUERY = "SELECT status,usage,cost_estimated_usd FROM calls"


class _FilteredCursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class _UnresolvedReceipts:
    """Pass an exact receipt view to the unchanged Store budget guard."""

    def __init__(self, db, excluded):
        self.db = db
        self.excluded = excluded

    def execute(self, query):
        if query != _BUDGET_QUERY:
            raise RuntimeError("Store budget query changed; review admission stopped")
        return _FilteredCursor([row for row in self.db.execute(
            "SELECT id,status,usage,cost_estimated_usd FROM calls") if row["id"] not in self.excluded])


class ReviewStore(Store):
    """Only a terminal Day 32 local embedding loss may be acknowledged."""

    def __init__(self, path):
        super().__init__(path)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS review_operator_resolutions (
                call_id TEXT PRIMARY KEY, resolution TEXT NOT NULL,
                resolved_at_utc TEXT NOT NULL, authorization_ref TEXT NOT NULL,
                reason TEXT NOT NULL)""")

    @staticmethod
    def _eligible(db, call_id):
        row = db.execute("""SELECT c.status,c.stage,c.model,c.request_dispatched,c.usage,
                                  c.cost_estimated_usd,c.metadata,r.state AS run_state,
                                  r.result AS run_result
                           FROM calls c LEFT JOIN review_runs r
                             ON r.turn_id=c.turn_id AND r.branch_id=c.branch_id
                             AND r.chat_id=c.chat_id WHERE c.id=?""", (call_id,)).fetchone()
        if (row is None or row["status"] != "unknown" or row["stage"] != "embedding"
                or row["model"] != "qwen3-embedding:0.6b" or row["request_dispatched"] != 1
                or row["usage"] is not None or row["cost_estimated_usd"] is not None
                or row["run_state"] != "error" or row["run_result"] is None):
            return False
        try:
            metadata = json.loads(row["metadata"])
            result = json.loads(row["run_result"])
        except (TypeError, ValueError):
            return False
        return (isinstance(metadata, dict) and metadata.get("stage") == "local_embedding"
                and metadata.get("service_mode") == "local_embedding"
                and isinstance(result, dict) and result.get("status") == "error")

    def ack_local_embedding_usage_loss(self, call_id, authorization_ref, reason):
        if not isinstance(call_id, str) or re.fullmatch(r"[0-9a-f]{32}", call_id) is None:
            raise ValueError("invalid_call_id")
        if (not isinstance(authorization_ref, str) or
                re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{7,127}", authorization_ref) is None):
            raise ValueError("invalid_authorization_ref")
        if (not isinstance(reason, str) or not 20 <= len(reason) <= 300
                or reason != reason.strip() or any(ord(char) < 32 or ord(char) == 127 for char in reason)):
            raise ValueError("invalid_reason")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not self._eligible(db, call_id):
                raise ValueError("ineligible_local_embedding_call")
            existing = db.execute("SELECT * FROM review_operator_resolutions WHERE call_id=?",
                                  (call_id,)).fetchone()
            if existing is not None:
                if (existing["resolution"] != _RESOLUTION or
                        existing["authorization_ref"] != authorization_ref or
                        existing["reason"] != reason):
                    raise ValueError("operator_resolution_conflict")
                recorded = dict(existing)
            else:
                recorded = {"call_id": call_id, "resolution": _RESOLUTION,
                            "resolved_at_utc": datetime.now(timezone.utc).isoformat(),
                            "authorization_ref": authorization_ref, "reason": reason}
                db.execute("""INSERT INTO review_operator_resolutions
                           (call_id,resolution,resolved_at_utc,authorization_ref,reason)
                           VALUES(:call_id,:resolution,:resolved_at_utc,:authorization_ref,:reason)""", recorded)
        return recorded

    def operator_resolutions(self):
        with self._connect() as db:
            rows = db.execute("SELECT call_id,resolution,resolved_at_utc FROM review_operator_resolutions "
                              "ORDER BY resolved_at_utc,call_id").fetchall()
        return [dict(row) for row in rows]

    def _check_budget_in_transaction(self, db, reserve_usd, budget_usd):
        resolutions = db.execute("SELECT call_id,resolution FROM review_operator_resolutions").fetchall()
        if not resolutions:
            return super()._check_budget_in_transaction(db, reserve_usd, budget_usd)
        excluded = {row["call_id"] for row in resolutions if row["resolution"] == _RESOLUTION
                    and self._eligible(db, row["call_id"])}
        return super()._check_budget_in_transaction(_UnresolvedReceipts(db, excluded),
                                                    reserve_usd, budget_usd)

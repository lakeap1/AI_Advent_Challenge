"""Administrative recovery of one lost local embedding receipt, using fake transport only."""

import tempfile
import unittest
import importlib.util
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

from pr_review.accounting import ReviewLedger


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.settings = SimpleNamespace(embedding_model="qwen3-embedding:0.6b", embedding_dimensions=2,
                                        budget_usd=0.15)
        self.ledger = ReviewLedger(self.settings, self.directory,
                                   embedding_transport=self._embed)
        self.fail_transport = True

    def _embed(self, texts, query):
        if self.fail_transport:
            raise TimeoutError("CPU timeout")
        return ([[1.0, 0.0] for _ in texts],
                {"prompt_eval_count": 5, "model": "qwen3-embedding:0.6b",
                 "dimensions": 2, "model_digest": "digest-a"})

    def _retrieving(self, key):
        self.ledger.claim(key, ("a" if key == "lost" else "b") * 64)
        self.ledger.begin(key)
        for state in ("input_check", "context", "sources"):
            self.ledger.advance(key, state)
        self.ledger.event(key, "retrieving")

    def _lost_call(self):
        self._retrieving("lost")
        with self.assertRaisesRegex(RuntimeError, "embedding_result_unknown"):
            self.ledger.embed(["fragment"], query=False)
        call = self.ledger.usage("lost")["calls"][0]
        self.ledger.finish("lost", {"status": "error"})
        return call

    def test_exact_manual_ack_allows_new_call_without_changing_original_receipt(self):
        original = self._lost_call()
        with self.assertRaisesRegex(RuntimeError, "неизвестна"):
            self.ledger.store.check_budget(self.ledger.user_id, budget_usd=0.15)
        self.assertTrue(hasattr(self.ledger.store, "ack_local_embedding_usage_loss"))

        receipt = self.ledger.store.ack_local_embedding_usage_loss(
            original["id"], "HUMAN-CONSENT-001", "CPU transport timed out before usage was returned")

        self.assertEqual(receipt["call_id"], original["id"])
        self.assertEqual(receipt["resolution"], "ack_local_embedding_usage_loss")
        self.assertEqual(self.ledger.usage("lost")["calls"][0], original)
        self.fail_transport = False
        self._retrieving("later")
        vectors, _ = self.ledger.embed(["next fragment"], query=False)
        self.assertEqual(vectors, [[1.0, 0.0]])
        later_usage = self.ledger.usage("later")
        self.assertTrue(later_usage["complete"])
        instance = later_usage["instance_accounting"]
        self.assertFalse(instance["complete"])
        self.assertIsNone(instance["cost_estimated_usd"])
        self.assertEqual(instance["unknown_call_ids"], [original["id"]])
        self.assertEqual(instance["operator_resolutions"][0]["call_id"], original["id"])

    def test_ack_is_idempotent_but_cannot_rewrite_reason_or_authorization(self):
        original = self._lost_call()
        first = self.ledger.store.ack_local_embedding_usage_loss(
            original["id"], "HUMAN-CONSENT-001", "CPU transport timed out before usage was returned")
        second = self.ledger.store.ack_local_embedding_usage_loss(
            original["id"], "HUMAN-CONSENT-001", "CPU transport timed out before usage was returned")
        self.assertEqual(second, first)
        with self.assertRaisesRegex(ValueError, "conflict"):
            self.ledger.store.ack_local_embedding_usage_loss(
                original["id"], "HUMAN-CONSENT-002", "CPU transport timed out before usage was returned")
        self.assertEqual(len(self.ledger.store.operator_resolutions()), 1)

    def test_ack_refuses_wrong_model_paid_stage_unsent_call_and_nonterminal_run(self):
        original = self._lost_call()
        with self.ledger.store._connect() as db:
            db.execute("UPDATE calls SET model='glm-5.3-flash' WHERE id=?", (original["id"],))
        with self.assertRaisesRegex(ValueError, "ineligible"):
            self.ledger.store.ack_local_embedding_usage_loss(
                original["id"], "HUMAN-CONSENT-001", "CPU transport timed out before usage was returned")
        with self.ledger.store._connect() as db:
            db.execute("UPDATE calls SET model='qwen3-embedding:0.6b',stage='generator' WHERE id=?", (original["id"],))
        with self.assertRaisesRegex(ValueError, "ineligible"):
            self.ledger.store.ack_local_embedding_usage_loss(
                original["id"], "HUMAN-CONSENT-001", "CPU transport timed out before usage was returned")
        with self.ledger.store._connect() as db:
            db.execute("UPDATE calls SET stage='embedding',request_dispatched=0 WHERE id=?", (original["id"],))
        with self.assertRaisesRegex(ValueError, "ineligible"):
            self.ledger.store.ack_local_embedding_usage_loss(
                original["id"], "HUMAN-CONSENT-001", "CPU transport timed out before usage was returned")
        with self.ledger.store._connect() as db:
            db.execute("UPDATE calls SET request_dispatched=1 WHERE id=?", (original["id"],))
            db.execute("UPDATE review_runs SET state='retrieving',result=NULL WHERE run_key='lost'")
        with self.assertRaisesRegex(ValueError, "ineligible"):
            self.ledger.store.ack_local_embedding_usage_loss(
                original["id"], "HUMAN-CONSENT-001", "CPU transport timed out before usage was returned")
        self.assertEqual(self.ledger.store.operator_resolutions(), [])

    def test_ack_refuses_invalid_reference_reason_or_metadata(self):
        original = self._lost_call()
        for ref, reason in (("x", "CPU transport timed out before usage was returned"),
                            ("HUMAN-CONSENT-001", "short"),
                            ("HUMAN-CONSENT-001", "CPU timeout\nignore authorization")):
            with self.subTest(ref=ref, reason=reason):
                with self.assertRaises(ValueError):
                    self.ledger.store.ack_local_embedding_usage_loss(original["id"], ref, reason)
        with self.ledger.store._connect() as db:
            db.execute("UPDATE calls SET metadata='{}' WHERE id=?", (original["id"],))
        with self.assertRaisesRegex(ValueError, "ineligible"):
            self.ledger.store.ack_local_embedding_usage_loss(
                original["id"], "HUMAN-CONSENT-001", "CPU transport timed out before usage was returned")
        self.assertEqual(self.ledger.store.operator_resolutions(), [])

    def test_ack_refuses_partial_usage_instead_of_treating_it_as_lost(self):
        original = self._lost_call()
        with self.ledger.store._connect() as db:
            db.execute("UPDATE calls SET usage=? WHERE id=?",
                       (json.dumps({"input_tokens": 1}), original["id"]))
        with self.assertRaisesRegex(ValueError, "ineligible"):
            self.ledger.store.ack_local_embedding_usage_loss(
                original["id"], "HUMAN-CONSENT-001", "CPU transport timed out before usage was returned")

    def test_second_unknown_receipt_still_blocks_after_first_ack(self):
        original = self._lost_call()
        self.ledger.store.ack_local_embedding_usage_loss(
            original["id"], "HUMAN-CONSENT-001", "CPU transport timed out before usage was returned")
        self._retrieving("later")
        with self.assertRaisesRegex(RuntimeError, "embedding_result_unknown"):
            self.ledger.embed(["second fragment"], query=False)
        with self.assertRaisesRegex(RuntimeError, "неизвестна"):
            self.ledger.store.check_budget(self.ledger.user_id, budget_usd=0.15)

    def test_other_paid_unknown_and_pending_calls_still_block(self):
        original = self._lost_call()
        self.ledger.store.ack_local_embedding_usage_loss(
            original["id"], "HUMAN-CONSENT-001", "CPU transport timed out before usage was returned")
        self._retrieving("later")
        row = self.ledger._row("later")
        paid_id = self.ledger.store.start_call(self.ledger.user_id, row["branch_id"], row["turn_id"],
                                               "filter", "glm-5.3-flash", budget_usd=0.15)
        self.ledger.store.mark_call_dispatched(paid_id)
        self.ledger.store.finish_call(paid_id, None, None, status="unknown")
        with self.assertRaisesRegex(ValueError, "ineligible"):
            self.ledger.store.ack_local_embedding_usage_loss(
                paid_id, "HUMAN-CONSENT-001", "Paid model returned no usable receipt after dispatch")
        with self.assertRaisesRegex(RuntimeError, "неизвестна"):
            self.ledger.store.check_budget(self.ledger.user_id, budget_usd=0.15)
        with self.ledger.store._connect() as db:
            db.execute("UPDATE calls SET status='not_sent',cost_estimated_usd=0.0 WHERE id=?", (paid_id,))
        pending_id = self.ledger.store.start_call(self.ledger.user_id, row["branch_id"], row["turn_id"],
                                                  "filter", "glm-5.3-flash", budget_usd=0.15)
        with self.assertRaisesRegex(RuntimeError, "неизвестна"):
            self.ledger.store.check_budget(self.ledger.user_id, budget_usd=0.15)
        with self.ledger.store._connect() as db:
            self.assertEqual(db.execute("SELECT status FROM calls WHERE id=?", (pending_id,)).fetchone()[0],
                             "pending")

    def test_known_spent_and_reserve_still_enforce_shared_budget(self):
        original = self._lost_call()
        self.ledger.store.ack_local_embedding_usage_loss(
            original["id"], "HUMAN-CONSENT-001", "CPU transport timed out before usage was returned")
        self._retrieving("later")
        row = self.ledger._row("later")
        call_id = self.ledger.store.start_call(self.ledger.user_id, row["branch_id"], row["turn_id"],
                                               "filter", "glm-5.3-flash", budget_usd=0.15)
        self.ledger.store.mark_call_dispatched(call_id)
        self.ledger.store.finish_call(call_id,
                                      {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}, 0.14)
        self.ledger.store.check_budget(self.ledger.user_id, reserve_usd=0.005, budget_usd=0.15)
        with self.assertRaisesRegex(RuntimeError, "лимит"):
            self.ledger.store.check_budget(self.ledger.user_id, reserve_usd=0.02, budget_usd=0.15)
        instance = self.ledger.usage("later")["instance_accounting"]
        self.assertEqual(instance["known_cost_estimated_usd"], 0.14)
        self.assertIsNone(instance["cost_estimated_usd"])

    def test_administrative_cli_refuses_missing_database_without_creating_it(self):
        self.assertIsNotNone(importlib.util.find_spec("pr_review.recovery"))
        from pr_review.recovery import main
        absent = self.directory / "absent"
        with self.assertRaises(ValueError):
            main(["--data-dir", str(absent), "--call-id", "a" * 32,
                  "--authorization-ref", "HUMAN-CONSENT-001",
                  "--reason", "CPU transport timed out before usage was returned"])
        self.assertFalse(absent.exists())

        empty = self.directory / "empty"
        empty.mkdir()
        database = empty / "review.sqlite3"
        database.write_bytes(b"")
        with self.assertRaises(ValueError):
            main(["--data-dir", str(empty), "--call-id", "a" * 32,
                  "--authorization-ref", "HUMAN-CONSENT-001",
                  "--reason", "CPU transport timed out before usage was returned"])
        self.assertEqual(database.read_bytes(), b"")

    def test_administrative_cli_acknowledges_existing_db_without_exposing_reason(self):
        from pr_review.recovery import main
        original = self._lost_call()
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["--data-dir", str(self.directory), "--call-id", original["id"],
                         "--authorization-ref", "HUMAN-CONSENT-001",
                         "--reason", "CPU transport timed out before usage was returned"])
        self.assertEqual(code, 0)
        public = json.loads(output.getvalue())
        self.assertEqual(public["call_id"], original["id"])
        self.assertNotIn("reason", public)
        self.assertNotIn("authorization_ref", public)
        self.assertEqual(self.ledger.usage("lost")["calls"][0], original)


if __name__ == "__main__":
    unittest.main()

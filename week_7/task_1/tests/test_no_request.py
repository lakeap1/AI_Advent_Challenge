"""A missing HTTP dependency must not be represented as an unknown paid call."""
from pathlib import Path
from contextlib import contextmanager
import tempfile
import threading
import unittest
from unittest.mock import patch

from graphics.config import Settings
from graphics.provider import GLMProvider, ProviderError
from graphics.store import Store


class NoRequestTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.settings = Settings(root, root / "data", api_key="unit-test-key")
        self.store = Store(self.settings.db_path)
        self.user = self.store.create_user("tester", "long-fixture-password")
        self.branch = self.store.create_chat(self.user)["branch_id"]
        self.turn = self.store.begin_turn(self.user, self.branch, "Question")["id"]
        self.store.move_turn(self.turn, "input_check")
        self.store.move_turn(self.turn, "context")

    def tearDown(self):
        self.temporary.cleanup()

    def test_missing_httpx_is_caught_before_any_call_receipt(self):
        provider = GLMProvider(self.settings, self.store)
        with patch.dict("sys.modules", {"httpx": None}):
            with self.assertRaises(ProviderError):
                provider.call(self.user, self.branch, self.turn, "preparation", [])
        self.assertEqual(self.store.ledger(self.user)["calls"], [])
        self.store.check_budget(self.user)

    def test_proven_unsent_attempt_has_no_model_usage_and_does_not_block_budget(self):
        call = self.store.start_call(self.user, self.branch, self.turn, "preparation", "glm-5.3-flash")
        self.store.finish_call(call, None, 0.0, status="not_sent",
                               metadata={"api_request_sent": False, "reason": "missing_http_dependency"})
        receipt = self.store.ledger(self.user)
        self.assertEqual(receipt["calls"][0]["status"], "not_sent")
        self.assertIsNone(receipt["calls"][0]["usage"])
        self.assertEqual(receipt["cost_estimated_usd"], 0.0)
        self.assertTrue(receipt["complete"])
        self.store.check_budget(self.user)

    def test_not_sent_requires_explicit_evidence_and_no_invented_token_counts(self):
        call = self.store.start_call(self.user, self.branch, self.turn, "preparation", "glm-5.3-flash")
        with self.assertRaises(ValueError):
            self.store.finish_call(call, {"input_tokens": 0}, 0.0, status="not_sent")
        with self.assertRaises(RuntimeError):
            self.store.check_budget(self.user)

    def test_dispatched_attempt_cannot_be_reclassified_as_not_sent(self):
        call = self.store.start_call(self.user, self.branch, self.turn, "preparation", "glm-5.3-flash")
        self.store.mark_call_dispatched(call)
        with self.assertRaises(ValueError):
            self.store.finish_call(call, None, 0.0, status="not_sent",
                                   metadata={"api_request_sent": False})
        with self.assertRaises(RuntimeError):
            self.store.check_budget(self.user)

    def test_provider_marks_dispatch_before_invoking_transport(self):
        observed = []
        def fail_after_dispatch(payload):
            observed.append(self.store.ledger(self.user)["calls"][0]["request_dispatched"])
            raise TimeoutError()
        provider = GLMProvider(self.settings, self.store, transport=fail_after_dispatch)
        with self.assertRaises(ProviderError):
            provider.call(self.user, self.branch, self.turn, "preparation", [])
        self.assertEqual(observed, [True])
        self.assertEqual(self.store.ledger(self.user)["calls"][0]["status"], "unknown")

    def test_dispatch_and_unsent_completion_are_atomic_across_connections(self):
        call = self.store.start_call(self.user, self.branch, self.turn, "preparation", "glm-5.3-flash")
        read_done = threading.Event()
        release_finish = threading.Event()
        marker_done = threading.Event()
        results = {}
        original_connect = self.store._connect

        class PausedCursor:
            def __init__(self, cursor):
                self.cursor = cursor
            def fetchone(self):
                row = self.cursor.fetchone()
                self.cursor.fetchall()  # Finish the real SELECT before the interleaving.
                read_done.set()
                if not release_finish.wait(3):
                    raise TimeoutError("race test did not release the final writer")
                return row

        class ConnectionProxy:
            def __init__(self, connection):
                self.connection = connection
            def execute(self, sql, args=()):
                cursor = self.connection.execute(sql, args)
                if sql.startswith("SELECT status,metadata,request_dispatched FROM calls"):
                    return PausedCursor(cursor)
                return cursor

        @contextmanager
        def connected():
            with original_connect() as connection:
                yield ConnectionProxy(connection) if threading.current_thread().name == "unsent-finalizer" else connection

        def finalize():
            try:
                self.store.finish_call(call, None, 0.0, status="not_sent", metadata={"api_request_sent": False})
                results["unsent"] = True
            except ValueError:
                results["unsent"] = False

        def dispatch():
            try:
                self.store.mark_call_dispatched(call)
                results["dispatched"] = True
            except ValueError:
                results["dispatched"] = False
            finally:
                marker_done.set()

        with patch.object(self.store, "_connect", connected):
            first = threading.Thread(target=finalize, name="unsent-finalizer")
            first.start()
            self.assertTrue(read_done.wait(3))
            second = threading.Thread(target=dispatch)
            second.start()
            marker_done.wait(0.25)
            release_finish.set()
            first.join(4)
            second.join(4)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertIn("unsent", results)
        self.assertIn("dispatched", results)
        self.assertFalse(results["unsent"] and results["dispatched"])


if __name__ == "__main__":
    unittest.main()

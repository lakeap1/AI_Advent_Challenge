import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pr_review.accounting import ReviewLedger


class ReviewLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.settings = SimpleNamespace(embedding_model="qwen3-embedding:0.6b", embedding_dimensions=2,
                                        embedding_url="http://127.0.0.1:11436", embedding_threads=2,
                                        budget_usd=1.0)
        self.ledger = ReviewLedger(self.settings, Path(self.temp.name),
                                   embedding_transport=lambda texts, query: ([[1.0, 0.0] for _ in texts],
                                                                             {"prompt_eval_count": 7,
                                                                         "model": "qwen3-embedding:0.6b",
                                                                         "dimensions": 2,
                                                                         "model_digest": "digest-a"}))

    def test_claim_and_embedding_receipt_persist(self):
        claimed = self.ledger.claim("key", "a" * 64)
        self.assertEqual(claimed["kind"], "new")
        self.ledger.begin("key")
        self.ledger.advance("key", "input_check")
        self.ledger.advance("key", "context")
        self.ledger.advance("key", "sources")
        self.ledger.event("key", "retrieving")
        vectors, receipt = self.ledger.embed(["example"], query=False)
        self.assertEqual(vectors, [[1.0, 0.0]])
        self.assertEqual(receipt["prompt_eval_count"], 7)
        calls = self.ledger.usage("key")["calls"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["usage"]["input_tokens"], 7)
        self.assertEqual(calls[0]["cost_estimated_usd"], 0.0)
        self.assertEqual(self.ledger.claim("key", "a" * 64)["kind"], "pending")
        self.assertEqual(self.ledger.claim("key", "b" * 64)["kind"], "mismatch")

    def test_unknown_embedding_blocks_following_run(self):
        self.ledger.embedding_transport = lambda texts, query: ([[1.0, 0.0]],
                                                                 {"model": "qwen3-embedding:0.6b",
                                                                  "dimensions": 2,
                                                                  "model_digest": "digest-a"})
        self.ledger.claim("one", "a" * 64)
        self.ledger.begin("one")
        for state in ("input_check", "context", "sources"):
            self.ledger.advance("one", state)
        self.ledger.event("one", "retrieving")
        with self.assertRaises(ValueError):
            self.ledger.embed(["x"], query=True)
        self.assertFalse(self.ledger.usage("one")["complete"])
        self.ledger.claim("two", "b" * 64)
        self.ledger.begin("two")
        for state in ("input_check", "context", "sources"):
            self.ledger.advance("two", state)
        self.ledger.event("two", "retrieving")
        with self.assertRaises(RuntimeError):
            self.ledger.embed(["x"], query=True)

    def test_conflicting_embedding_identity_keeps_actual_receipt_and_usage(self):
        self.ledger.claim("identity", "c" * 64)
        self.ledger.begin("identity")
        for state in ("input_check", "context", "sources"):
            self.ledger.advance("identity", state)
        self.ledger.event("identity", "retrieving")
        self.ledger.embedding_transport = lambda texts, query: (
            [[1.0, 0.0]], {"prompt_eval_count": 7, "model": "wrong-model",
                           "model_digest": "digest-a", "dimensions": 3})
        with self.assertRaises(ValueError):
            self.ledger.embed(["x"], query=True)
        call = self.ledger.usage("identity")["calls"][0]
        self.assertEqual(call["status"], "complete")
        self.assertEqual(call["usage"]["input_tokens"], 7)
        self.assertEqual(call["cost_estimated_usd"], 0.0)
        self.assertEqual(call["metadata"]["embedding"]["model"], "wrong-model")
        self.assertEqual(call["metadata"]["embedding"]["dimensions"], 3)
        self.assertFalse(call["output_policy"]["ok"])

    def test_review_fsm_rejects_jumps_and_post_terminal_calls(self):
        self.ledger.claim("fsm", "d" * 64)
        self.ledger.begin("fsm")
        with self.assertRaises(ValueError):
            self.ledger.event("fsm", "semantic_check")
        with self.assertRaises(ValueError):
            self.ledger.finish("fsm", {"status": "accepted"})
        with self.assertRaises(ValueError):
            self.ledger.finish("fsm", {"status": "rejected"})
        self.ledger.event("fsm", "retrieving")
        self.ledger.finish("fsm", {"status": "error"})
        with self.assertRaises(RuntimeError):
            self.ledger.embed(["x"], query=True)
        with self.assertRaises(RuntimeError):
            self.ledger.claim_stage("fsm", "validator")

    def test_review_call_stage_cannot_repeat(self):
        self.ledger.claim("once", "e" * 64)
        self.ledger.begin("once")
        for state in ("retrieving", "filtering", "analyzing", "program_check", "semantic_check"):
            self.ledger.event("once", state)
        self.ledger.claim_stage("once", "validator")
        with self.assertRaises(RuntimeError):
            self.ledger.claim_stage("once", "validator")
        with self.assertRaises(ValueError):
            self.ledger.finish("once", {"status": "accepted", "review_accepted": True,
                                         "semantic_validation": "accepted"})

    def test_default_http_transport_preserves_observed_model_and_stable_tags(self):
        self.settings.data_dir = Path(self.temp.name)
        self.settings.app_root = Path(self.temp.name)
        ledger = ReviewLedger(self.settings, Path(self.temp.name))
        ledger.claim("default-http", "f" * 64)
        ledger.begin("default-http")
        for state in ("input_check", "context", "sources"):
            ledger.advance("default-http", state)
        ledger.event("default-http", "retrieving")
        calls = {"tags": 0, "embed": 0}

        class Response:
            def __init__(self, payload):
                self.payload = payload

            def raise_for_status(self):
                pass

            def json(self):
                return self.payload

        class Client:
            def __init__(self, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def get(self, url):
                calls["tags"] += 1
                return Response({"models": [{"name": "qwen3-embedding:0.6b", "digest": "digest-a"}]})

            def post(self, url, json):
                calls["embed"] += 1
                return Response({"model": "different-model", "embeddings": [[1.0, 0.0]],
                                 "prompt_eval_count": 7})

        with patch("graphics.retrieval.httpx.Client", Client):
            with self.assertRaises(ValueError):
                ledger.embed(["x"], query=False)
        self.assertEqual(calls, {"tags": 2, "embed": 1})
        call = ledger.usage("default-http")["calls"][0]
        self.assertEqual(call["status"], "complete")
        self.assertEqual(call["usage"]["input_tokens"], 7)
        self.assertEqual(call["metadata"]["embedding"]["model"], "different-model")
        self.assertEqual(call["metadata"]["embedding"]["dimensions"], 2)
        self.assertEqual(call["metadata"]["embedding"]["tag_digest_before"], "digest-a")
        self.assertEqual(call["metadata"]["embedding"]["tag_digest_after"], "digest-a")
        self.assertFalse(call["output_policy"]["ok"])

    def test_changed_postflight_tag_rejects_but_keeps_known_usage(self):
        self.settings.data_dir = Path(self.temp.name)
        self.settings.app_root = Path(self.temp.name)
        ledger = ReviewLedger(self.settings, Path(self.temp.name))
        ledger.claim("tag-change", "1" * 64)
        ledger.begin("tag-change")
        for state in ("input_check", "context", "sources"):
            ledger.advance("tag-change", state)
        ledger.event("tag-change", "retrieving")
        calls = {"tags": 0}

        class Response:
            def __init__(self, payload):
                self.payload = payload

            def raise_for_status(self):
                pass

            def json(self):
                return self.payload

        class Client:
            def __init__(self, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def get(self, url):
                calls["tags"] += 1
                digest = "digest-a" if calls["tags"] == 1 else "digest-b"
                return Response({"models": [{"name": "qwen3-embedding:0.6b", "digest": digest}]})

            def post(self, url, json):
                return Response({"model": "qwen3-embedding:0.6b", "embeddings": [[1.0, 0.0]],
                                 "prompt_eval_count": 7})

        with patch("graphics.retrieval.httpx.Client", Client):
            with self.assertRaises(ValueError):
                ledger.embed(["x"], query=False)
        call = ledger.usage("tag-change")["calls"][0]
        self.assertEqual(call["status"], "complete")
        self.assertEqual(call["usage"]["input_tokens"], 7)
        self.assertEqual(call["metadata"]["embedding"]["tag_digest_after"], "digest-b")
        self.assertEqual(call["metadata"]["embedding"]["digest_observation"], "stable_tags_only")
        self.assertFalse(call["output_policy"]["ok"])

    def test_postflight_tag_failure_keeps_known_embedding_usage(self):
        self.settings.data_dir = Path(self.temp.name)
        self.settings.app_root = Path(self.temp.name)
        ledger = ReviewLedger(self.settings, Path(self.temp.name))
        ledger.claim("tag-failure", "2" * 64)
        ledger.begin("tag-failure")
        for state in ("input_check", "context", "sources"):
            ledger.advance("tag-failure", state)
        ledger.event("tag-failure", "retrieving")
        receipt = {"model": "qwen3-embedding:0.6b", "dimensions": 2,
                   "prompt_eval_count": 11}
        with patch("pr_review.accounting.Retriever._model_digest",
                   side_effect=["digest-a", RuntimeError("tags unavailable")]), \
             patch("pr_review.accounting.Retriever._embed",
                   return_value=([[1.0, 0.0]], receipt)):
            with self.assertRaises(ValueError):
                ledger.embed(["x"], query=False)
        call = ledger.usage("tag-failure")["calls"][0]
        self.assertEqual(call["status"], "complete")
        self.assertEqual(call["usage"]["input_tokens"], 11)
        self.assertEqual(call["cost_estimated_usd"], 0.0)
        self.assertIsNone(call["metadata"]["embedding"]["tag_digest_after"])
        self.assertFalse(call["output_policy"]["ok"])


if __name__ == "__main__":
    unittest.main()

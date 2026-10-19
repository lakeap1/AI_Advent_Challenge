"""Shared Day32 retrieval and semantic contracts consumed by the Day33 chat."""
import math
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from graphics.retrieval import Retriever
from graphics.validation import SemanticValidationError, validate_semantics


class SharedIntegrationTests(unittest.TestCase):
    def test_answer_support_and_review_use_one_call_with_trusted_criteria(self):
        seen = []

        def judge(payload):
            seen.append(payload)
            return {"accepted": True, "reason": "достаточно", "used_source_ids": ["S1"]}

        common = {"original_question": "Что с обращением?", "answer_kind": "support",
                  "context": {"profile": {"role": "QA"}, "conditions": {"depth": "short"}},
                  "sources": [{"label": "S1", "text": "FAQ"}],
                  "tool_results": {"ticket": {"receipt": {"operation_id": "confirmed"}}},
                  "draft": {"answer": "Обращение создано", "source_ids": ["S1"]}}
        result = validate_semantics(judge, common, mode="answer", allowed_source_ids=["S1"])
        self.assertTrue(result["accepted"])
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["tool_results"], common["tool_results"])
        self.assertEqual(seen[0]["context"], common["context"])
        self.assertEqual(seen[0]["allowed_labels"], ["S1"])
        self.assertIn("FAQ", seen[0]["validation_criteria"])
        self.assertIn("квитанц", seen[0]["validation_criteria"])
        self.assertIn("приват", seen[0]["validation_criteria"])

        seen.clear()
        validate_semantics(judge, {**common, "answer_kind": "explanation"},
                           mode="answer", allowed_source_ids=["S1"])
        self.assertNotIn("квитанц", seen[0]["validation_criteria"])
        seen.clear()
        validate_semantics(judge, {"review": "diff"}, mode="review", allowed_source_ids=["S1"])
        self.assertIn("контрольный ответ", seen[0]["validation_criteria"])

    def test_semantic_rejects_unallowed_and_duplicate_source_labels_without_retry(self):
        for labels in (["foreign"], ["S1", "S1"]):
            calls = []

            def judge(payload):
                calls.append(payload)
                return {"accepted": True, "reason": "bad", "used_source_ids": labels}

            with self.subTest(labels=labels), self.assertRaises(SemanticValidationError):
                validate_semantics(judge, {"draft": {}}, mode="answer", allowed_source_ids=["S1"])
            self.assertEqual(len(calls), 1)

    @staticmethod
    def _retriever():
        settings = SimpleNamespace(data_dir=Path("unused"), app_root=Path("unused"),
                                   embedding_dimensions=2, embedding_model="qwen-fixture",
                                   embedding_threads=1, embedding_url="http://127.0.0.1:1")

        class FixtureRetriever(Retriever):
            chunks = [
                {"chunk_id": "A", "title": "shader alpha", "section": "", "text": "material bright",
                 "embedding": [1.0, 0.0]},
                {"chunk_id": "B", "title": "lighting beta", "section": "", "text": "shader shadow",
                 "embedding": [0.8, 0.6]},
                {"chunk_id": "C", "title": "texture gamma", "section": "", "text": "diffuse",
                 "embedding": [0.0, 1.0]},
            ]

            def _read_index(self, domain):
                self.observed_domain = domain
                return [dict(item) for item in self.chunks], "fixture-digest"

            def _embed(self, texts, *, query):
                return [[1.0, 0.0]], {"prompt_eval_count": None}

            def _expanded_terms(self, query):
                return {"shader"}

        return FixtureRetriever(settings)

    def test_learning_and_service_rank_scores_match_pre_extraction_fixture(self):
        retriever = self._retriever()
        lexical = math.log(1 + 1.5 / 2.5) * 2.2 / (1 + 1.2 * (0.25 + 0.75 * 4 / (11 / 3)))
        for domain in ("learning", "service"):
            result = retriever.search("shader", domain=domain)
            candidates = result["candidates"]
            self.assertEqual(retriever.observed_domain, domain)
            self.assertEqual([item["chunk_id"] for item in candidates], ["A", "B", "C"])
            self.assertAlmostEqual(candidates[0]["bm25"], lexical)
            self.assertAlmostEqual(candidates[1]["bm25"], lexical)
            self.assertEqual(candidates[2]["bm25"], 0)
            self.assertAlmostEqual(candidates[0]["rrf"], 2 / 61)
            self.assertAlmostEqual(candidates[1]["rrf"], 2 / 62)
            self.assertAlmostEqual(candidates[2]["rrf"], 1 / 63)
            self.assertAlmostEqual(candidates[1]["cosine"], 0.8)
            self.assertEqual(result["receipt"]["prompt_eval_count"], None)

    def test_duplicate_chunk_identity_is_rejected(self):
        retriever = self._retriever()
        retriever.chunks[1] = {**retriever.chunks[1], "chunk_id": "A"}
        with self.assertRaisesRegex(ValueError, "duplicate chunk identity"):
            retriever.search("shader", domain="service")

    def test_embedding_response_receipt_reports_only_observed_facts(self):
        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                return {"embeddings": [[1.0, 2.0]], "model": "actual-model",
                        "model_digest": "actual-digest"}

        class Client:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

            def post(self, *_args, **_kwargs):
                return Response()

        with patch("graphics.retrieval.httpx.Client", return_value=Client()):
            vectors, receipt = Retriever(self._retriever().settings)._embed(["shader"], query=True)
        self.assertEqual(vectors, [[1.0, 2.0]])
        self.assertEqual(receipt["model"], "actual-model")
        self.assertEqual(receipt["dimensions"], 2)
        self.assertEqual(receipt["response_model_digest"], "actual-digest")
        self.assertNotIn("prompt_eval_count", receipt)
        self.assertNotIn("usage", receipt)

    def test_faq_full_source_uses_existing_same_origin_reference_map(self):
        app = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('"corpus/support-faq.md": "support-faq"', app)

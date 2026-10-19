"""Conditional RAG property checks report coverage and pending behavior."""
import unittest
import json
import hashlib

from pr_review.rag_checks import assess_snapshot_rag


class RagChecksTests(unittest.TestCase):
    def test_unrelated_change_skips_checks(self):
        self.assertEqual(assess_snapshot_rag({"sources": [], "coverage": {"complete": True, "omissions": []}},
                         {"rag_checks_required": False, "checks": [], "reasons": []}, [], {} )["status"], "skipped")

    def test_missing_corpus_and_changed_algorithm_are_not_pass(self):
        snapshot = {"sources": [], "coverage": {"complete": False,
                    "omissions": [{"path": "week_7/task_1/corpus/manifest.json", "side": "head", "reason": "missing"}]}}
        impact = {"rag_checks_required": True, "checks": ["availability_provenance", "retrieval"],
                  "reasons": [{"path": "week_7/task_1/graphics/retrieval.py", "rule": "retrieval"}]}
        result = assess_snapshot_rag(snapshot, impact, [], {"model": "qwen-test", "dimensions": 2})
        self.assertEqual(result["status"], "incomplete")
        self.assertTrue(any("algorithm" in item for item in result["pending"]))
        self.assertTrue(any(check["status"] == "failed" for check in result["checks"]))

    def test_changed_manifest_reference_missing_from_pinned_snapshot_is_pending(self):
        manifest_path = "week_7/task_1/corpus/manifest.json"
        data_path = "week_7/task_1/data/corpus/material.md"
        snapshot = {"head_revision": "b" * 40, "changed_files": [
            {"status": "M", "path": manifest_path}],
            "sources": [{"source_id": "manifest-head", "revision": "b" * 40,
                         "side": "head", "path": manifest_path,
                         "text": json.dumps({"version": 1, "sources": [
                             {"id": "material", "path": "data/corpus/material.md"}]})}],
            "coverage": {"complete": True, "omissions": []}}
        impact = {"rag_checks_required": True, "checks": ["availability_provenance"],
                  "reasons": [{"path": manifest_path, "rule": "corpus"}]}
        result = assess_snapshot_rag(snapshot, impact, [], {})
        self.assertEqual(result["status"], "incomplete")
        self.assertTrue(any(data_path in item for item in result["pending"]))
        self.assertNotEqual(result["checks"][0]["status"], "passed")

        pinned = {"source_id": "material-head", "revision": "b" * 40, "side": "head",
                  "path": data_path, "text": "Pinned material", "content_sha256": hashlib.sha256(b"Pinned material").hexdigest()}
        snapshot["sources"].append(pinned)
        verified = assess_snapshot_rag(snapshot, impact, [], {})
        self.assertEqual(verified["status"], "passed")
        pinned["content_sha256"] = "0" * 64
        corrupted = assess_snapshot_rag(snapshot, impact, [], {})
        self.assertEqual(corrupted["status"], "incomplete")
        self.assertEqual(corrupted["checks"][0]["status"], "failed")

    def test_wrong_known_source_cannot_pass_control_retrieval(self):
        path = "week_7/task_1/docs/usage.md"
        snapshot = {"sources": [
            {"source_id": "usage-head", "revision": "b" * 40, "path": path,
             "side": "head", "text": "В панели «Учёт» указана стоимость."},
            {"source_id": "other-head", "revision": "b" * 40, "path": "other.md",
             "side": "head", "text": "Другой материал."}],
            "coverage": {"complete": True, "omissions": []}}
        impact = {"rag_checks_required": True, "checks": ["retrieval"], "reasons": []}
        control = {"id": "usage-accounting-v1", "source_id": "usage-head",
                   "query": "Где пользователь видит токены и стоимость выполненных этапов?",
                   "revision": "b" * 40, "path": path, "marker": "В панели «Учёт»"}
        wrong = [{"source_id": "other-head", "revision": "b" * 40,
                  "path": "other.md", "text": "Другой материал."}]
        result = assess_snapshot_rag(snapshot, impact, wrong, {}, control=control)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(next(item for item in result["checks"] if item["name"] == "retrieval")["status"], "failed")

    def test_retrieved_control_does_not_prove_generated_grounded_answer(self):
        path = "week_7/task_1/docs/usage.md"
        source = {"source_id": "usage-head", "revision": "b" * 40, "path": path,
                  "side": "head", "text": "В панели «Учёт» видны токены и стоимость."}
        control = {"id": "usage-accounting-v1", "source_id": "usage-head",
                   "query": "Где пользователь видит токены и стоимость выполненных этапов?",
                   "revision": "b" * 40, "path": path, "marker": "В панели «Учёт»"}
        impact = {"rag_checks_required": True, "checks": ["grounded_source_use"], "reasons": []}
        result = assess_snapshot_rag({"sources": [source],
            "coverage": {"complete": True, "omissions": []}}, impact, [source], {}, control=control)
        self.assertEqual(next(c for c in result["checks"] if c["name"] == "retrieval")["status"], "passed")
        self.assertEqual(result["status"], "incomplete")
        self.assertIn("grounded_source_use_unverified", result["pending"])
        self.assertEqual(next(c for c in result["checks"] if c["name"] == "grounded_source_use")["status"], "pending")

    def test_distinct_revision_keys_do_not_prove_version_semantics(self):
        snapshot = {"sources": [{"source_id": "a", "revision": "a" * 40, "path": "x.md"},
                                {"source_id": "b", "revision": "b" * 40, "path": "x.md"}],
                    "coverage": {"complete": True, "omissions": []}}
        impact = {"rag_checks_required": True, "checks": ["version_separation"], "reasons": []}
        result = assess_snapshot_rag(snapshot, impact, [], {})
        self.assertEqual(result["status"], "incomplete")
        self.assertIn("version_semantics_unverified", result["pending"])

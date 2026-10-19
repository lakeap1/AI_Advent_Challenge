"""Pinned, isolated review retrieval contract."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

from pr_review.review_context import SnapshotIndex


BASE = "a" * 40
HEAD = "b" * 40


def snapshot():
    data = {"schema_version": 1, "base_revision": BASE, "head_revision": HEAD, "merge_base": BASE,
            "changed_files": [{"status": "M", "path": "docs/shape.md", "old_path": None, "patch": "@@ -1 +1 @@\n-old\n+new\n"}],
            "sources": [
                {"source_id": "base-shape", "revision": BASE, "path": "docs/shape.md", "side": "base",
                 "start_line": 1, "end_line": 2, "text": "# Форма\nСтарый шейдер\n",
                 "content_sha256": ""},
                {"source_id": "head-shape", "revision": HEAD, "path": "docs/shape.md", "side": "head",
                 "start_line": 1, "end_line": 2, "text": "# Форма\nНовый шейдер\n",
                 "content_sha256": ""}],
            "coverage": {"complete": True, "omissions": []}, "limits": {}}
    for source in data["sources"]:
        source["content_sha256"] = hashlib.sha256(source["text"].encode("utf-8")).hexdigest()
    return data


def long_snapshot():
    data = snapshot()
    for source, first in zip(data["sources"], (0, 9)):
        lines = [f"fragment {number:02d} " + "x" * 3000 + "\n"
                 for number in range(first, first + 9)]
        source["text"] = "".join(lines)
        source["end_line"] = len(lines)
        source["content_sha256"] = hashlib.sha256(source["text"].encode("utf-8")).hexdigest()
    return data


class StrictCpuEmbed:
    """Simulate a CPU request budget that accepts one source fragment at a time."""

    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on

    def __call__(self, texts, *, query):
        self.calls.append((tuple(texts), query))
        if query or len(texts) != 1:
            raise AssertionError("CPU embedding request exceeded its budget")
        if len(self.calls) == self.fail_on:
            raise TimeoutError("CPU embedding failed")
        return [[1.0, 0.0]], {"model": "qwen-test", "model_digest": "sha256:digest",
                              "dimensions": 2, "prompt_eval_count": 3}


class Embed:
    def __init__(self):
        self.model = "qwen-test"
        self.digest = "sha256:digest"
        self.bad = False
        self.calls = []

    def __call__(self, texts, *, query):
        self.calls.append((tuple(texts), query))
        vectors = [[1.0, 0.0] if "шейдер" in text.casefold() else [0.0, 1.0] for text in texts]
        if self.bad:
            vectors[0][0] = float("nan")
        return vectors, {"model": self.model, "model_digest": self.digest,
                         "dimensions": 2, "prompt_eval_count": len(texts)}


class SnapshotIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.production = self.root / "production"
        self.production.mkdir()
        self.production_index = self.production / "retrieval.sqlite3"
        self.production_index.write_bytes(b"untouched")
        self.settings = SimpleNamespace(data_dir=self.production, embedding_model="qwen-test", embedding_dimensions=2)
        self.embed = Embed()
        self.index = SnapshotIndex(self.settings, self.embed, self.root / "review")

    def test_cpu_embedding_covers_all_chunks_in_order_with_provenance_and_usage(self):
        data = long_snapshot()
        expected = [line for source in data["sources"] for line in source["text"].splitlines(keepends=True)]
        cpu = StrictCpuEmbed()
        self.index.embed = cpu

        metadata = self.index.build(data)
        stored = json.loads(self.index.index_path.read_text(encoding="utf-8"))

        self.assertEqual(metadata["chunks"], 18)
        self.assertEqual(metadata["prompt_eval_count"], 54)
        self.assertEqual([call[0][0] for call in cpu.calls], expected)
        self.assertTrue(all(not query for _, query in cpu.calls))
        self.assertEqual([chunk["text"] for chunk in stored["chunks"]], expected)
        self.assertEqual([(chunk["source_id"], chunk["revision"], chunk["origin"],
                           chunk["start_line"], chunk["end_line"])
                          for chunk in stored["chunks"]],
                         [("base-shape", BASE, "repo_base", line, line) for line in range(1, 10)] +
                         [("head-shape", HEAD, "pr_head", line, line) for line in range(1, 10)])
        self.assertTrue(all(chunk["sha256"] == hashlib.sha256(chunk["text"].encode("utf-8")).hexdigest()
                            for chunk in stored["chunks"]))

    def test_cpu_embedding_failure_stops_without_retry_or_successful_index(self):
        cpu = StrictCpuEmbed(fail_on=7)
        self.index.embed = cpu

        with self.assertRaisesRegex(TimeoutError, "CPU embedding failed"):
            self.index.build(long_snapshot())

        self.assertEqual(len(cpu.calls), 7)
        self.assertFalse(self.index.index_path.exists())
        self.assertIsNone(self.index._index)

    def test_same_path_has_distinct_revision_identity_and_exact_lf_quote(self):
        data = snapshot()
        original = copy.deepcopy(data)
        metadata = self.index.build(data)
        result = self.index.search("шейдер", limit=8)
        candidates = result["candidates"]
        self.assertEqual(data, original)
        self.assertEqual(metadata["documents"], 2)
        self.assertEqual({c["source_id"] for c in candidates}, {"base-shape", "head-shape"})
        self.assertEqual({c["origin"] for c in candidates}, {"repo_base", "pr_head"})
        self.assertTrue(all(c["text"] in next(s["text"] for s in data["sources"] if s["source_id"] == c["source_id"]) for c in candidates))
        self.assertTrue(all(c["start_line"] == 1 and c["end_line"] == 2 for c in candidates))
        self.assertEqual(self.production_index.read_bytes(), b"untouched")
        self.assertEqual(result["receipt"]["model_digest"], "sha256:digest")
        self.assertEqual(self.embed.calls[-1][1], True)

    def test_model_dimension_and_nonfinite_mismatch_fail_closed(self):
        data = snapshot()
        self.embed.model = "wrong"
        with self.assertRaises(ValueError):
            self.index.build(data)
        self.embed.model = "qwen-test"
        self.embed.bad = True
        with self.assertRaises(ValueError):
            self.index.build(data)
        self.embed.bad = False
        self.index.build(data)
        self.embed.digest = "sha256:other"
        with self.assertRaises(ValueError):
            self.index.search("шейдер")

    def test_production_directory_is_refused(self):
        with self.assertRaises(ValueError):
            SnapshotIndex(self.settings, self.embed, self.production)

        with self.assertRaises(ValueError):
            SnapshotIndex(self.settings, self.embed, self.production / "review")
        with self.assertRaises(ValueError):
            SnapshotIndex(self.settings, self.embed, self.root)

    def test_unicode_separator_and_cr_preserve_git_lf_lines(self):
        data = snapshot()
        data["sources"] = data["sources"][:1]
        source = data["sources"][0]
        source["text"] = "А\u2028Б\r\nШейдер\n"
        source["end_line"] = 2
        source["content_sha256"] = hashlib.sha256(source["text"].encode("utf-8")).hexdigest()
        self.index.build(data)
        candidate = self.index.search("Шейдер")["candidates"][0]
        self.assertEqual(candidate["text"], source["text"])
        self.assertEqual((candidate["start_line"], candidate["end_line"]), (1, 2))

    def test_receipt_dimensions_and_oversized_utf8_line_fail(self):
        data = snapshot()
        source = data["sources"][0]
        source["text"] = "Я" * 5000 + "\n"
        source["end_line"] = 1
        source["content_sha256"] = hashlib.sha256(source["text"].encode("utf-8")).hexdigest()
        with self.assertRaisesRegex(ValueError, "chunk budget"):
            self.index.build(data)
        data = snapshot()
        self.embed.model = "qwen-test"
        self.settings.embedding_dimensions = 3
        with self.assertRaisesRegex(ValueError, "dimensions"):
            self.index.build(data)

    def test_conditional_checks_keep_changed_pr_algorithm_pending(self):
        data = snapshot()
        self.index.build(data)
        before = len(self.embed.calls)
        self.assertEqual(self.index.checks({"rag_checks_required": False, "checks": []})["status"], "skipped")
        self.assertEqual(len(self.embed.calls), before)
        impact = {"rag_checks_required": True, "checks": ["retrieval", "vector_compatibility"],
                  "reasons": [{"path": "week_7/task_1/graphics/retrieval.py", "rule": "retrieval"}]}
        result = self.index.checks(impact)
        self.assertEqual(result["status"], "incomplete")
        self.assertTrue(any("algorithm" in pending for pending in result["pending"]))
        self.assertIn("retrieval_control_unavailable", result["pending"])
        self.assertEqual(len(self.embed.calls), before)

    def test_changed_stored_provenance_fails_before_query_embedding(self):
        self.index.build(snapshot())
        stored = json.loads(self.index.index_path.read_text(encoding="utf-8"))
        stored["chunks"][0]["path"] = "forged.py"
        self.index.index_path.write_text(json.dumps(stored), encoding="utf-8")
        fresh = SnapshotIndex(self.settings, self.embed, self.root / "review")
        before = len(self.embed.calls)
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            fresh.search("шейдер")
        self.assertEqual(len(self.embed.calls), before)

    def test_empty_git_blob_retains_provenance_without_aborting_other_sources(self):
        data = snapshot()
        source = data["sources"][0]
        source.update(text="", start_line=0, end_line=0,
                      content_sha256=hashlib.sha256(b"").hexdigest())
        result = self.index.build(data)
        self.assertEqual(result["documents"], 2)
        self.assertEqual(result["chunks"], 1)
        self.assertEqual(self.index.search("шейдер")["candidates"][0]["source_id"], "head-shape")

        for source in data["sources"]:
            source.update(text="", start_line=0, end_line=0,
                          content_sha256=hashlib.sha256(b"").hexdigest())
        before = len(self.embed.calls)
        empty = self.index.build(data)
        self.assertEqual(empty["status"], "incomplete")
        self.assertEqual(empty["chunks"], 0)
        self.assertEqual(self.index.search("шейдер")["candidates"], [])
        self.assertEqual(len(self.embed.calls), before)

    def test_missing_trusted_control_does_not_call_query_embedding(self):
        self.index.build(snapshot())
        before = len(self.embed.calls)
        impact = {"rag_checks_required": True, "checks": ["retrieval"], "reasons": []}
        result = self.index.checks(impact)
        self.assertEqual(result["status"], "incomplete")
        self.assertIn("retrieval_control_unavailable", result["pending"])
        self.assertEqual(len(self.embed.calls), before)

    def test_fixed_usage_control_requires_expected_head_chunk(self):
        data = snapshot()
        source = data["sources"][1]
        source["path"] = "week_7/task_1/docs/usage.md"
        source["text"] = "В панели «Учёт» видны токены и стоимость.\n"
        source["start_line"] = source["end_line"] = 1
        source["content_sha256"] = hashlib.sha256(source["text"].encode("utf-8")).hexdigest()
        self.index.build(data)
        impact = {"rag_checks_required": True, "checks": ["retrieval", "grounded_source_use"], "reasons": []}
        before = len(self.embed.calls)
        result = self.index.checks(impact)
        self.assertEqual(result["control"]["id"], "usage-accounting-v1")
        self.assertEqual(result["control"]["source_id"], "head-shape")
        self.assertEqual(next(item for item in result["checks"] if item["name"] == "retrieval")["status"], "passed")
        self.assertEqual(len(self.embed.calls), before + 1)
        self.assertEqual(result["status"], "incomplete")
        self.assertNotIn("candidates", result)
        private = self.index.grounding_context()
        self.assertEqual(private["control"]["source_id"], "head-shape")
        self.assertIn("В панели «Учёт»", private["candidates"][0]["text"])
        private["candidates"].clear()
        self.assertTrue(self.index.grounding_context()["candidates"])

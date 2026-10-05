import hashlib
import dataclasses
import json
import math
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from indexing.chunking import chunk_document
from indexing.client import OpenAIEmbedder
from indexing.config import load_config
from indexing.corpus import read_corpus
from indexing.service import IndexingService
from indexing.store import IndexStore


class IndexPipelineTests(unittest.TestCase):
    def test_prebuild_inspect_lists_unique_source_hashes_without_store_or_api(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "corpus").mkdir()
            first = "# Один\r\nТекст\r\n".encode("utf-8")
            second = "def draw():\n    return 1\n".encode("utf-8")
            (root / "first.md").write_bytes(first)
            (root / "copy.md").write_bytes(first)
            (root / "draw.py").write_bytes(second)
            (root / "corpus" / "manifest.json").write_text(json.dumps({"version": 1, "files": ["first.md", "copy.md", "draw.py"]}), encoding="utf-8")
            actual = IndexingService(root, None, None).inspect()
            self.assertEqual(actual["documents"], 2)
            self.assertIn("files", actual)
            self.assertEqual(actual["files"], [
                {"file": "first.md", "source": "first.md", "title": "first.md",
                 "document_hash": hashlib.sha256(first).hexdigest(), "characters": len(first.decode("utf-8"))},
                {"file": "draw.py", "source": "draw.py", "title": "draw.py",
                 "document_hash": hashlib.sha256(second).hexdigest(), "characters": len(second.decode("utf-8"))},
            ])

    def test_unicode_windows_newlines_and_structural_boundaries(self):
        text = "# Тема\r\nабв\r\n## Раздел\r\nстрока\r\n"
        document = SimpleNamespace(file="note.md", source="note.md", title="Note",
                                   text=text, document_hash=hashlib.sha256(text.encode()).hexdigest())
        fixed = chunk_document(document, "fixed", size=9, overlap=2)
        self.assertEqual([c["start"] for c in fixed], [0, 7, 14, 21, 28])
        self.assertTrue(all(c["text"] == text[c["start"]:c["end"]] for c in fixed))
        structural = chunk_document(document, "structural", size=100, overlap=2)
        self.assertEqual(len(structural), 2)
        self.assertEqual("".join(c["text"] for c in structural), text)
        self.assertEqual(structural[1]["start"], text.index("## Раздел"))

    def test_manifest_rejects_traversal_before_document_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "corpus").mkdir()
            (root / "corpus" / "manifest.json").write_text(json.dumps({"version": 1, "files": ["../secret.md"]}), encoding="utf-8")
            with self.assertRaises(ValueError):
                read_corpus(root)

    def test_manifest_link_is_rejected_before_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "corpus").mkdir()
            manifest = root / "corpus" / "manifest.json"
            manifest.write_text(json.dumps({"version": 1, "files": []}), encoding="utf-8")
            original_link = Path.is_symlink
            original_read = Path.read_text
            def is_link(path):
                return path == manifest or original_link(path)
            def read_text(path, *args, **kwargs):
                if path == manifest:
                    raise AssertionError("linked manifest was read")
                return original_read(path, *args, **kwargs)
            with patch.object(Path, "is_symlink", is_link), patch.object(Path, "read_text", read_text):
                with self.assertRaises(ValueError):
                    read_corpus(root)

    def test_structural_python_distinguishes_methods(self):
        text = "class Render:\n    def light(self):\n        return 1\n    def shade(self):\n        return 2\n"
        document = SimpleNamespace(file="render.py", source="render.py", title="Render",
                                   text=text, document_hash=hashlib.sha256(text.encode()).hexdigest())
        chunks = chunk_document(document, "structural", size=1000, overlap=0)
        self.assertEqual("".join(c["text"] for c in chunks), text)
        self.assertTrue(any("Render.light" in c["section"] for c in chunks))
        self.assertTrue(any("Render.shade" in c["section"] for c in chunks))

    def test_embedder_reorders_by_index_and_retains_usage_on_rejection(self):
        cfg = load_config()
        vector = [1.0] + [0.0] * (cfg.dimensions - 1)
        def response(request):
            self.assertEqual(str(request.url), "https://api.openai.com/v1/embeddings")
            return httpx.Response(200, json={"model": cfg.model, "data": [
                {"index": 1, "embedding": [0.0, 1.0] + [0.0] * (cfg.dimensions - 2)},
                {"index": 0, "embedding": vector}], "usage": {"prompt_tokens": 7, "total_tokens": 7}})
        client = OpenAIEmbedder(cfg, api_key="secret", client=httpx.Client(transport=httpx.MockTransport(response)))
        result = client.embed(["first", "second"])
        self.assertEqual(result["vectors"][0], vector)
        self.assertEqual(result["usage"]["prompt_tokens"], 7)

        def invalid(request):
            return httpx.Response(200, json={"model": cfg.model, "data": [{"index": 0, "embedding": [0.0] * cfg.dimensions}], "usage": {"prompt_tokens": 9, "total_tokens": 9}})
        client = OpenAIEmbedder(cfg, api_key="secret", client=httpx.Client(transport=httpx.MockTransport(invalid)))
        with self.assertRaises(ValueError) as context:
            client.embed(["first"])
        self.assertEqual(context.exception.metadata["usage"]["prompt_tokens"], 9)

    def test_nonfinite_timeout_is_rejected_by_config(self):
        with self.assertRaises(ValueError):
            dataclasses.replace(load_config(), timeout_seconds=float("nan")).validate()

    def test_embedding_usage_with_unattributed_output_tokens_has_unknown_cost(self):
        cfg = dataclasses.replace(load_config(), min_corpus_characters=1)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "corpus").mkdir()
            (root / "note.md").write_text("# Title\nBody\n", encoding="utf-8")
            (root / "corpus" / "manifest.json").write_text(json.dumps({"version": 1, "files": ["note.md"]}), encoding="utf-8")
            def response(request):
                body = json.loads(request.content)
                return httpx.Response(200, json={"model": cfg.model,
                                                 "data": [{"index": i, "embedding": [1.0] + [0.0] * (cfg.dimensions - 1)} for i in range(len(body["input"]))],
                                                 "usage": {"prompt_tokens": 7, "total_tokens": 9}})
            store = IndexStore(":memory:")
            try:
                service = IndexingService(root, store, OpenAIEmbedder(cfg, api_key="secret", client=httpx.Client(transport=httpx.MockTransport(response))), cfg)
                service.build()
                rows = store.ledger()
                self.assertEqual(len(rows), 2)
                self.assertTrue(all(row["input_tokens"] is None and row["total_tokens"] is None and row["cost_usd"] is None for row in rows))
                self.assertFalse(service.export()["cumulative"]["complete"])
            finally:
                store.close()

    def test_failed_rebuild_preserves_active_pair_and_bills_rejected_batch(self):
        cfg = load_config()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "corpus").mkdir()
            (root / "note.md").write_text("# Title\n" + "x" * 54000, encoding="utf-8")
            (root / "corpus" / "manifest.json").write_text(json.dumps({"version": 1, "files": ["note.md"]}), encoding="utf-8")
            calls = 0
            fail = False
            def response(request):
                nonlocal calls
                calls += 1
                data = json.loads(request.content)
                vectors = [{"index": i, "embedding": [1.0] + [0.0] * (cfg.dimensions - 1)} for i in range(len(data["input"]))]
                if fail:
                    vectors[0]["embedding"] = [0.0] * cfg.dimensions
                return httpx.Response(200, json={"model": cfg.model, "data": vectors,
                                                 "usage": {"prompt_tokens": 11, "total_tokens": 11}})
            store = IndexStore(":memory:")
            service = IndexingService(root, store, OpenAIEmbedder(cfg, api_key="secret", client=httpx.Client(transport=httpx.MockTransport(response))), cfg)
            service.build()
            before = service.export()
            old_calls = calls
            service.build()
            self.assertEqual(calls, old_calls)
            fail = True
            with self.assertRaises(ValueError):
                service.build(force=True)
            after = service.export()
            self.assertEqual(after["indexes"], before["indexes"])
            self.assertEqual(after["documents"], before["documents"])
            self.assertFalse(store.ledger()[-1]["output_policy"]["passed"])
            self.assertEqual(store.ledger()[-1]["input_tokens"], 11)
            competing = store.start_run(before["corpus"], cfg.contract)
            with self.assertRaisesRegex(ValueError, "already running"):
                service.build()
            store.fail_run(competing, "test worker stopped")
            store.close()

    def test_disk_reader_does_not_interrupt_live_build_but_recovers_stale_one(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.sqlite3"
            first = IndexStore(path)
            try:
                run_id = first.claim_run({"corpus_hash": "a" * 64}, load_config().contract)
                with self.assertRaisesRegex(ValueError, "already running"):
                    first.claim_run({"corpus_hash": "a" * 64}, load_config().contract)
                reader = IndexStore(path)
                try:
                    self.assertEqual(reader.summary()["status"], "running")
                    with self.assertRaisesRegex(ValueError, "already running"):
                        reader.claim_run({"corpus_hash": "a" * 64}, load_config().contract)
                finally:
                    reader.close()
            finally:
                first.close()  # Simulate the lock release of a stopped process.
            recovered = IndexStore(path)
            try:
                self.assertEqual(recovered.summary()["status"], "interrupted")
                self.assertEqual(recovered.export()["runs"][-1]["run_id"], run_id)
            finally:
                recovered.close()

    def test_begin_failure_releases_disk_lease_for_next_build(self):
        with tempfile.TemporaryDirectory() as directory:
            store = IndexStore(Path(directory) / "index.sqlite3")
            connection = store._db
            class FailFirstBegin:
                pending = True
                def execute(self, sql, *args, **kwargs):
                    if sql == "BEGIN IMMEDIATE" and self.pending:
                        self.pending = False
                        raise sqlite3.OperationalError("injected busy transaction")
                    return connection.execute(sql, *args, **kwargs)
                def __getattr__(self, name):
                    return getattr(connection, name)
            store._db = FailFirstBegin()
            try:
                with self.assertRaisesRegex(sqlite3.OperationalError, "injected busy"):
                    store.claim_run({"corpus_hash": "a" * 64}, load_config().contract)
                run_id = store.claim_run({"corpus_hash": "a" * 64}, load_config().contract)
                self.assertTrue(run_id)
            finally:
                store._db = connection
                store.close()


if __name__ == "__main__":
    unittest.main()

"""Behavioral checks for structural, domain-scoped local retrieval."""
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from graphics.chunking import structural_chunks
from graphics.config import Settings
from graphics.retrieval import IndexStaleError, Retriever


def source(identifier, path, domain="learning", language="en"):
    return {"id": identifier, "title": identifier.title(), "url": f"https://example.test/{identifier}",
            "domain": domain, "version": "v1", "pipeline": "all", "language": language,
            "license": "test", "path": path}


class FakeRetriever(Retriever):
    digest = "sha256:test-model"
    broken = False

    def _model_digest(self):
        return self.digest

    def _embed(self, texts, *, query):
        if self.broken:
            return [[float("nan")] * 1024 for _ in texts], {"prompt_eval_count": 1}
        vectors = []
        for value in texts:
            vector = [0.0] * 1024
            vector[0 if "shader" in value.casefold() or "шейдер" in value.casefold() else 1] = 1.0
            vectors.append(vector)
        return vectors, {"prompt_eval_count": len(texts)}


class ChunkingTests(unittest.TestCase):
    def test_sections_and_offsets_are_exact_even_when_heading_is_fenced(self):
        text = "# Surface\nA material uses a shader.\n```md\n# Not heading\n```\n## Detail\nNormal mapping changes light.\n"
        chunks = structural_chunks(text, source("surface", "corpus/surface.md"), chunk_size=48, overlap=8)
        self.assertGreaterEqual(len(chunks), 3)
        self.assertTrue(any("Surface / Detail" in chunk["section"] for chunk in chunks))
        self.assertFalse(any("Not heading" in chunk["section"] for chunk in chunks))
        self.assertTrue(all(text[c["start"]:c["end"]] == c["text"] for c in chunks))
        self.assertTrue(all(len(c["text"]) <= 48 for c in chunks))

    def test_invalid_overlap_is_rejected(self):
        with self.assertRaises(ValueError):
            structural_chunks("abc", source("a", "a.md"), chunk_size=3, overlap=3)


class RetrievalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "app"
        self.root.mkdir()
        self.data = Path(self.temp.name) / "separate-index-and-receipts"
        (self.root / "corpus").mkdir()
        (self.root / "docs").mkdir()
        (self.root / "corpus" / "shader.md").write_text("# Shader\nA shader program shades pixels.", encoding="utf-8")
        (self.root / "corpus" / "light.md").write_text("# Lighting\nA light illuminates the scene.", encoding="utf-8")
        (self.root / "docs" / "usage.md").write_text("# Service\nUse the graphics learning chat.", encoding="utf-8")
        (self.root / "corpus" / "glossary.md").write_text("# Terms\nПоисковые соответствия: шейдер shader; свет lighting light.", encoding="utf-8")
        self.manifest = self.root / "corpus" / "manifest.json"
        self.sources = [source("shader", "corpus/shader.md"), source("light", "corpus/light.md"),
                        source("usage", "docs/usage.md", "service"), source("glossary", "corpus/glossary.md", language="ru")]
        self.manifest.write_text(json.dumps({"version": 1, "sources": self.sources}), encoding="utf-8")
        self.retriever = FakeRetriever(Settings(app_root=self.root, data_dir=self.data))

    def test_http_embed_receipt_reports_response_identity_and_vector_dimensions(self):
        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                return {"model": "unexpected-model", "embeddings": [[1.0, 0.0, 0.0]],
                        "prompt_eval_count": 9, "total_duration": 20}

        class Client:
            def __init__(self, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def post(self, url, json):
                return Response()

        real = Retriever(Settings(app_root=self.root, data_dir=self.data))
        with patch("graphics.retrieval.httpx.Client", Client):
            vectors, receipt = real._embed(["shader"], query=False)
        self.assertEqual(vectors, [[1.0, 0.0, 0.0]])
        self.assertEqual(receipt["model"], "unexpected-model")
        self.assertEqual(receipt["dimensions"], 3)
        self.assertEqual(receipt["prompt_eval_count"], 9)

    def test_russian_query_finds_english_shader_with_both_rank_contributions(self):
        built = self.retriever.build_index()
        self.assertGreaterEqual(built["chunks"], 4)
        result = self.retriever.search("Что такое шейдер?", limit=4)
        shader = next(c for c in result["candidates"] if c["title"] == "Shader")
        self.assertGreater(shader["cosine"], 0)
        self.assertGreater(shader["bm25"], 0)
        self.assertGreater(shader["rrf"], 1 / 61)
        self.assertEqual(result["receipt"]["prompt_eval_count"], 1)
        self.assertEqual(result["receipt"]["dimensions"], 1024)

    def test_service_documents_never_enter_learning_results(self):
        self.retriever.build_index()
        learning = self.retriever.search("service graphics learning", domain="learning")
        service = self.retriever.search("service graphics learning", domain="service")
        self.assertTrue(all(c["domain"] == "learning" for c in learning["candidates"]))
        self.assertEqual({c["title"] for c in service["candidates"]}, {"Usage"})

    def test_glossary_expansion_uses_original_query_terms_without_topic_chaining(self):
        (self.root / "corpus" / "glossary.md").write_text(
            "Поисковые соответствия: шейдер shader vertex; геометрия mesh vertex edge.\n", encoding="utf-8")
        expanded = self.retriever._expanded_terms("шейдер")
        self.assertIn("shader", expanded)
        self.assertIn("vertex", expanded)
        self.assertNotIn("mesh", expanded)
        self.assertNotIn("edge", expanded)

    def test_changed_document_manifest_or_model_rejects_index_before_query_embedding(self):
        self.retriever.build_index()
        (self.root / "corpus" / "shader.md").write_text("# Changed\nText", encoding="utf-8")
        with self.assertRaises(IndexStaleError):
            self.retriever.search("shader")
        self.retriever.build_index()
        self.retriever.digest = "sha256:replacement"
        with self.assertRaises(IndexStaleError):
            self.retriever.search("shader")
        self.retriever.digest = "sha256:test-model"
        self.sources[0]["version"] = "v2"
        self.manifest.write_text(json.dumps({"version": 1, "sources": self.sources}), encoding="utf-8")
        with self.assertRaises(IndexStaleError):
            self.retriever.search("shader")

    def test_nonfinite_embedding_and_corrupt_stored_vector_are_rejected(self):
        self.retriever.broken = True
        with self.assertRaises(ValueError):
            self.retriever.build_index()
        self.retriever.broken = False
        self.retriever.build_index()
        with closing(sqlite3.connect(self.retriever.index_path)) as db:
            db.execute("UPDATE chunks SET embedding_json=? WHERE rowid=(SELECT min(rowid) FROM chunks)", (json.dumps([0.0] * 1024),))
            db.commit()
        with self.assertRaises(ValueError):
            self.retriever.search("shader")

    def test_tampered_index_domain_cannot_expose_service_document_in_learning(self):
        self.retriever.build_index()
        with closing(sqlite3.connect(self.retriever.index_path)) as db:
            db.execute("UPDATE chunks SET domain='learning' WHERE domain='service'")
            db.commit()
        with self.assertRaises(IndexStaleError):
            self.retriever.search("service", domain="learning")


if __name__ == "__main__":
    unittest.main()

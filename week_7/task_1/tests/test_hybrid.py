"""The shared ranker must keep Day 31's vector/BM25/RRF ordering."""
import unittest

from graphics.hybrid import hybrid_rank


class HybridTests(unittest.TestCase):
    def test_lexical_and_vector_evidence_fuse_without_leaking_embeddings(self):
        chunks = [
            {"chunk_id": "a", "title": "Shader", "section": "Document", "text": "shader pixels", "embedding": [1.0, 0.0], "source_id": "base"},
            {"chunk_id": "b", "title": "Light", "section": "Document", "text": "light rays", "embedding": [0.0, 1.0], "source_id": "head"},
        ]
        result = hybrid_rank(chunks, [0.0, 1.0], {"shader"})
        self.assertEqual([row["chunk_id"] for row in result], ["a", "b"])
        self.assertGreater(result[0]["bm25"], 0)
        self.assertEqual(result[1]["cosine"], 1)
        self.assertGreater(result[0]["rrf"], result[1]["rrf"])
        self.assertNotIn("embedding", result[0])
        self.assertEqual(result[0]["source_id"], "base")

    def test_nonfinite_vector_is_rejected(self):
        with self.assertRaises(ValueError):
            hybrid_rank([{"chunk_id": "a", "title": "", "section": "", "text": "x", "embedding": [float("nan")]}], [1.0], {"x"})

"""Local Qwen embeddings, authenticated structural index and BM25/RRF search.

Cosine, Unicode-token BM25 and reciprocal-rank fusion adapt the independent
primitives in week_5/task_5/rag/retrieval.py for this service's own corpus.
"""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time

import httpx

from .chunking import structural_chunks
from .hybrid import bm25_scores, hybrid_rank, tokens as _tokens, vector_norm as _vector


class IndexStaleError(ValueError):
    """The index does not match the current documents or embedding model."""


_BATCH_SIZE = 16
_QUERY_INSTRUCTION = "Instruct: Retrieve passages that answer the user's question about computer graphics and this learning service.\nQuery: "


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Retriever:
    def __init__(self, settings):
        self.settings = settings
        self.index_path = Path(settings.data_dir) / "retrieval.sqlite3"
        self.manifest_path = Path(settings.app_root) / "corpus" / "manifest.json"

    def _source_path(self, source):
        relative = Path(source["path"])
        if relative.is_absolute() or ".." in relative.parts or any(part.startswith(".") for part in relative.parts):
            raise ValueError("unsafe source path")
        root = Path(self.settings.app_root).resolve()
        path = (root / relative).resolve()
        if not path.is_relative_to(root) or path.is_symlink():
            raise ValueError("source escapes app root")
        return path

    def _manifest(self):
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        if manifest.get("version") != 1 or not isinstance(manifest.get("sources"), list) or not manifest["sources"]:
            raise ValueError("invalid corpus manifest")
        ids = set()
        documents = []
        for source in manifest["sources"]:
            if not isinstance(source, dict) or source.get("domain") not in ("learning", "service"):
                raise ValueError("invalid corpus source")
            if source.get("id") in ids:
                raise ValueError("duplicate source id")
            ids.add(source.get("id"))
            path = self._source_path(source)
            if not path.is_file():
                raise ValueError("corpus document missing")
            documents.append((source, path, _sha256(path)))
        return manifest, documents

    def _model_digest(self):
        """Get the current Ollama model digest; a model name alone is insufficient."""
        with httpx.Client(timeout=8, trust_env=False, follow_redirects=False) as client:
            response = client.get(self.settings.embedding_url.rstrip("/") + "/api/tags")
            response.raise_for_status()
            payload = response.json()
        models = [item for item in payload.get("models", []) if item.get("name") == self.settings.embedding_model]
        if len(models) != 1 or not isinstance(models[0].get("digest"), str) or not models[0]["digest"]:
            raise ValueError("embedding model digest unavailable")
        return models[0]["digest"]

    def _embed(self, texts, *, query):
        prompts = [_QUERY_INSTRUCTION + text if query else text for text in texts]
        payload = {"model": self.settings.embedding_model, "input": prompts, "truncate": False,
                   "options": {"num_gpu": 0, "num_thread": self.settings.embedding_threads}}
        with httpx.Client(timeout=120, trust_env=False, follow_redirects=False) as client:
            response = client.post(self.settings.embedding_url.rstrip("/") + "/api/embed", json=payload)
            response.raise_for_status()
            result = response.json()
        vectors = result.get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise ValueError("embedding response count mismatch")
        receipt = {key: result[key] for key in ("prompt_eval_count", "total_duration", "load_duration")
                   if key in result}
        receipt["model"] = result.get("model")
        # Describe the response actually received. The requested model and configured
        # dimensions are not observations of the embedding transport.
        if vectors and all(isinstance(vector, list) and len(vector) == len(vectors[0])
                           for vector in vectors):
            receipt["dimensions"] = len(vectors[0])
        if "model_digest" in result:
            receipt["response_model_digest"] = result["model_digest"]
        return vectors, receipt

    def _fingerprint(self, manifest, documents):
        value = json.dumps({"manifest": manifest, "documents": [(s["id"], digest) for s, _, digest in documents]},
                           ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def build_index(self, manifest_path=None):
        """Build a complete SQLite index atomically; failed embeddings keep the previous one."""
        if manifest_path is not None:
            self.manifest_path = Path(manifest_path)
        started = time.monotonic()
        manifest, documents = self._manifest()
        digest = self._model_digest()
        chunks = []
        for source, path, _ in documents:
            chunks.extend(structural_chunks(path.read_text(encoding="utf-8"), source))
        if not chunks:
            raise ValueError("empty structural corpus")
        vectors = []
        prompt_counts = []
        for offset in range(0, len(chunks), _BATCH_SIZE):
            batch = chunks[offset:offset + _BATCH_SIZE]
            values, receipt = self._embed([part["text"] for part in batch], query=False)
            if len(values) != len(batch):
                raise ValueError("embedding response count mismatch")
            for value in values:
                _vector(value, self.settings.embedding_dimensions)
            vectors.extend(values)
            prompt_counts.append(receipt.get("prompt_eval_count"))
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix="retrieval-", suffix=".sqlite3", dir=self.index_path.parent)
        os.close(descriptor)
        try:
            with closing(sqlite3.connect(temporary)) as db:
                db.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                db.execute("CREATE TABLE chunks (chunk_id TEXT PRIMARY KEY, domain TEXT NOT NULL, chunk_json TEXT NOT NULL, embedding_json TEXT NOT NULL)")
                metadata = {"model": self.settings.embedding_model, "digest": digest,
                            "dimensions": str(self.settings.embedding_dimensions),
                            "fingerprint": self._fingerprint(manifest, documents),
                            "manifest_hash": _sha256(self.manifest_path)}
                db.executemany("INSERT INTO metadata VALUES (?,?)", metadata.items())
                db.executemany("INSERT INTO chunks VALUES (?,?,?,?)", [
                    (part["chunk_id"], part["domain"], json.dumps(part, ensure_ascii=False), json.dumps(vector))
                    for part, vector in zip(chunks, vectors)])
                db.commit()
            os.replace(temporary, self.index_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return {"chunks": len(chunks), "documents": len(documents), "model": self.settings.embedding_model,
                "model_digest": digest, "dimensions": self.settings.embedding_dimensions,
                "manifest_hash": _sha256(self.manifest_path), "fingerprint": self._fingerprint(manifest, documents),
                "prompt_eval_count": sum(prompt_counts) if all(type(x) is int for x in prompt_counts) else None,
                "elapsed_ms": round((time.monotonic() - started) * 1000, 2)}

    def _read_index(self, domain):
        if not self.index_path.is_file() or self.index_path.is_symlink():
            raise IndexStaleError("retrieval index missing or unsafe")
        try:
            manifest, documents = self._manifest()
            digest = self._model_digest()
            with closing(sqlite3.connect(self.index_path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA query_only=ON")
                if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise IndexStaleError("retrieval index corrupt")
                metadata = dict(db.execute("SELECT key,value FROM metadata"))
                expected = {"model": self.settings.embedding_model, "digest": digest,
                            "dimensions": str(self.settings.embedding_dimensions),
                            "fingerprint": self._fingerprint(manifest, documents),
                            "manifest_hash": _sha256(self.manifest_path)}
                if metadata != expected:
                    raise IndexStaleError("retrieval index stale; rebuild required")
                rows = db.execute("SELECT chunk_json,embedding_json FROM chunks WHERE domain=?", (domain,)).fetchall()
            chunks = []
            expected_chunks = [part for source, path, _ in documents if source["domain"] == domain
                               for part in structural_chunks(path.read_text(encoding="utf-8"), source)]
            expected_by_id = {part["chunk_id"]: part for part in expected_chunks}
            if len(rows) != len(expected_chunks):
                raise IndexStaleError("retrieval index chunks differ from corpus")
            for row in rows:
                chunk = json.loads(row["chunk_json"])
                if chunk != expected_by_id.get(chunk.get("chunk_id")):
                    raise IndexStaleError("retrieval index chunk provenance differs from corpus")
                vector = json.loads(row["embedding_json"])
                _vector(vector, self.settings.embedding_dimensions)
                chunk["embedding"] = vector
                chunks.append(chunk)
            return chunks, digest
        except (sqlite3.Error, OSError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise IndexStaleError("retrieval index unreadable; rebuild required") from error

    def _expanded_terms(self, query):
        terms = set(_tokens(query))
        expanded = set(terms)
        glossary = Path(self.settings.app_root) / "corpus" / "glossary.md"
        if not glossary.is_file():
            return expanded
        for line in glossary.read_text(encoding="utf-8").splitlines():
            if "Поисковые соответствия:" not in line:
                continue
            for pair in line.split("Поисковые соответствия:", 1)[1].split(";"):
                pair_terms = set(_tokens(pair))
                if terms.intersection(pair_terms):
                    expanded.update(pair_terms)
        return expanded

    @staticmethod
    def _bm25(chunks, terms):
        return bm25_scores(chunks, terms)

    def search(self, query, domain="learning", limit=8, on_request=None):
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query required")
        if domain not in ("learning", "service"):
            raise ValueError("invalid retrieval domain")
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("invalid result limit")
        started = time.monotonic()
        chunks, digest = self._read_index(domain)
        if not chunks:
            return {"candidates": [], "receipt": {"model": self.settings.embedding_model, "model_digest": digest,
                    "dimensions": self.settings.embedding_dimensions, "prompt_eval_count": None,
                    "elapsed_ms": round((time.monotonic() - started) * 1000, 2)}}
        if on_request is not None:
            on_request()
        values, embedding_receipt = self._embed([query], query=True)
        if len(values) != 1:
            raise ValueError("query embedding response count mismatch")
        vector = values[0]
        candidates = hybrid_rank(chunks, vector, self._expanded_terms(query))
        receipt = {"model": self.settings.embedding_model, "model_digest": digest,
                   "dimensions": self.settings.embedding_dimensions, "prompt_eval_count": embedding_receipt.get("prompt_eval_count"),
                   "elapsed_ms": round((time.monotonic() - started) * 1000, 2), "domain": domain,
                   "options": {"num_gpu": 0, "num_thread": self.settings.embedding_threads,
                               "truncate": False, "query_instruction": "Qwen retrieval instruction v1"}}
        for key in ("total_duration", "load_duration"):
            if key in embedding_receipt:
                receipt[key] = embedding_receipt[key]
        return {"candidates": candidates[:limit], "receipt": receipt}

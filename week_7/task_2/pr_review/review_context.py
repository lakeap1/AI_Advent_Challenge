"""Isolated, bounded retrieval over an already prepared pinned Git snapshot."""
import hashlib
import json
import os
from copy import deepcopy
from pathlib import Path
import tempfile

from graphics.hybrid import hybrid_rank, tokens, vector_norm

from .rag_checks import assess_snapshot_rag, select_retrieval_control


_MAX_TOTAL_BYTES = 2 * 1024 * 1024
_MAX_CHUNKS = 1200
_MAX_CHUNK_BYTES = 8192
_TARGET_CHUNK_BYTES = 4096
_BATCH = 16


def _digest(value):
    return hashlib.sha256(value).hexdigest()


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _lf_lines(text):
    parts = text.split("\n")
    return [part + "\n" for part in parts[:-1]] + ([parts[-1]] if parts[-1] else [])


def _chunks(source):
    text = source["text"]
    lines = _lf_lines(text)
    if source["start_line"] != (1 if lines else 0) or source["end_line"] != len(lines):
        raise ValueError("source LF line provenance mismatch")
    if source.get("content_sha256") != _digest(text.encode("utf-8")):
        raise ValueError("source content digest mismatch")
    result = []
    group = []
    size = 0
    start = 1

    def emit():
        nonlocal group, size, start
        if not group:
            return
        value = "".join(group)
        if value.strip():
            end = start + len(group) - 1
            identity = [source["source_id"], source["revision"], source["path"], start, end, _digest(value.encode("utf-8"))]
            result.append({"chunk_id": _digest(_canonical(identity)), "source_id": source["source_id"],
                           "revision": source["revision"], "path": source["path"],
                           "start_line": start, "end_line": end, "text": value,
                           "sha256": _digest(value.encode("utf-8")), "origin": "repo_base" if source["side"] == "base" else "pr_head",
                           "title": source["path"], "section": "Document"})
        start += len(group)
        group = []
        size = 0

    for line in lines:
        line_size = len(line.encode("utf-8"))
        if line_size > _MAX_CHUNK_BYTES:
            raise ValueError("source line exceeds chunk budget")
        if group and size + line_size > _TARGET_CHUNK_BYTES:
            emit()
        group.append(line)
        size += line_size
    emit()
    return result


class SnapshotIndex:
    """The embedding callback owns transport and durable accounting."""

    def __init__(self, settings, embed, directory):
        self.settings = settings
        self.embed = embed
        self.directory = Path(directory).resolve()
        production = Path(settings.data_dir).resolve()
        corpus = Path(settings.app_root).resolve() / "corpus" if hasattr(settings, "app_root") else None
        if (self.directory == production or self.directory.is_relative_to(production) or
                production.is_relative_to(self.directory) or
                corpus is not None and (self.directory == corpus or self.directory.is_relative_to(corpus) or
                                        corpus.is_relative_to(self.directory))):
            raise ValueError("review index may not occupy production directory")
        self.index_path = self.directory / "snapshot-index.json"
        self._snapshot = None
        self._index = None
        self._grounding_context = None

    def _response(self, texts, *, query, digest=None):
        vectors, receipt = self.embed(texts, query=query)
        if not isinstance(receipt, dict) or receipt.get("model") != self.settings.embedding_model:
            raise ValueError("embedding model mismatch")
        model_digest = receipt.get("model_digest")
        if not isinstance(model_digest, str) or not model_digest or digest is not None and model_digest != digest:
            raise ValueError("embedding digest mismatch")
        dimensions = self.settings.embedding_dimensions
        if type(receipt.get("dimensions")) is not int or receipt["dimensions"] != dimensions:
            raise ValueError("embedding dimensions mismatch")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise ValueError("embedding response count mismatch")
        for vector in vectors:
            vector_norm(vector, dimensions)
        return vectors, receipt

    def build(self, snapshot):
        if not isinstance(snapshot, dict) or snapshot.get("schema_version") != 1 or not isinstance(snapshot.get("sources"), list):
            raise ValueError("invalid pinned snapshot")
        base = snapshot.get("merge_base")
        head = snapshot.get("head_revision")
        if not all(isinstance(value, str) and len(value) in (40, 64) for value in (base, head)):
            raise ValueError("invalid pinned revisions")
        documents = []
        parts = []
        identities = set()
        byte_total = 0
        for source in snapshot["sources"]:
            if not isinstance(source, dict) or source.get("side") not in ("base", "head"):
                raise ValueError("invalid snapshot source")
            if source.get("revision") != (base if source["side"] == "base" else head):
                raise ValueError("source revision mismatch")
            if (any(not isinstance(source.get(key), str) or not source[key] for key in ("source_id", "path"))
                    or not isinstance(source.get("text"), str)):
                raise ValueError("incomplete snapshot source")
            if source["source_id"] in identities:
                raise ValueError("duplicate source identity")
            identities.add(source["source_id"])
            byte_total += len(source["text"].encode("utf-8"))
            if byte_total > _MAX_TOTAL_BYTES:
                raise ValueError("snapshot source budget exceeded")
            source_chunks = _chunks(source)
            parts.extend(source_chunks)
            documents.append({"source_id": source["source_id"], "revision": source["revision"],
                              "path": source["path"], "content_sha256": source["content_sha256"]})
            if len(parts) > _MAX_CHUNKS:
                raise ValueError("snapshot chunk budget exceeded")
        vectors = []
        receipts = []
        digest = None
        for offset in range(0, len(parts), _BATCH):
            batch = parts[offset:offset + _BATCH]
            values, receipt = self._response([part["text"] for part in batch], query=False, digest=digest)
            digest = receipt["model_digest"]
            vectors.extend(values)
            receipts.append(receipt)
        indexed = [{**part, "embedding": vector} for part, vector in zip(parts, vectors)]
        fingerprint = _digest(_canonical({"documents": documents, "chunks": indexed,
                                          "model": self.settings.embedding_model,
                                          "model_digest": digest, "dimensions": self.settings.embedding_dimensions}))
        metadata = {"documents": len(documents), "chunks": len(parts), "model": self.settings.embedding_model,
                    "model_digest": digest, "dimensions": self.settings.embedding_dimensions,
                    "fingerprint": fingerprint, "source_bytes": byte_total,
                    "status": "ready" if parts else "incomplete",
                    "prompt_eval_count": sum(r["prompt_eval_count"] for r in receipts)
                    if receipts and all(type(r.get("prompt_eval_count")) is int for r in receipts) else None}
        payload = {"metadata": metadata, "documents": documents, "chunks": indexed}
        self.directory.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix="snapshot-", suffix=".json", dir=self.directory)
        os.close(descriptor)
        try:
            Path(temporary).write_bytes(_canonical(payload))
            os.replace(temporary, self.index_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        self._snapshot = deepcopy(snapshot)
        self._index = payload
        self._grounding_context = None
        return metadata.copy()

    def _read(self):
        if self._index is None:
            if not self.index_path.is_file() or self.index_path.is_symlink():
                raise ValueError("review index missing or unsafe")
            self._index = json.loads(self.index_path.read_text(encoding="utf-8"))
        data = self._index
        metadata = data["metadata"]
        if metadata["model"] != self.settings.embedding_model or metadata["dimensions"] != self.settings.embedding_dimensions:
            raise ValueError("review index model or dimensions stale")
        expected = _digest(_canonical({"documents": data["documents"], "chunks": data["chunks"],
                                       "model": metadata["model"],
                                       "model_digest": metadata["model_digest"], "dimensions": metadata["dimensions"]}))
        if metadata["fingerprint"] != expected:
            raise ValueError("review index fingerprint mismatch")
        for part in data["chunks"]:
            if _digest(part["text"].encode("utf-8")) != part["sha256"]:
                raise ValueError("review chunk digest mismatch")
            vector_norm(part["embedding"], metadata["dimensions"])
        return data

    def search(self, query, limit=8):
        if not isinstance(query, str) or not query.strip() or type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("invalid review search request")
        data = self._read()
        if not data["chunks"]:
            return {"candidates": [], "receipt": {"status": "no_request", "model": self.settings.embedding_model,
                    "model_digest": None, "dimensions": self.settings.embedding_dimensions,
                    "prompt_eval_count": None}}
        values, receipt = self._response([query], query=True, digest=data["metadata"]["model_digest"])
        candidates = hybrid_rank(data["chunks"], values[0], set(tokens(query)))[:limit]
        return {"candidates": candidates, "receipt": receipt}

    def checks(self, impact):
        data = self._read()
        if self._snapshot is None:
            raise ValueError("snapshot required for checks")
        results = []
        requested = impact.get("checks", [])
        control = None
        self._grounding_context = None
        if impact.get("rag_checks_required") and ("retrieval" in requested or "grounded_source_use" in requested):
            control = select_retrieval_control(self._snapshot)
            if control is not None and data["chunks"]:
                results = self.search(control["query"], limit=8)["candidates"]
                if "grounded_source_use" in requested:
                    matching = [item for item in results if
                        item.get("source_id") == control["source_id"] and
                        item.get("revision") == control["revision"] and
                        item.get("path") == control["path"] and
                        control["marker"] in item.get("text", "")]
                    if matching:
                        self._grounding_context = {"control": deepcopy(control),
                                                   "candidates": deepcopy(matching[:1])}
        return assess_snapshot_rag(self._snapshot, impact, results, data["metadata"], control=control)

    def grounding_context(self):
        """Private pinned source selected by the fixed retrieval check, if available."""
        return deepcopy(self._grounding_context)

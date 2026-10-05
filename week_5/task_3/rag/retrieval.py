"""Read and authenticate the immutable structural index before embedding a query."""
import json
import math
import sqlite3
from contextlib import closing
from pathlib import Path

from indexing.chunking import chunk_document
from indexing.corpus import read_corpus


class IndexError(ValueError):
    pass


def _vector(value, dimensions):
    if not isinstance(value, list) or len(value) != dimensions or any(type(x) not in (int, float) or not math.isfinite(x) for x in value):
        raise IndexError("Invalid embedding vector")
    norm = math.sqrt(sum(x * x for x in value))
    if not math.isfinite(norm) or norm == 0:
        raise IndexError("Invalid embedding vector norm")
    return norm


def read_index(task_root, data_dir, config):
    """Return verified chunks. No API or question embedding is reached on failure."""
    path = Path(data_dir) / "indexing.sqlite3"
    if not path.is_file() or path.is_symlink():
        raise IndexError("Index is missing or unsafe")
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise IndexError("Index integrity check failed")
            rows = db.execute("SELECT value FROM metadata WHERE key='active_run_id'").fetchall()
            if len(rows) != 1:
                raise IndexError("No single active index")
            run_id = rows[0]["value"]
            run = db.execute("SELECT * FROM runs WHERE run_id=? AND status='complete'", (run_id,)).fetchone()
            if run is None:
                raise IndexError("Active index is incomplete")
            contract = json.loads(run["contract_json"])
            if contract.get("model") != config.embedding_model or contract.get("dimensions") != config.embedding_dimensions:
                raise IndexError("Embedding model or dimensions mismatch")
            corpus = read_corpus(task_root)
            saved = json.loads(run["corpus_json"])
            if run["corpus_hash"] != corpus.summary["corpus_hash"] or saved != corpus.summary:
                raise IndexError("Corpus changed since indexing")
            source_docs = [doc.__dict__ for doc in corpus.documents]
            stored_docs = [json.loads(row[0]) for row in db.execute("SELECT document_json FROM documents WHERE run_id=? ORDER BY rowid", (run_id,))]
            if sorted(stored_docs, key=lambda d: d["file"]) != sorted(source_docs, key=lambda d: d["file"]):
                raise IndexError("Indexed document provenance mismatch")
            size, overlap = contract.get("chunk_size"), contract.get("overlap")
            expected = [chunk for doc in corpus.documents for chunk in chunk_document(doc, "structural", size, overlap)]
            rows = db.execute("SELECT ordinal,chunk_id,text,embedding_json,chunk_json FROM chunks WHERE run_id=? AND strategy='structural' ORDER BY ordinal", (run_id,)).fetchall()
            if len(rows) != len(expected) or not rows:
                raise IndexError("Structural chunks missing or extra")
            verified = []
            for ordinal, (row, canonical) in enumerate(zip(rows, expected)):
                chunk = json.loads(row["chunk_json"])
                if row["ordinal"] != ordinal or row["chunk_id"] != canonical["chunk_id"] or row["text"] != canonical["text"]:
                    raise IndexError("Chunk row differs from source")
                if {key: chunk.get(key) for key in canonical} != canonical:
                    raise IndexError("Chunk metadata differs from source")
                vector = json.loads(row["embedding_json"])
                if chunk.get("embedding") != vector:
                    raise IndexError("Chunk embedding differs from row")
                _vector(vector, config.embedding_dimensions)
                verified.append(chunk)
            return verified
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError) as error:
        if isinstance(error, IndexError):
            raise
        raise IndexError("Index cannot be safely read") from error


def rank(chunks, vector, config):
    query_norm = _vector(vector, config.embedding_dimensions)
    scored = []
    for chunk in chunks:
        document = chunk["embedding"]
        score = sum(a * b for a, b in zip(vector, document)) / (query_norm * _vector(document, config.embedding_dimensions))
        if not math.isfinite(score):
            raise IndexError("Invalid similarity score")
        scored.append((score, chunk))
    scored.sort(key=lambda item: (-item[0], item[1]["chunk_id"]))
    return scored


def select_context(scored, config, candidates=None):
    sources = []
    spans = {}
    context = ""
    by_id = {item["chunk_id"]: item for item in candidates or []}
    for score, chunk in scored:
        candidate_record = by_id.get(chunk["chunk_id"])
        if len(sources) == config.top_k_after:
            if candidate_record is not None:
                candidate_record["decision"] = "top_k"
            continue
        # An overlapping window cannot be a second citation for the same bytes.
        current = (chunk["start"], chunk["end"])
        if any(current[0] < end and start < current[1] for start, end in spans.get(chunk["file"], [])):
            if candidate_record is not None:
                candidate_record["decision"] = "overlap"
            continue
        label = f"S{len(sources) + 1}"
        section = " / ".join(chunk["section"]) if isinstance(chunk["section"], list) else str(chunk["section"])
        block = f"[{label}] {chunk['file']} lines {chunk['line_start']}-{chunk['line_end']} | {section} | chunk {chunk['chunk_id']}\n{chunk['text']}"
        candidate = context + ("\n\n" if context else "") + block
        # UTF-8 bytes are a strict upper bound on byte-pair token count. This
        # avoids fetching a tokenizer asset at query time and never overfills.
        if len(candidate.encode("utf-8")) > config.max_context_tokens:
            if candidate_record is not None:
                candidate_record["decision"] = "context_budget"
            continue
        context = candidate
        spans.setdefault(chunk["file"], []).append(current)
        source = {key: chunk[key] for key in ("chunk_id", "file", "source", "title", "section", "line_start", "line_end", "document_hash", "text")}
        source.update(score=score, label=label)
        if candidate_record is not None:
            candidate_record["decision"] = "selected"
            if candidate_record["relevance_score"] is not None:
                source["relevance_score"] = candidate_record["relevance_score"]
        sources.append(source)
    return sources, context

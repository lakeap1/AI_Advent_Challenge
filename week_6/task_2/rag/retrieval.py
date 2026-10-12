"""Read and authenticate the immutable structural index before embedding a query."""
import json
import math
import re
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path
from itertools import product

from indexing.chunking import chunk_document
from indexing.corpus import read_corpus


class IndexError(ValueError):
    pass


_STOPWORDS = frozenset('the a an of in on to and or for is what how which our this '
    'при что как какие какого какой по в на и или с о из для у от ли не сколько '
    'нашем наше нашего какой'.split())
_BM25_K1 = 1.2
_BM25_B = 0.75
_RRF_K = 60
_VECTOR_RESERVE_FRACTION = 0.4


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


def reconstruct_documents(chunks):
    """Rebuild lexical documents from verified chunk offsets, without rereading corpus."""
    grouped = defaultdict(list)
    for chunk in chunks:
        grouped[chunk['file']].append(chunk)
    documents = {}
    for file, parts in grouped.items():
        parts.sort(key=lambda part: (part['start'], part['end'], part['chunk_id']))
        valid = all(type(part['start']) is int and type(part['end']) is int
            and part['start'] >= 0 and part['end'] - part['start'] == len(part['text'])
            for part in parts)
        if valid:
            characters = [None] * max(part['end'] for part in parts)
            for part in parts:
                for position, character in enumerate(part['text'], part['start']):
                    existing = characters[position]
                    if existing is not None and existing != character:
                        valid = False
                        break
                    characters[position] = character
                if not valid:
                    break
            if valid:
                documents[file] = ''.join(character if character is not None else ' '
                    for character in characters)
                continue
        # Synthetic or older test fixtures may have non-canonical offsets.
        documents[file] = '\n'.join(dict.fromkeys(part['text'] for part in parts))
    return documents


def _lexical_tokens(text):
    words = re.findall(r'[^\W_]+', text.casefold())
    return [word[:6] if len(word) > 6 and re.fullmatch(r'[а-яё]+', word) else word
        for word in words if len(word) >= 2 and word not in _STOPWORDS]


def _document_lexical_ranks(chunks, question):
    query = set(_lexical_tokens(question))
    if not query:
        return {}
    documents = reconstruct_documents(chunks)
    if not documents:
        return {}
    terms = {file: Counter(_lexical_tokens(text + ' ' + file))
        for file, text in documents.items()}
    lengths = {file: sum(counts.values()) for file, counts in terms.items()}
    average = sum(lengths.values()) / len(lengths)
    if average == 0:
        return {}
    frequency = Counter(token for counts in terms.values() for token in counts)
    number = len(terms)
    scores = {}
    for file, counts in terms.items():
        length_factor = _BM25_K1 * (1 - _BM25_B + _BM25_B * lengths[file] / average)
        score = sum(math.log(1 + (number - frequency[token] + 0.5)
            / (frequency[token] + 0.5)) * (counts[token] * (_BM25_K1 + 1))
            / (counts[token] + length_factor) for token in query if counts[token])
        if score > 0:
            scores[file] = score
    ordered = sorted(scores, key=lambda file: (-scores[file], file))
    return {file: index + 1 for index, file in enumerate(ordered)}


def rank_candidates(chunks, vector, question, config):
    """Reserve cosine leaders, then fill one bounded candidate set with document RRF."""
    scored = rank(chunks, vector, config)
    limit = min(config.top_k_before, len(scored))
    lexical_ranks = _document_lexical_ranks(chunks, question)
    if not lexical_ranks:
        return scored[:limit]
    best_by_file = {}
    for _, chunk in scored:
        best_by_file.setdefault(chunk['file'], chunk['chunk_id'])
    fused = []
    for vector_rank, (cosine, chunk) in enumerate(scored, 1):
        lexical_rank = lexical_ranks.get(chunk['file'])
        bonus = (1 / (_RRF_K + lexical_rank)
            if lexical_rank is not None and best_by_file[chunk['file']] == chunk['chunk_id']
            else 0)
        fused.append((1 / (_RRF_K + vector_rank) + bonus, cosine, chunk))
    fused.sort(key=lambda item: (-item[0], item[2]['chunk_id']))
    reserve = min(limit, max(1, math.ceil(config.top_k_before * _VECTOR_RESERVE_FRACTION)))
    selected = scored[:reserve]
    seen = {chunk['chunk_id'] for _, chunk in selected}
    for _, cosine, chunk in fused:
        if len(selected) >= limit:
            break
        if chunk['chunk_id'] not in seen:
            selected.append((cosine, chunk))
            seen.add(chunk['chunk_id'])
    return selected


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


def select_part_context(scored, part_scores, config, candidates=None):
    """Maximize covered parts within the same rendered-byte and source budgets.

    At most three lists of twenty candidates make exhaustive seed selection
    bounded. Trial packing uses no live candidate records.
    """
    by_id = {chunk['chunk_id']: (score, chunk) for score, chunk in scored}
    passing = {part: sorted((cid for cid, verdict in scores.items()
        if cid in by_id and verdict['score'] >= config.relevance_threshold),
        key=lambda cid: (-scores[cid]['score'], -by_id[cid][0], cid))
        for part, scores in part_scores.items()}

    def covered(sources):
        ids = {source['chunk_id'] for source in sources}
        return {part: ids.intersection(eligible) for part, eligible in passing.items()}

    best_ids, best_key = [], None
    for bundle in product(*(eligible + [None] for eligible in passing.values())):
        ids = list(dict.fromkeys(cid for cid in bundle if cid is not None))
        sources, _ = select_context([by_id[cid] for cid in ids], config)
        coverage = covered(sources)
        retained = [source['chunk_id'] for source in sources]
        quality = sum(max((part_scores[part][cid]['score'] for cid in ids), default=0)
            for part, ids in coverage.items())
        key = (-sum(bool(ids) for ids in coverage.values()), -quality,
               -sum(by_id[cid][0] for cid in retained), tuple(retained))
        if best_key is None or key < best_key:
            best_key, best_ids = key, retained
    # Give each part one opportunity per round to fill the remaining budget.
    for position in range(max((len(ids) for ids in passing.values()), default=0)):
        for ids in passing.values():
            if position >= len(ids) or ids[position] in best_ids:
                continue
            trial, _ = select_context([by_id[cid] for cid in best_ids + [ids[position]]], config)
            if len(trial) > len(best_ids):
                best_ids.append(ids[position])
    sources, context = select_context([by_id[cid] for cid in best_ids], config, candidates)
    mapping = {part: [source['label'] for source in sources
        if source['chunk_id'] in eligible] for part, eligible in passing.items()}
    coverage = {part: {'source_status': 'selected' if mapping[part] else
        'budget_excluded' if eligible else 'no_context', 'source_labels': mapping[part]}
        for part, eligible in passing.items()}
    selected = set(best_ids)
    for candidate in candidates or []:
        if candidate['chunk_id'] not in selected:
            candidate['decision'] = ('context_budget' if any(candidate['chunk_id'] in ids
                for ids in passing.values()) else 'threshold')
    return sources, context, mapping, coverage

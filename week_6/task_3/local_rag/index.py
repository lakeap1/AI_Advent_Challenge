"""Compatible local chunk index and bounded retrieval snapshot."""

import hashlib
import json
import math
import re
import time
from collections import Counter

from bootstrap import BASE_ROOT
from indexing.chunking import chunk_document
from indexing.corpus import read_corpus


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                      separators=(',', ':')).encode('utf-8')).hexdigest()


def corpus_fingerprint(base_root, task_root):
    base = read_corpus(base_root)
    task = read_corpus(task_root)
    return (base, task), _digest([base.summary['corpus_hash'], task.summary['corpus_hash']])


def make_chunks(corpora, config, model, digest):
    result = []
    for corpus in corpora:
        for document in corpus.documents:
            for chunk in chunk_document(document, 'structural', config['chunk_size'],
                                        config['chunk_overlap']):
                chunk = dict(chunk)
                chunk['chunk_id'] = _digest([model, digest, chunk['chunk_id']])
                result.append(chunk)
    return result


def index_identity(corpus_hash, model, digest, dimensions, config):
    return _digest([corpus_hash, model, digest, dimensions,
                    config['chunk_size'], config['chunk_overlap']])


def _terms(value):
    return re.findall(r'[^\W_]+', value.casefold(), re.UNICODE)


_STOPWORDS = frozenset({
    'а', 'без', 'бы', 'в', 'во', 'где', 'да', 'для', 'до', 'его', 'её', 'ее',
    'же', 'за', 'и', 'из', 'или', 'их', 'к', 'как', 'какая', 'какие', 'какую',
    'ли', 'на', 'не', 'но', 'о', 'об', 'он', 'она', 'они', 'от', 'по', 'при',
    'с', 'со', 'то', 'у', 'что', 'чем', 'это', 'этот', 'эти',
    'a', 'an', 'and', 'are', 'for', 'from', 'in', 'is', 'of', 'on', 'or',
    'the', 'to', 'what', 'with',
})


def _lexical_order(rows, question):
    terms = set(_terms(question)) - _STOPWORDS
    if not terms:
        return []
    counts = [Counter(_terms(chunk['text'])) for chunk, _ in rows]
    document_count = len(rows)
    average_length = sum(sum(item.values()) for item in counts) / document_count
    frequencies = Counter(term for item in counts for term in terms if item[term])
    scored = []
    for index, item in enumerate(counts):
        length = sum(item.values())
        normalization = 1.2 * (0.25 + 0.75 * length / average_length) if average_length else 1.2
        score = sum(
            math.log1p((document_count - frequencies[term] + 0.5) / (frequencies[term] + 0.5))
            * item[term] * 2.2 / (item[term] + normalization)
            for term in terms if item[term]
        )
        if score > 0:
            scored.append((index, score))
    return [index for index, _ in sorted(
        scored, key=lambda pair: (-pair[1], rows[pair[0]][0]['chunk_id']))]


def _cosine(first, second):
    product = sum(a * b for a, b in zip(first, second))
    one = math.sqrt(sum(a * a for a in first))
    two = math.sqrt(sum(b * b for b in second))
    return product / (one * two) if one and two else 0.0


def retrieve(rows, question, query_vector, metadata, config):
    started = time.monotonic()
    if not rows:
        return {'sources': [], 'context': '', 'context_sha256': hashlib.sha256(b'').hexdigest(),
                'index_id': metadata['index_id'], 'elapsed_seconds': time.monotonic() - started}
    vectors = sorted(enumerate(rows), key=lambda pair: _cosine(pair[1][1], query_vector), reverse=True)
    lexical = _lexical_order(rows, question)
    ranks = {}
    for order in ([index for index, _ in vectors], lexical):
        for rank, index in enumerate(order, 1):
            ranks[index] = ranks.get(index, 0.0) + 1.0 / (60 + rank)
    ordered = sorted(ranks, key=lambda index: (-ranks[index], rows[index][0]['chunk_id']))
    sources, pieces = [], []
    budget = config['context_bytes']
    for index in ordered:
        if len(sources) >= config['top_k']:
            break
        chunk = rows[index][0]
        label = f'S{len(sources) + 1}'
        header = f'[{label}] {chunk["title"]} | {chunk["source"]} | {" / ".join(chunk["section"])} | {chunk["file"]}:{chunk["line_start"]}-{chunk["line_end"]} | {chunk["chunk_id"]}\n'
        piece = header + chunk['text']
        new_context = '\n\n'.join(pieces + [piece])
        if len(new_context.encode('utf-8')) > budget:
            continue
        pieces.append(piece)
        sources.append({key: chunk[key] for key in ('chunk_id', 'file', 'title', 'source',
                                                      'section', 'line_start', 'line_end', 'text')}
                       | {'label': label, 'score': round(ranks[index], 9)})
    context = '\n\n'.join(pieces)
    return {'sources': sources, 'context': context,
            'context_sha256': hashlib.sha256(context.encode('utf-8')).hexdigest(),
            'index_id': metadata['index_id'], 'elapsed_seconds': time.monotonic() - started}

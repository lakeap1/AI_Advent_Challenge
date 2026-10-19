"""Pure, deterministic Unicode BM25 and cosine/RRF ranking shared by retrievals."""
from collections import Counter
import math
import re


STOPWORDS = frozenset("the a an of in on to and or for is what how which this при что как какие какой по в на и или с о из для у от ли не".split())
RRF_K = 60


def tokens(value):
    words = re.findall(r"[^\W_]+", value.casefold())
    return [word[:6] if len(word) > 6 and re.fullmatch(r"[а-яё]+", word) else word
            for word in words if len(word) >= 2 and word not in STOPWORDS]


def vector_norm(value, dimensions):
    if (not isinstance(value, list) or len(value) != dimensions or
            any(type(x) not in (int, float) or not math.isfinite(x) for x in value)):
        raise ValueError("invalid embedding vector or dimensions")
    norm = math.sqrt(sum(x * x for x in value))
    if not math.isfinite(norm) or norm <= 0:
        raise ValueError("invalid embedding norm")
    return norm


def bm25_scores(chunks, terms):
    if not chunks or not terms:
        return {}
    counts = {c["chunk_id"]: Counter(tokens(c["title"] + " " + c["section"] + " " + c["text"])) for c in chunks}
    lengths = {key: sum(words.values()) for key, words in counts.items()}
    average = sum(lengths.values()) / len(lengths)
    if not average:
        return {}
    frequency = Counter(term for words in counts.values() for term in words)
    count = len(chunks)
    scores = {}
    for key, words in counts.items():
        factor = 1.2 * (0.25 + 0.75 * lengths[key] / average)
        score = sum(math.log(1 + (count - frequency[t] + 0.5) / (frequency[t] + 0.5))
                    * (words[t] * 2.2) / (words[t] + factor) for t in terms if words[t])
        scores[key] = score
    return scores


def hybrid_rank(chunks, query_vector, terms):
    """Return ranked source fields plus scores, never the embedding itself."""
    if not chunks:
        return []
    dimensions = len(query_vector) if isinstance(query_vector, list) else 0
    norm = vector_norm(query_vector, dimensions)
    ids = [chunk["chunk_id"] for chunk in chunks]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate chunk identity")
    cosine = {c["chunk_id"]: sum(a * b for a, b in zip(query_vector, c["embedding"])) /
              (norm * vector_norm(c["embedding"], dimensions)) for c in chunks}
    vector_order = sorted(chunks, key=lambda c: (-cosine[c["chunk_id"]], c["chunk_id"]))
    vector_rank = {c["chunk_id"]: rank for rank, c in enumerate(vector_order, 1)}
    bm25 = bm25_scores(chunks, terms)
    lexical_order = sorted((c for c in chunks if bm25.get(c["chunk_id"], 0) > 0),
                           key=lambda c: (-bm25[c["chunk_id"]], c["chunk_id"]))
    lexical_rank = {c["chunk_id"]: rank for rank, c in enumerate(lexical_order, 1)}
    candidates = []
    for chunk in chunks:
        key = chunk["chunk_id"]
        score = 1 / (RRF_K + vector_rank[key])
        if key in lexical_rank:
            score += 1 / (RRF_K + lexical_rank[key])
        candidate = {field: value for field, value in chunk.items() if field != "embedding"}
        candidate.update(cosine=cosine[key], bm25=bm25.get(key, 0.0), rrf=score)
        candidates.append(candidate)
    candidates.sort(key=lambda c: (-c["rrf"], -c["cosine"], c["chunk_id"]))
    return candidates

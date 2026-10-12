"""Document lexical recall before the existing local RAG model filter."""
import json
import math
from types import SimpleNamespace

import pytest

from agent import load_config as load_agent_config
from rag import chat, retrieval
from rag.chat import RagChatAgent
from rag.chat_store import RagSQLiteStore
from rag.config import load_config as load_rag_config


QUERY_VECTOR = [1.0] + [0.0] * 1535
QUESTION = 'Как HTTP-маршрут сохраняет отчёт?'
TARGET_FILE = 'report_routes.py'
TARGET_ID = 'target-code'


def chunk(chunk_id, file, text, cosine, *, start=0):
    return {'chunk_id': chunk_id, 'file': file, 'source': 'local',
        'title': file, 'section': ['Document'], 'document_hash': 'fixture',
        'start': start, 'end': start + len(text), 'line_start': 1,
        'line_end': text.count('\n') + 1, 'text': text,
        'embedding': [cosine, math.sqrt(1 - cosine * cosine)] + [0.0] * 1534}


def mixed_topic_chunks():
    preamble = 'HTTP POST endpoint for report operations.\n'
    code = 'def save_report(store, body):\n    return store.persist(body)\n'
    chunks = [chunk(f'noise-{index:02}', f'noise_{index:02}.py',
        f'Rendering note number {index}.\n', 0.99 - index * 0.01)
        for index in range(63)]
    chunks.extend([chunk('target-preamble', TARGET_FILE, preamble, 0.1),
        chunk(TARGET_ID, TARGET_FILE, code, 0.2, start=len(preamble))])
    return chunks, preamble + code


class Embedder:
    def __init__(self):
        self.calls = []

    def embed(self, texts):
        self.calls.append(texts)
        return {'model': 'text-embedding-3-small', 'vectors': [QUERY_VECTOR],
            'usage': {'prompt_tokens': 5, 'total_tokens': 5}}


class Transport:
    def __init__(self):
        self.calls = []

    def create(self, payload, timeout):
        from test_chat_rag import response
        self.calls.append(payload)
        if payload.get('text', {}).get('format', {}).get('name') == 'filter':
            candidates = json.loads(payload['input'][0]['content'])['candidates']
            return response(json.dumps({'scores': {item['chunk_id']: {
                'score': 3 if item['chunk_id'] == TARGET_ID else 0,
                'reason': 'Direct answer' if item['chunk_id'] == TARGET_ID else 'No answer'}
                for item in candidates}}))
        return response(json.dumps({'status': 'answered', 'claims': [
            {'text': 'The route persists the report.', 'source_labels': ['S1']}],
            'clarification': ''}))


def test_hybrid_recall_promotes_code_from_vector_rank_64_with_one_embedding(tmp_path, monkeypatch):
    chunks, _ = mixed_topic_chunks()
    config = load_rag_config()
    vector_rank = [item['chunk_id'] for _, item in retrieval.rank(chunks, QUERY_VECTOR, config)]
    assert vector_rank.index(TARGET_ID) + 1 == 64
    monkeypatch.setattr(chat, 'read_index', lambda *_: chunks)
    transport, embedder = Transport(), Embedder()
    agent = RagChatAgent(load_agent_config(), transport,
        RagSQLiteStore(tmp_path / 'dialogue.sqlite3'), index_data_dir=tmp_path,
        embedder=embedder)

    result = agent.run(QUESTION, rag_mode='filter')

    filter_payload = next(call for call in transport.calls
        if call.get('text', {}).get('format', {}).get('name') == 'filter')
    candidates = json.loads(filter_payload['input'][0]['content'])['candidates']
    assert len(candidates) == config.top_k_before == 20
    assert TARGET_ID in {item['chunk_id'] for item in candidates}
    assert len(embedder.calls) == 1 and embedder.calls[0] == [QUESTION]
    assert result.status == 'ok', result
    assert len(transport.calls) == 2
    parent = next(item for item in agent.state()['requests']
        if item['metadata']['kind'] == 'answer')
    rag = parent['metadata']['rag']
    assert rag['counts']['candidates'] == 20
    assert [source['chunk_id'] for source in rag['sources']] == [TARGET_ID]
    assert rag['sources'][0]['score'] == pytest.approx(0.2)
    assert next(item for item in rag['candidates'] if item['chunk_id'] == TARGET_ID)['score'] == pytest.approx(0.2)
    assert rag['context_budget']['used_utf8_bytes'] <= config.max_context_tokens
    agent.close()


def test_document_reconstruction_deduplicates_overlapping_chunk_positions():
    text = 'HTTP route\nsave_report persists the report\n'
    first = chunk('first', 'routes.py', text[:27], 0.9)
    second = chunk('second', 'routes.py', text[20:], 0.8, start=20)

    assert retrieval.reconstruct_documents([second, first]) == {'routes.py': text}


def test_document_reconstruction_tolerates_noncanonical_fixture_offsets():
    fixture = chunk('fixture', 'fixture.py', 'HTTP endpoint.\n', 0.5)
    fixture['end'] += 10

    assert retrieval.reconstruct_documents([fixture]) == {
        'fixture.py': 'HTTP endpoint.\n'}


def test_no_lexical_match_keeps_exact_vector_order_and_cosine():
    chunks, _ = mixed_topic_chunks()
    config = load_rag_config()

    expected = retrieval.rank(chunks, QUERY_VECTOR, config)[:config.top_k_before]
    actual = retrieval.rank_candidates(chunks, QUERY_VECTOR, 'zzzxxyy', config)

    assert actual == expected
    assert len(actual) == config.top_k_before


def test_vector_prefix_preserves_high_rank_secondary_chunks_from_same_file():
    shared = [chunk(f'shared-{index}', 'shared.py', f'Unrelated render note {index}.\n',
        0.99 - index * 0.01, start=index * 100) for index in range(8)]
    lexical = [chunk(f'lex-{index}', f'lex_{index:02}.py', 'HTTP endpoint documentation.\n',
        0.70 - index * 0.01) for index in range(20)]
    config = SimpleNamespace(embedding_dimensions=1536, top_k_before=20)
    vector_top = retrieval.rank(shared + lexical, QUERY_VECTOR, config)[:8]

    selected = retrieval.rank_candidates(shared + lexical, QUERY_VECTOR, 'HTTP', config)

    assert len(selected) == 20
    assert [item['chunk_id'] for _, item in selected[:8]] == [
        item['chunk_id'] for _, item in vector_top]
    assert 'shared-1' in {item['chunk_id'] for _, item in selected}
    assert len({item['chunk_id'] for _, item in selected}) == 20


def test_candidate_budget_one_and_fewer_available_chunks_are_respected():
    chunks = [chunk('vector-first', 'vector.py', 'Rendering note.\n', 0.9),
        chunk('lexical-second', 'lexical.py', 'HTTP endpoint.\n', 0.2)]
    one = SimpleNamespace(embedding_dimensions=1536, top_k_before=1)
    five = SimpleNamespace(embedding_dimensions=1536, top_k_before=5)

    assert [item['chunk_id'] for _, item in retrieval.rank_candidates(
        chunks, QUERY_VECTOR, 'HTTP', one)] == ['vector-first']
    available = retrieval.rank_candidates(chunks, QUERY_VECTOR, 'HTTP', five)
    assert len(available) == 2
    assert {item['chunk_id'] for _, item in available} == {
        'vector-first', 'lexical-second'}


def test_lexical_file_ties_are_deterministic_across_chunk_input_order():
    chunks = [chunk('z', 'a.py', 'HTTP signal\n', 0.5),
        chunk('a', 'b.py', 'HTTP signal\n', 0.5)]
    config = load_rag_config()

    first = retrieval.rank_candidates(chunks, QUERY_VECTOR, 'HTTP', config)
    reversed_input = retrieval.rank_candidates(list(reversed(chunks)), QUERY_VECTOR, 'HTTP', config)

    assert [item['chunk_id'] for _, item in first] == [item['chunk_id'] for _, item in reversed_input]
    assert all(cosine == pytest.approx(0.5) for cosine, _ in first)

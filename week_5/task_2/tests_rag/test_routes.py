"""HTTP behavior over the real RAG service and its durable SQLite ledger."""
import uuid
import time
import json
import pytest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from flask import Flask

from app import create_app
from rag_routes import register_rag
from rag import service as service_module
from rag.service import RagService
from tests_rag.test_rag import Embedder, Transport, indexed, response


def make_client(root, data, transport, embedder):
    service = RagService(root, data, transport=transport, embedder=embedder)
    app = Flask(__name__, template_folder=str(root / "templates"))
    register_rag(app, data, service=service)
    return app.test_client(), service


def test_http_pair_persists_sources_stages_and_cost_after_reload(indexed):
    root, data = indexed
    session = str(uuid.uuid4())
    transport = Transport([response("Без базы."), response("С базой [S1].")])
    client, service = make_client(root, data, transport, Embedder())
    try:
        result = client.post("/api/rag/compare", json={"question": "Где alpha?", "session_id": session})
        assert result.status_code == 200
        pair = result.json
        assert pair["plain"]["text"] == "Без базы."
        assert pair["rag"]["text"] == "С базой [S1]."
        assert pair["rag"]["sources"][0]["file"] == "alpha.md"
        assert "Unique alpha evidence" in pair["rag"]["context"]
        state = client.get("/api/rag/state", query_string={"session_id": session}).json
        assert [r["mode"] for r in state["requests"]] == ["plain", "rag"]
        assert [s["kind"] for r in state["requests"] for s in r["stages"]] == ["generation", "query_embedding", "generation"]
        assert state["cumulative"]["complete"] is True
        assert state["cumulative"]["known_cost_usd"] > 0
    finally:
        service.close()
    reopened, next_service = make_client(root, data, Transport([]), Embedder())
    try:
        assert reopened.get("/api/rag/state", query_string={"session_id": session}).json == state
    finally:
        next_service.close()


def test_http_rejects_bad_payload_before_external_call(tmp_path):
    transport, embedder = Transport(), Embedder()
    client, service = make_client(tmp_path, tmp_path / "data", transport, embedder)
    session = str(uuid.uuid4())
    try:
        invalid = [
            client.post("/api/rag/ask", data="[]", content_type="application/json"),
            client.post("/api/rag/ask", json={"question": "x", "mode": "plain", "session_id": session, "extra": "x"}),
            client.post("/api/rag/ask", json={"question": "x", "mode": "wrong", "session_id": session}),
            client.post("/api/rag/compare", json={"question": "x", "session_id": "bad"}),
            client.get("/api/rag/state?session_id=bad"),
        ]
        assert all(item.status_code == 400 and item.json["status"] == "rejected" for item in invalid)
        assert transport.payloads == [] and embedder.calls == []
        assert client.get("/api/rag/state", query_string={"session_id": session}).json["requests"] == []
    finally:
        service.close()


@pytest.mark.parametrize('question,code', [('   ', 'input_invalid'), ('a' * 4001, 'input_too_long'), ('\ud800', 'input_invalid')], ids=['empty', 'overlong', 'non-utf8'])
def test_input_policy_refusal_is_saved_without_external_call(tmp_path, question, code):
    transport, embedder = Transport(), Embedder()
    client, service = make_client(tmp_path, tmp_path / 'data', transport, embedder)
    session = str(uuid.uuid4())
    try:
        result = client.post('/api/rag/ask', json={'question': question, 'mode': 'plain', 'session_id': session})
        assert result.status_code == 400 and result.json['code'] == code
        state = client.get('/api/rag/state', query_string={'session_id': session}).json
        assert len(state['requests']) == 1
        assert state['requests'][0]['input_policy']['code'] == code
        assert state['requests'][0]['stages'][0]['api_called'] is False
        assert transport.payloads == [] and embedder.calls == []
    finally:
        service.close()


def test_http_shows_incomplete_cost_when_provider_omits_usage(tmp_path):
    session = str(uuid.uuid4())
    transport = Transport([response("Нет данных.", usage=False)])
    client, service = make_client(tmp_path, tmp_path / "data", transport, Embedder())
    try:
        result = client.post("/api/rag/ask", json={"question": "Что известно?", "mode": "plain", "session_id": session})
        assert result.status_code == 200 and result.json["status"] == "ok"
        state = client.get("/api/rag/state", query_string={"session_id": session}).json
        assert state["cumulative"]["complete"] is False
        assert state["cumulative"]["unknown_calls"] == 1
        assert state["requests"][0]["cost_usd"] is None
    finally:
        service.close()


def test_existing_pages_survive_and_rag_starts_lazily(tmp_path):
    (tmp_path / 'evaluation.json').write_text(json.dumps({
        'status': 'failed', 'questions': [{'id': 'q01', 'plain': {'status': 'error'}, 'rag': None,
                                          'assessment': 'pending'}], 'summary': {'assessment': 'pending'}
    }), encoding='utf-8')
    app = create_app(data_dir=tmp_path)
    client = app.test_client()
    assert app.extensions['rag_service'] is None
    assert client.get('/').status_code == 200
    assert client.get('/indexing').status_code == 200
    assert client.get('/rag').status_code == 200
    assert app.extensions['rag_service'] is None
    try:
        questions = client.get('/api/rag/questions')
        assert questions.status_code == 200 and len(questions.json['questions']) == 10
        report = client.get('/api/rag/evaluation').json
        assert report['status'] == 'failed' and report['questions'][0]['rag'] is None
    finally:
        if app.extensions['rag_service']:
            app.extensions['rag_service'].close()


def test_parallel_first_reads_share_one_ledger_owner(tmp_path, monkeypatch):
    actual = service_module.RagService
    created = []

    def slow_create(*args, **kwargs):
        time.sleep(0.06)
        service = actual(*args, **kwargs)
        created.append(service)
        return service

    monkeypatch.setattr(service_module, 'RagService', slow_create)
    app = Flask(__name__)
    register_rag(app, tmp_path)
    start = Barrier(2)
    session = str(uuid.uuid4())

    def get(path):
        start.wait(timeout=3)
        return app.test_client().get(path)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(get, ['/api/rag/questions', f'/api/rag/state?session_id={session}']))
        assert [response.status_code for response in responses] == [200, 200]
        assert len(created) == 1
    finally:
        for service in created:
            service.close()

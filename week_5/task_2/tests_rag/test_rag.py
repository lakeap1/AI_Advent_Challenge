"""Public RAG boundaries over an actual day-21 SQLite index and fake HTTP calls."""
import json
import os
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from indexing.chunking import chunk_document
from indexing.client import EmbeddingError
from indexing.config import load_config as index_config
from indexing.corpus import read_corpus
from indexing.store import IndexStore
from rag.service import RagService
from rag.cli import evaluate, import_index
from rag.config import load_config as rag_config
from rag.retrieval import read_index


def response(text, usage=True):
    value = {"status": "completed", "model": "gpt-6-luna", "service_tier": "default",
             "output": [{"type": "message", "role": "assistant", "status": "completed",
                         "content": [{"type": "output_text", "text": text}]}]}
    if usage:
        value["usage"] = {"input_tokens": 20, "output_tokens": 8, "total_tokens": 28,
                          "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
                          "output_tokens_details": {"reasoning_tokens": 2}}
    return value


class Transport:
    def __init__(self, values=None):
        self.values = list(values or [response("Готово [S1].")])
        self.payloads = []

    def create(self, payload, timeout):
        self.payloads.append(payload)
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class Embedder:
    def __init__(self, vector=None, error=None):
        self.calls = []
        self.vector = vector or [1.0] + [0.0] * 1535
        self.error = error

    def embed(self, texts):
        self.calls.append(texts)
        if self.error:
            raise self.error
        return {"model": "text-embedding-3-small", "vectors": [self.vector],
                "usage": {"prompt_tokens": 5, "total_tokens": 5}}


@pytest.fixture
def indexed(tmp_path):
    root = tmp_path / "task"
    (root / "corpus").mkdir(parents=True)
    (root / "corpus" / "manifest.json").write_text(json.dumps({"version": 1, "files": ["alpha.md", "beta.md"]}), encoding="utf-8")
    (root / "alpha.md").write_text("# Alpha\nUnique alpha evidence.\n", encoding="utf-8")
    (root / "beta.md").write_text("# Beta\nDifferent beta evidence.\n", encoding="utf-8")
    corpus = read_corpus(root)
    data = tmp_path / "data"
    data.mkdir()
    store = IndexStore(data / "indexing.sqlite3")
    cfg = index_config()
    run = store.start_run(corpus.summary, cfg.contract)
    store.save_documents(run, [doc.__dict__ for doc in corpus.documents])
    for ordinal, doc in enumerate(corpus.documents):
        chunk = chunk_document(doc, "structural")[0]
        chunk["embedding"] = ([1.0] + [0.0] * 1535) if ordinal == 0 else ([0.0, 1.0] + [0.0] * 1534)
        store.save_chunks(run, "structural", ordinal, [chunk])
    store.publish(run, {"structural": {"chunks": 2}})
    store.close()
    return root, data


def test_plain_has_no_index_or_embedding(tmp_path):
    root = tmp_path / "absent"
    root.mkdir()
    transport, embedder = Transport([response("Прямой ответ.")]), Embedder()
    service = RagService(root, tmp_path / "data", transport=transport, embedder=embedder)
    result = service.ask("Что известно?", "plain", str(uuid.uuid4()))
    assert result["status"] == "ok" and result["text"] == "Прямой ответ."
    assert result["sources"] == [] and embedder.calls == []
    assert "Unique alpha evidence" not in json.dumps(transport.payloads)
    assert transport.payloads[0]["input"] == [{"role": "user", "content": "Что известно?"}]
    assert result["usage"]["total_tokens"] == 28
    service.close()


def test_rag_ranks_real_vectors_and_sends_exact_context(indexed):
    root, data = indexed
    transport, embedder = Transport(), Embedder()
    service = RagService(root, data, transport=transport, embedder=embedder)
    result = service.ask("Где alpha?", "rag", str(uuid.uuid4()))
    assert result["status"] == "ok" and embedder.calls == [["Где alpha?"]], result
    assert result["sources"][0]["file"] == "alpha.md"
    assert result["sources"][0]["text"] == (root / "alpha.md").read_bytes().decode("utf-8")
    assert "Unique alpha evidence" in transport.payloads[0]["input"][0]["content"]
    assert transport.payloads[0]["input"][0]["content"].endswith(result["context"])
    assert result["sources"][0]["score"] > result["sources"][1]["score"]
    assert result["context_budget"]["method"] == "utf8_bytes_upper_bound"
    assert result["context_budget"]["used_utf8_bytes"] == len(result["context"].encode("utf-8")) <= 6000
    service.close()


@pytest.mark.parametrize("question,mode,session", [(" ", "plain", "ok"), ("Hello", "wrong", "ok"), ("Hello", "plain", "bad")])
def test_invalid_input_never_calls_api(tmp_path, question, mode, session):
    transport, embedder = Transport(), Embedder()
    service = RagService(tmp_path, tmp_path / "data", transport=transport, embedder=embedder)
    result = service.ask(question, mode, session)
    assert result["status"] == "rejected" and result["code"]
    assert transport.payloads == [] and embedder.calls == []
    service.close()


@pytest.mark.parametrize("field,expected_code", [("question", "input_invalid"),
                                                  ("mode", "invalid_mode"),
                                                  ("session_id", "invalid_session")])
def test_invalid_unicode_input_is_durably_rejected_without_unsafe_json(tmp_path, field, expected_code):
    transport, embedder = Transport(), Embedder()
    data = tmp_path / "data"
    session = str(uuid.uuid4())
    values = {"question": "Normal question", "mode": "plain", "session_id": session}
    values[field] = "\ud800"  # A lone surrogate cannot be encoded as UTF-8.
    service = RagService(tmp_path, data, transport=transport, embedder=embedder)
    result = service.ask(values["question"], values["mode"], values["session_id"])
    assert result["status"] == "rejected" and result["code"] == expected_code
    assert transport.payloads == [] and embedder.calls == []
    result_json = json.dumps(result, ensure_ascii=False)
    result_json.encode("utf-8")
    if field != "session_id":
        json.dumps(service.state(session), ensure_ascii=False).encode("utf-8")
    service.close()
    with sqlite3.connect(data / "rag.sqlite3") as db:
        rows = db.execute("SELECT session_id,record_json FROM requests").fetchall()
    assert len(rows) == 1
    stored = json.loads(rows[0][1])
    assert stored["input_policy"]["code"] == expected_code
    rows[0][0].encode("utf-8")
    rows[0][1].encode("utf-8")


@pytest.mark.parametrize("damage", ["source", "model", "dimension", "vector", "text"])
def test_bad_index_stops_before_api(indexed, damage):
    root, data = indexed
    db = data / "indexing.sqlite3"
    if damage == "source":
        (root / "alpha.md").write_text("# Alpha\nTampered evidence.\n", encoding="utf-8")
    else:
        with sqlite3.connect(db) as conn:
            if damage in ("model", "dimension"):
                contract = json.loads(conn.execute("SELECT contract_json FROM runs").fetchone()[0])
                contract["model" if damage == "model" else "dimensions"] = "other" if damage == "model" else 12
                conn.execute("UPDATE runs SET contract_json=?", (json.dumps(contract),))
            else:
                row = conn.execute("SELECT rowid,chunk_json FROM chunks WHERE strategy='structural' LIMIT 1").fetchone()
                chunk = json.loads(row[1])
                if damage == "vector":
                    chunk["embedding"][0] = float("nan")
                else:
                    chunk["text"] = "fabricated"
                conn.execute("UPDATE chunks SET chunk_json=?", (json.dumps(chunk),))
    transport, embedder = Transport(), Embedder()
    service = RagService(root, data, transport=transport, embedder=embedder)
    result = service.ask("Где alpha?", "rag", str(uuid.uuid4()))
    assert result["status"] == "error" and transport.payloads == [] and embedder.calls == []
    service.close()


def test_rejected_answer_stops_pair_and_preserves_cost(indexed):
    root, data = indexed
    transport, embedder = Transport([response("Некорректная ссылка [S9].")]), Embedder()
    session = str(uuid.uuid4())
    service = RagService(root, data, transport=transport, embedder=embedder)
    pair = service.compare("Вопрос", session)
    assert pair["status"] == "rejected" and pair["rag"] is None
    assert len(transport.payloads) == 1 and embedder.calls == []
    service.close()
    restarted = RagService(root, data, transport=Transport(), embedder=Embedder())
    state = restarted.state(session)
    assert state["cumulative"]["known_cost_usd"] > 0
    assert len(state["requests"]) == 1 and state["requests"][0]["output_policy"]["passed"] is False
    restarted.close()


def test_unknown_usage_is_incomplete(tmp_path):
    session = str(uuid.uuid4())
    service = RagService(tmp_path, tmp_path / "data", transport=Transport([response("Ответ.", usage=False)]), embedder=Embedder())
    assert service.ask("Вопрос", "plain", session)["status"] == "ok"
    assert service.state(session)["cumulative"]["complete"] is False
    service.close()


def test_embedding_failure_preserves_reported_usage_and_stops_pair(indexed):
    root, data = indexed
    class FailedEmbedding(Exception):
        metadata = {"api_called": True, "actual_model": "text-embedding-3-small",
                    "usage": {"prompt_tokens": 7, "total_tokens": 7}}

    transport = Transport([response("Первый ответ.")])
    embedder = Embedder(error=FailedEmbedding())
    session = str(uuid.uuid4())
    service = RagService(root, data, transport=transport, embedder=embedder)
    pair = service.compare("Вопрос", session)
    assert pair["status"] == "error" and pair["rag"]["code"] == "retrieval_failed"
    assert len(transport.payloads) == 1 and len(embedder.calls) == 1
    state = service.state(session)
    assert state["requests"][1]["stages"][0]["usage"]["input_tokens"] == 7
    assert state["requests"][1]["stages"][0]["usage"]["cached_input_tokens"] is None
    assert state["cumulative"]["complete"] is True
    service.close()


@pytest.mark.parametrize('failure_shape', ['client_error', 'returned_dict'])
def test_unexpected_embedding_model_has_unknown_cost_in_experiment(indexed, failure_shape):
    root, data = indexed
    if failure_shape == 'client_error':
        embedder = Embedder(error=EmbeddingError('wrong model',
            usage={'prompt_tokens': 5, 'total_tokens': 5},
            actual_model='different-embedding-model', invalid_output=True))
    else:
        class WrongModel(Embedder):
            def embed(self, texts):
                output = super().embed(texts)
                output['model'] = 'different-embedding-model'
                return output
        embedder = WrongModel()
    transport = Transport()
    session = str(uuid.uuid4())
    service = RagService(root, data, transport=transport, embedder=embedder)
    result = service.ask('Вопрос', 'rag', session)
    stage = service.state(session)['requests'][0]['stages'][0]
    assert result['status'] == 'error'
    assert stage['provider_usage'] == {'prompt_tokens': 5, 'total_tokens': 5}
    assert stage['usage']['input_tokens'] == 5
    assert stage['actual_model'] == 'different-embedding-model'
    assert stage['cost_usd'] is None
    assert not service.state(session)['cumulative']['complete']
    assert transport.payloads == []
    service.close()


@pytest.mark.parametrize("bad_response", [
    {"status": "incomplete", "model": "gpt-6-luna", "service_tier": "default", "usage": response("x")["usage"]},
    {"status": "completed", "model": "gpt-6-luna", "service_tier": "default", "usage": response("x")["usage"],
     "output": [{"type": "message", "role": "assistant", "status": "completed", "content": [{"type": "refusal"}]}]},
])
def test_provider_rejection_keeps_cost_and_prevents_rag(indexed, bad_response):
    root, data = indexed
    transport, embedder = Transport([bad_response]), Embedder()
    session = str(uuid.uuid4())
    service = RagService(root, data, transport=transport, embedder=embedder)
    pair = service.compare("Вопрос", session)
    assert pair["status"] == "rejected" and pair["rag"] is None
    assert len(transport.payloads) == 1 and embedder.calls == []
    assert service.state(session)["cumulative"]["known_cost_usd"] > 0
    service.close()


def test_restart_marks_inflight_call_interrupted(tmp_path):
    session = str(uuid.uuid4())
    script = """import os,sys
from rag.service import RagService
class CrashTransport:
    def create(self,payload,timeout):
        os._exit(17)
service=RagService(sys.argv[1],sys.argv[2],transport=CrashTransport())
service.ask('Вопрос','plain',sys.argv[3])
"""
    child = subprocess.run([sys.executable, "-B", "-c", script, str(tmp_path), str(tmp_path / "data"), session],
                           cwd=Path(__file__).resolve().parents[1], env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                           capture_output=True, text=True, timeout=10)
    assert child.returncode == 17, child.stderr
    restarted = RagService(tmp_path, tmp_path / "data", transport=Transport(), embedder=Embedder())
    state = restarted.state(session)
    assert state["requests"][0]["status"] == "interrupted"
    assert state["requests"][0]["stages"][0]["status"] == "interrupted"
    assert state["cumulative"]["complete"] is False
    restarted.close()


def test_second_store_cannot_interrupt_live_pending_request(tmp_path):
    class ConcurrentOpenTransport:
        def create(self, payload, timeout):
            before = owner.state(session)["requests"][0]
            assert before["status"] == "pending"
            assert before["stages"][0]["status"] == "pending"
            try:
                other = RagService(tmp_path, tmp_path / "data", transport=Transport(), embedder=Embedder())
            except OSError as error:
                assert "already in use" in str(error)
            else:
                other.close()
                pytest.fail("A second store acquired an active request")
            after = owner.state(session)["requests"][0]
            assert after["status"] == "pending"
            assert after["stages"][0]["status"] == "pending"
            return response("Ответ после concurrent open.")

    session = str(uuid.uuid4())
    owner = RagService(tmp_path, tmp_path / "data", transport=ConcurrentOpenTransport(), embedder=Embedder())
    result = owner.ask("Вопрос", "plain", session)
    assert result["status"] == "ok"
    assert owner.state(session)["requests"][0]["status"] == "ok"
    owner.close()
    reopened = RagService(tmp_path, tmp_path / "data", transport=Transport(), embedder=Embedder())
    assert reopened.state(session)["requests"][0]["status"] == "ok"
    reopened.close()


def test_cli_import_is_verified_and_does_not_replace_target(indexed, tmp_path):
    root, data = indexed
    source = data / "indexing.sqlite3"
    source_bytes = source.read_bytes()
    target = tmp_path / "imported"
    result = import_index(root, source, target)
    assert result["structural_chunks"] == 2
    assert len(read_index(root, target, rag_config())) == 2
    with pytest.raises(ValueError, match="already exists"):
        import_index(root, source, target)
    assert source.read_bytes() == source_bytes


def test_evaluation_sends_ten_clean_pairs_without_gold(indexed, tmp_path):
    root, data = indexed
    (root / "evaluation").mkdir()
    gold = [{"id": f"q{n:02}", "question": f"Question {n}",
             "expected_facts": ["SECRET GOLD FACT"],
             "expected_sources": [{"file": "alpha.md", "evidence": "SECRET GOLD EVIDENCE"}],
             "unanswerable": False} for n in range(1, 11)]
    (root / "evaluation" / "questions.json").write_text(json.dumps({"version": 1, "scope": "fixture", "questions": gold}), encoding="utf-8")
    transport = Transport([answer for _ in range(10) for answer in (response("plain"), response("grounded [S1]"))])
    embedder = Embedder()
    service = RagService(root, data, transport=transport, embedder=embedder)
    report = evaluate(service, tmp_path / "report.json")
    assert report["status"] == "complete" and len(report["questions"]) == 10
    assert report["summary"]["assessment"] == "pending"
    assert len(transport.payloads) == 20 and len(embedder.calls) == 10
    assert "SECRET GOLD" not in json.dumps(transport.payloads)
    assert service.evaluation()["status"] == "complete"
    service.close()


def test_evaluation_stops_on_first_failed_pair(indexed, tmp_path):
    root, data = indexed
    (root / "evaluation").mkdir()
    gold = [{"id": f"q{n:02}", "question": f"Question {n}", "expected_facts": [],
             "expected_sources": [], "unanswerable": True} for n in range(1, 11)]
    (root / "evaluation" / "questions.json").write_text(json.dumps({"version": 1, "scope": "fixture", "questions": gold}), encoding="utf-8")
    transport = Transport([response("invalid [S9]")])
    embedder = Embedder()
    service = RagService(root, data, transport=transport, embedder=embedder)
    report = evaluate(service, tmp_path / "failed.json")
    assert report["status"] == "failed" and len(report["questions"]) == 1
    assert len(transport.payloads) == 1 and embedder.calls == []
    assert service.evaluation()["status"] == "failed"
    service.close()

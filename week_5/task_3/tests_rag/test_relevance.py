"""Day 23 behavior through a verified SQLite index and fake provider responses."""
import json
import time
import uuid
from dataclasses import replace

import pytest

from rag.service import RagService
from rag.config import load_config
from rag.retrieval import read_index
from agent.transport import TransportError
from test_rag import Embedder, Transport, indexed, response


def scores(*rows):
    return response(json.dumps({"scores": [
        {"chunk_id": chunk_id, "score": score, "reason": reason}
        for chunk_id, score, reason in rows
    ]}))


def test_filter_replaces_noisy_top_hit_in_actual_generation_context(indexed):
    root, data = indexed
    # The fake provider supplies judgments only; production must apply them.
    alpha, beta = [chunk["chunk_id"] for chunk in read_index(root, data, load_config())]
    transport = Transport([scores((alpha, 1, "Topic only"), (beta, 3, "Direct evidence")), response("Ответ [S1].")])
    service = RagService(root, data, transport=transport, embedder=Embedder())
    result = service.ask("Что говорит beta?", "filter", str(uuid.uuid4()))
    assert result["status"] == "ok", result
    assert [source["file"] for source in result["sources"]] == ["beta.md"]
    assert "Different beta evidence" in transport.payloads[1]["input"][0]["content"]
    assert "Unique alpha evidence" not in transport.payloads[1]["input"][0]["content"]
    assert result["retrieval"]["counts"] == {"candidates": 2, "passed": 1, "selected": 1}
    assert [candidate["decision"] for candidate in result["retrieval"]["candidates"]] == ["below_threshold", "selected"]
    service.close()


def test_threshold_is_inclusive_and_no_context_skips_generation(indexed):
    root, data = indexed
    alpha, beta = [chunk["chunk_id"] for chunk in read_index(root, data, load_config())]
    service = RagService(root, data, transport=Transport([scores((alpha, 2, "Partial"), (beta, 1, "Topic")), response("Ответ [S1].")]), embedder=Embedder())
    accepted = service.ask("Вопрос", "filter", str(uuid.uuid4()))
    assert accepted["sources"][0]["file"] == "alpha.md"
    assert accepted["sources"][0]["relevance_score"] == 2
    service.close()
    transport = Transport([scores((alpha, 1, "Topic"), (beta, 0, "Unrelated"))])
    service = RagService(root, data, transport=transport, embedder=Embedder())
    absent = service.ask("Вопрос", "filter", str(uuid.uuid4()))
    assert absent["status"] == "no_context" and absent["sources"] == [] and absent["context"] == ""
    assert len(transport.payloads) == 1
    assert absent["output_policy"]["code"] == "not_called"
    service.close()


def test_rewrite_changes_embedding_only_and_keeps_explicit_anchor(indexed):
    root, data = indexed
    alpha, beta = [chunk["chunk_id"] for chunk in read_index(root, data, load_config())]
    question = "Как исправить alpha.md в 2026?"
    rewritten = "alpha.md 2026 исправить графику"
    transport = Transport([response(json.dumps({"query": rewritten})), scores((alpha, 3, "Direct"), (beta, 0, "No")), response("Ответ [S1].")])
    embedder = Embedder()
    service = RagService(root, data, transport=transport, embedder=embedder)
    result = service.ask(question, "rewrite_filter", str(uuid.uuid4()))
    assert result["status"] == "ok", result
    assert embedder.calls == [[rewritten]]
    assert question in transport.payloads[1]["input"][0]["content"]
    assert question in transport.payloads[2]["input"][0]["content"]
    assert rewritten not in transport.payloads[2]["input"][0]["content"]
    assert result["retrieval"]["original_query"] == question
    assert result["retrieval"]["search_query"] == rewritten
    assert result["stages"][1]["prompt"]["input"] == [rewritten]
    assert [stage["kind"] for stage in result["stages"]] == ["rewrite", "query_embedding", "relevance_filter", "generation"]
    assert result["usage"]["input_tokens"] == 65
    assert result["usage"]["output_tokens"] == 24
    assert result["usage"]["total_tokens"] == 89
    assert result["usage"]["reasoning_tokens"] == 6
    assert result["usage"]["cached_input_tokens"] is None
    service.close()


@pytest.mark.parametrize("overrides", [
    {"top_k_before": True}, {"top_k_after": 0}, {"top_k_after": 21},
    {"relevance_threshold": True}, {"relevance_threshold": 4},
    {"max_filter_input_bytes": float("nan")}, {"timeout_seconds": True},
])
def test_invalid_retrieval_config_is_rejected(overrides):
    with pytest.raises(ValueError):
        replace(load_config(), **overrides).validate()


def test_full_filter_payload_limit_prevents_call(indexed):
    root, data = indexed
    transport = Transport()
    service = RagService(root, data, transport=transport, embedder=Embedder(),
                         config=replace(load_config(), max_filter_input_bytes=100))
    result = service.ask("Вопрос", "filter", str(uuid.uuid4()))
    assert result["status"] == "rejected" and result["code"] == "filter_input_too_large"
    assert transport.payloads == []
    refused = result["stages"][-1]
    assert refused["api_called"] is False
    assert refused["model"] == "gpt-6-luna" and refused["service_tier"] == "default"
    assert refused["tariff"] == service.config.tariff["generation"]
    assert refused["input_policy"]["code"] == "filter_input_too_large"
    assert refused["output_policy"]["code"] == "not_called"
    assert isinstance(refused["started_at_utc"], str) and refused["elapsed_seconds"] == 0
    service.close()


@pytest.mark.parametrize("bad", ["missing", "duplicate", "unknown", "bad_score"])
def test_bad_filter_ids_or_score_rejects_without_generation_and_keeps_cost(indexed, bad):
    root, data = indexed
    alpha, beta = [chunk["chunk_id"] for chunk in read_index(root, data, load_config())]
    cases = {
        "missing": [{"chunk_id": alpha, "score": 3, "reason": "yes"}],
        "duplicate": [{"chunk_id": alpha, "score": 3, "reason": "yes"}] * 2,
        "unknown": [{"chunk_id": alpha, "score": 3, "reason": "yes"}, {"chunk_id": "alien", "score": 2, "reason": "maybe"}],
        "bad_score": [{"chunk_id": alpha, "score": True, "reason": "yes"}, {"chunk_id": beta, "score": 2, "reason": "yes"}],
    }
    session = str(uuid.uuid4())
    transport = Transport([response(json.dumps({"scores": cases[bad]}))])
    service = RagService(root, data, transport=transport, embedder=Embedder())
    result = service.ask("Вопрос", "filter", session)
    assert result["status"] == "rejected" and len(transport.payloads) == 1
    assert [stage["kind"] for stage in result["stages"]] == ["query_embedding", "relevance_filter"]
    assert result["cost_usd"] > 0
    service.close()
    reopened = RagService(root, data, transport=Transport(), embedder=Embedder())
    assert reopened.state(session)["requests"][0]["stages"][1]["cost_usd"] is not None
    reopened.close()


def test_rewrite_anchor_rejection_and_unknown_usage_are_durable(indexed):
    root, data = indexed
    session = str(uuid.uuid4())
    transport = Transport([response('{"query":"generic art"}', usage=False)])
    embedder = Embedder()
    service = RagService(root, data, transport=transport, embedder=embedder)
    result = service.ask("Как исправить alpha.md?", "rewrite", session)
    assert result["status"] == "rejected" and embedder.calls == []
    assert result["output_policy"] == {"name": service.config.output_policy, "passed": None, "code": "not_called"}
    assert result["stages"][0]["output_policy"]["code"] == "invalid_rewrite"
    assert result["cost_usd"] is None and result["usage"] is None
    assert service.state(session)["cumulative"]["unknown_calls"] == 1
    service.close()


def test_rejected_filter_keeps_machine_policy_separate_from_unrun_answer_policy(indexed):
    root, data = indexed
    alpha = read_index(root, data, load_config())[0]["chunk_id"]
    service = RagService(root, data, transport=Transport([response(json.dumps({"scores": [
        {"chunk_id": alpha, "score": 3, "reason": "direct"}]}))]), embedder=Embedder())
    session = str(uuid.uuid4())
    result = service.ask("Вопрос", "filter", session)
    assert result["status"] == "rejected" and result["code"] == "invalid_filter_scores"
    assert result["output_policy"] == {"name": service.config.output_policy, "passed": None, "code": "not_called"}
    assert result["stages"][-1]["output_policy"]["code"] == "invalid_filter_scores"
    assert service.state(session)["requests"][0]["output_policy"] == result["output_policy"]
    service.close()


def test_embedding_error_after_rewrite_does_not_claim_answer_policy(indexed):
    class FailedEmbedding(Exception):
        metadata = {"api_called": True, "usage": {"prompt_tokens": 7, "total_tokens": 7}}

    root, data = indexed
    service = RagService(root, data, transport=Transport([response('{"query":"alpha.md"}')]),
                         embedder=Embedder(error=FailedEmbedding()))
    result = service.ask("alpha.md", "rewrite", str(uuid.uuid4()))
    assert result["status"] == "error" and result["code"] == "retrieval_failed"
    assert [stage["kind"] for stage in result["stages"]] == ["rewrite", "query_embedding"]
    assert result["stages"][0]["output_policy"]["passed"] is True
    assert result["stages"][1]["code"] == "embedding_failed"
    assert result["stages"][1]["output_policy"]["passed"] is None
    assert result["stages"][1]["output_policy"]["code"] == "not_checked"
    assert result["output_policy"] == {"name": service.config.output_policy, "passed": None, "code": "not_called"}
    service.close()


def test_transport_preflight_failure_is_not_an_output_policy_rejection(tmp_path):
    session = str(uuid.uuid4())
    service = RagService(tmp_path, tmp_path / "data",
                         transport=Transport([TransportError("not_configured", request_started=False)]),
                         embedder=Embedder())
    result = service.ask("Вопрос", "plain", session)
    assert result["status"] == "error" and result["code"] == "not_configured"
    stage = service.state(session)["requests"][0]["stages"][0]
    assert stage["status"] == "error" and stage["code"] == "not_configured"
    assert stage["api_called"] is False
    assert stage["output_policy"] == {"name": service.config.output_policy, "passed": None, "code": "not_called"}
    assert result["output_policy"] == stage["output_policy"]
    assert service.state(session)["cumulative"]["unknown_calls"] == 0
    service.close()


def test_started_rewrite_transport_failure_is_unchecked_and_preserves_reason(indexed):
    root, data = indexed
    session = str(uuid.uuid4())
    service = RagService(root, data, transport=Transport([TransportError("timeout", request_started=True)]),
                         embedder=Embedder())
    result = service.ask("Вопрос", "rewrite", session)
    assert result["status"] == "error" and result["code"] == "timeout"
    stage = service.state(session)["requests"][0]["stages"][0]
    assert stage["kind"] == "rewrite" and stage["status"] == "error" and stage["code"] == "timeout"
    assert stage["api_called"] is True
    assert stage["output_policy"] == {"name": "rewrite_query_json_anchors_v1", "passed": None, "code": "not_checked"}
    assert result["output_policy"]["code"] == "not_called"
    assert service.state(session)["cumulative"]["unknown_calls"] == 1
    service.close()


def test_retrieval_elapsed_excludes_slow_generation(indexed):
    class SlowGeneration(Transport):
        def create(self, payload, timeout):
            time.sleep(0.06)
            return super().create(payload, timeout)

    root, data = indexed
    service = RagService(root, data, transport=SlowGeneration([response("Ответ [S1].")]), embedder=Embedder())
    result = service.ask("Вопрос", "rag", str(uuid.uuid4()))
    assert result["status"] == "ok"
    assert result["elapsed_seconds"] - result["retrieval"]["elapsed_seconds"] >= 0.05
    service.close()


def test_incomplete_rewrite_keeps_partial_machine_text_and_cost(indexed):
    root, data = indexed
    partial = response('{"query":"unfinished')
    partial["status"] = "incomplete"
    transport = Transport([partial])
    service = RagService(root, data, transport=transport, embedder=Embedder())
    result = service.ask("alpha.md", "rewrite", str(uuid.uuid4()))
    assert result["status"] == "rejected" and result["code"] == "incomplete"
    assert result["stages"][0]["raw_text"] == '{"query":"unfinished'
    assert result["stages"][0]["cost_usd"] is not None
    assert result["cost_usd"] > 0
    service.close()


def test_compare_runs_four_modes_and_stops_after_failure(indexed):
    root, data = indexed
    alpha, beta = [chunk["chunk_id"] for chunk in read_index(root, data, load_config())]
    transport = Transport([response("rag [S1]"), response('{"query":"alpha question"}'), response("rewrite [S1]"),
                           scores((alpha, 3, "yes"), (beta, 0, "no")), response("filter [S1]"),
                           response('{"query":"alpha question"}'), scores((alpha, 3, "yes"), (beta, 0, "no")), response("both [S1]")])
    service = RagService(root, data, transport=transport, embedder=Embedder())
    pair = service.compare("alpha question", str(uuid.uuid4()))
    assert pair["status"] == "ok" and list(pair["results"]) == ["rag", "rewrite", "filter", "rewrite_filter"]
    assert [pair["results"][mode]["status"] for mode in pair["results"]] == ["ok"] * 4
    service.close()
    service = RagService(root, data, transport=Transport([response("bad [S9]")]), embedder=Embedder())
    stopped = service.compare("alpha question", str(uuid.uuid4()))
    assert stopped["status"] == "rejected" and stopped["results"]["rag"]["status"] == "rejected"
    assert all(stopped["results"][mode] is None for mode in ("rewrite", "filter", "rewrite_filter"))
    service.close()


def test_compare_continues_after_filter_has_no_context(indexed):
    root, data = indexed
    alpha, beta = [chunk["chunk_id"] for chunk in read_index(root, data, load_config())]
    low = scores((alpha, 1, "topic"), (beta, 0, "unrelated"))
    high = scores((alpha, 3, "direct"), (beta, 0, "unrelated"))
    transport = Transport([response("rag [S1]"), response('{"query":"alpha question"}'), response("rewrite [S1]"),
                           low, response('{"query":"alpha question"}'), high, response("combined [S1]")])
    service = RagService(root, data, transport=transport, embedder=Embedder())
    pair = service.compare("alpha question", str(uuid.uuid4()))
    assert pair["status"] == "ok"
    assert pair["results"]["filter"]["status"] == "no_context"
    assert pair["results"]["rewrite_filter"]["status"] == "ok"
    assert len(transport.payloads) == 7
    service.close()

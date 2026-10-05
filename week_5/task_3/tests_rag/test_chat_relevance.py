"""Day 23 relevance behavior at the production chat boundary."""

import hashlib
import json

import pytest

from agent import load_config
from rag.chat import RAG_MODES, RagChatAgent, RagProfileWorkspace
from rag.chat_store import RagSQLiteStore
from test_chat_rag import Fake, response


class Embedder:
    def __init__(self):
        self.calls = []

    def embed(self, texts):
        self.calls.append(texts)
        return {"model": "text-embedding-3-small", "vectors": [[1.0] + [0.0] * 1535],
                "usage": {"prompt_tokens": 5, "total_tokens": 5}}


class Transport:
    def __init__(self, *, rewrite=None, scores=None, filter_text=None,
                 answer="Supported [S1].", usage=True):
        self.calls = []
        self.rewrite = rewrite
        self.scores = scores
        self.filter_text = filter_text
        self.answer = answer
        self.usage = usage

    def create(self, payload, timeout):
        self.calls.append(payload)
        name = payload.get("text", {}).get("format", {}).get("name")
        if name == "rewrite":
            result = response(json.dumps({"query": self.rewrite}, ensure_ascii=False))
        elif name == "filter":
            result = response(self.filter_text if self.filter_text is not None else
                json.dumps({"scores": self.scores}, ensure_ascii=False))
        else:
            result = response(self.answer)
        if not self.usage:
            result.pop("usage")
        return result


@pytest.fixture
def agent_case(tmp_path, monkeypatch):
    from rag import chat

    def chunk(number, similarity):
        vector = [similarity, (1.0 - similarity * similarity) ** 0.5] + [0.0] * 1534
        return {"chunk_id": f"c{number:02}", "file": f"part{number}.md", "source": "local",
                "title": f"Part {number}", "section": ["Evidence"], "line_start": 1,
                "line_end": 1, "start": number * 100, "end": number * 100 + 50,
                "document_hash": str(number), "text": f"Evidence {number}", "embedding": vector}

    chunks = [chunk(0, 1.0), chunk(1, .8)]
    monkeypatch.setattr(chat, "read_index", lambda *_: chunks)
    made = []

    def make(transport):
        embedder = Embedder()
        agent = RagChatAgent(load_config(), transport, RagSQLiteStore(tmp_path / f"chat{len(made)}.sqlite3"),
                             index_data_dir=tmp_path, embedder=embedder)
        made.append(agent)
        return agent, embedder

    yield make, chunks
    for agent in made:
        agent.close()


def _requests(agent):
    return agent.state()["requests"]


def test_filter_rank_trap_keeps_full_candidate_trace_and_original_question(agent_case):
    make, chunks = agent_case
    transport = Transport(scores={"c00": {"score": 1, "reason": "Topic only"},
                                  "c01": {"score": 3, "reason": "Direct evidence"}})
    agent, embedder = make(transport)

    result = agent.run("What is in part1.md?", rag_mode="filter")

    assert result.status == "ok"
    assert embedder.calls == [["What is in part1.md?"]]
    assert len(transport.calls) == 2
    assert transport.calls[0]["text"]["format"]["name"] == "filter"
    assert "What is in part1.md?" in transport.calls[0]["input"][0]["content"]
    rag = next(r["metadata"]["rag"] for r in _requests(agent) if r["metadata"]["kind"] == "answer")
    assert rag["mode"] == "filter"
    assert [c["chunk_id"] for c in rag["candidates"]] == ["c00", "c01"]
    assert [c["decision"] for c in rag["candidates"]] == ["threshold", "selected"]
    assert rag["candidates"][1]["text"] == chunks[1]["text"]
    assert [s["chunk_id"] for s in rag["sources"]] == ["c01"]
    assert "Evidence 1" in transport.calls[1]["input"][0]["content"]
    assert rag["filter_request_id"] in [r["id"] for r in _requests(agent)]
    child = next(r for r in _requests(agent) if r["metadata"]["kind"] == "relevance_filter")
    assert child["metadata"]["provider_usage"]["total_tokens"] == 110
    assert child["metadata"]["actual_model"] == "gpt-6-luna"
    assert child["metadata"]["elapsed_seconds"] >= 0
    assert child["output_policy"]["status"] == "accepted"


def test_rewrite_only_changes_embedding_query_and_keeps_base_payload(agent_case):
    make, _ = agent_case
    transport = Transport(rewrite="part1.md evidence", answer="See [S1].")
    agent, embedder = make(transport)

    result = agent.run("Explain part1.md", rag_mode="rewrite", use_rag=False)

    assert result.status == "ok"
    assert embedder.calls == [["part1.md evidence"]]
    rag = next(r["metadata"]["rag"] for r in _requests(agent) if r["metadata"]["kind"] == "answer")
    assert rag["original_query"] == "Explain part1.md"
    assert rag["search_query"] == "part1.md evidence"
    assert rag["base_generation_payload"]["input"][-1]["content"] == "Explain part1.md"
    paid = dict(transport.calls[-1])
    assert paid["max_output_tokens"] == 1600
    assert paid["input"][0]["content"].startswith("Локальные справочные фрагменты")
    paid["input"] = paid["input"][1:]
    paid["instructions"] = paid["instructions"].split("\nЛокальные фрагменты в input", 1)[0]
    assert paid == rag["base_generation_payload"]
    assert hashlib.sha256(json.dumps(paid, sort_keys=True, ensure_ascii=False,
        separators=(",", ":")).encode("utf-8")).hexdigest() == rag["base_generation_sha256"]
    assert [r["metadata"]["kind"] for r in _requests(agent) if r["metadata"]["kind"] in
            {"query_rewrite", "query_embedding"}] == ["query_rewrite", "query_embedding"]


def test_invalid_rewrite_anchor_stops_all_dependent_calls_and_keeps_spend(agent_case):
    make, _ = agent_case
    transport = Transport(rewrite="generic evidence")
    agent, embedder = make(transport)

    result = agent.run("Explain part1.md", rag_mode="rewrite_filter")

    assert result.status == "rejected" and result.code == "invalid_rewrite"
    assert embedder.calls == [] and len(transport.calls) == 1
    state = agent.state()
    parent = next(r for r in state["requests"] if r["metadata"]["kind"] == "answer")
    child = next(r for r in state["requests"] if r["metadata"]["kind"] == "query_rewrite")
    assert parent["usage_status"] == "not_requested"
    assert child["usage"]["total_tokens"] == 110 and child["cost_usd"] is not None
    assert not any(m["role"] == "assistant" for m in state["messages"])


def test_invalid_filter_ids_stop_generation_and_unknown_cost_survives_reload(agent_case, tmp_path):
    make, _ = agent_case
    transport = Transport(scores={"c00": {"score": 3, "reason": "Relevant"},
                                  "unknown": {"score": 3, "reason": "Wrong ID"}}, usage=False)
    agent, _ = make(transport)

    result = agent.run("Find evidence", rag_mode="filter")

    assert result.status == "rejected" and result.code == "invalid_filter_scores"
    assert len(transport.calls) == 1
    state = agent.state()
    parent = next(r for r in state["requests"] if r["metadata"]["kind"] == "answer")
    assert parent["usage_status"] == "not_requested"
    assert state["summary"]["cost_complete"] is False
    assert state["summary"]["unknown_cost_requests"] == 1
    agent.close()
    reopened = RagSQLiteStore(tmp_path / "chat0.sqlite3")
    restored = reopened.state()
    assert restored["requests"] == state["requests"]
    assert restored["summary"]["unknown_cost_requests"] == 1
    reopened.close()


@pytest.mark.parametrize("filter_text", [
    '{"scores":{"c00":{"score":3,"reason":"Direct"}}}',
    '{"scores":{"c00":{"score":3,"reason":"Direct"},'
    '"c00":{"score":1,"reason":"Duplicate"},'
    '"c01":{"score":2,"reason":"Partial"}}}',
    '{"scores":[{"chunk_id":"c00","score":3,"reason":"Direct"},'
    '{"chunk_id":"c01","score":2,"reason":"Partial"}]}',
])
def test_incomplete_duplicate_or_legacy_filter_output_never_generates(agent_case, filter_text):
    make, _ = agent_case
    transport = Transport(filter_text=filter_text)
    agent, _ = make(transport)

    result = agent.run("Find evidence", rag_mode="filter")

    assert result.status == "rejected" and result.code == "invalid_filter_scores"
    assert len(transport.calls) == 1
    state = agent.state()
    parent = next(r for r in state["requests"] if r["metadata"]["kind"] == "answer")
    child = next(r for r in state["requests"] if r["metadata"]["kind"] == "relevance_filter")
    assert parent["usage_status"] == "not_requested"
    assert child["cost_usd"] is not None
    assert not any(m["role"] == "assistant" for m in state["messages"])


def test_no_context_is_terminal_without_generation(agent_case):
    make, _ = agent_case
    transport = Transport(scores={"c00": {"score": 1, "reason": "Topic"},
                                  "c01": {"score": 0, "reason": "None"}})
    agent, _ = make(transport)

    result = agent.run("Find evidence", rag_mode="filter")

    assert result.status == "no_context" and result.code == "no_context"
    assert len(transport.calls) == 1
    state = agent.state()
    parent = next(r for r in state["requests"] if r["metadata"]["kind"] == "answer")
    rag = parent["metadata"]["rag"]
    assert parent["usage_status"] == "not_requested"
    assert rag["sources"] == [] and rag["context"] == ""
    assert rag["base_generation_payload"]["input"][-1]["content"] == "Find evidence"
    assert not any(m["role"] == "assistant" for m in state["messages"])


def test_explicit_plain_overrides_boolean_and_validates_modes(agent_case):
    make, _ = agent_case
    transport = Transport(answer="Ordinary answer.")
    agent, embedder = make(transport)
    assert RAG_MODES == ("rag", "rewrite", "filter", "rewrite_filter")
    result = agent.run("Hello", use_rag=True, rag_mode="plain")
    assert result.status == "ok" and embedder.calls == []
    assert "text" not in transport.calls[0]
    rag = next(r["metadata"]["rag"] for r in _requests(agent) if r["metadata"]["kind"] == "answer")
    assert rag["mode"] == "plain"
    with pytest.raises(ValueError):
        agent.run("Hello", rag_mode="unknown")


def test_filter_caps_default_candidates_at_ten_and_accepts_threshold_two(agent_case):
    make, chunks = agent_case
    for number in range(2, 21):
        extra = dict(chunks[1], chunk_id=f"c{number:02}", file=f"part{number}.md",
                     text=f"Evidence {number}", start=number * 100, end=number * 100 + 50,
                     embedding=[.7 - number * .001, (.51 + number * .001) ** .5] + [0.0] * 1534)
        chunks.append(extra)
    scores = {f"c{number:02}": {"score": 2 if number == 9 else 1,
               "reason": "Some evidence" if number == 9 else "Topic only"}
              for number in range(10)}
    transport = Transport(scores=scores)
    agent, _ = make(transport)

    result = agent.run("Find evidence", rag_mode="filter")

    assert result.status == "ok"
    score_schema = transport.calls[0]["text"]["format"]["schema"]["properties"]["scores"]
    assert score_schema["type"] == "object"
    assert score_schema["required"] == [f"c{number:02}" for number in range(10)]
    assert set(score_schema["properties"]) == set(score_schema["required"])
    assert score_schema["additionalProperties"] is False
    assert set(score_schema["properties"]["c09"]["required"]) == {"score", "reason"}
    assert score_schema["properties"]["c09"]["additionalProperties"] is False
    rag = next(r["metadata"]["rag"] for r in _requests(agent) if r["metadata"]["kind"] == "answer")
    assert rag["counts"] == {"candidates": 10, "passed": 1, "selected": 1}
    assert len(rag["candidates"]) == 10
    assert all(c["chunk_id"] != "c10" for c in rag["candidates"])
    assert [s["chunk_id"] for s in rag["sources"]] == ["c09"]


def test_filtered_context_is_reused_across_mcp_steps(agent_case):
    make, _ = agent_case

    class ToolTransport(Transport):
        def create(self, payload, timeout):
            name = payload.get("text", {}).get("format", {}).get("name")
            if name == "filter":
                return super().create(payload, timeout)
            self.calls.append(payload)
            if sum("text" not in call for call in self.calls) == 1:
                tool = response("unused")
                tool["output"] = [{"type": "function_call", "name": "lookup_wikipedia",
                    "call_id": "call-1", "arguments": json.dumps({"query": "Alpha",
                        "language": "en", "limit": 1})}]
                return tool
            return response("Supported [S1].")

    class References:
        def fetch(self, provider, query, language, community, *, limit):
            return {"provider": provider, "query": query, "sources": [{"title": "Alpha",
                "excerpt": "Reference", "url": "https://en.wikipedia.org/wiki/Alpha"}],
                "metadata": {}}

    transport = ToolTransport(scores={"c00": {"score": 2, "reason": "Partial"},
                                   "c01": {"score": 1, "reason": "Topic"}})
    agent, embedder = make(transport)
    agent._retrieval_client = References()

    result = agent.run("Find evidence", rag_mode="filter")

    assert result.status == "ok", result
    assert len(embedder.calls) == 1
    assert len([c for c in transport.calls if c.get("text", {}).get("format", {}).get("name") == "filter"]) == 1
    answers = [c for c in transport.calls if c.get("text", {}).get("format", {}).get("name") != "filter"]
    assert len(answers) == 2
    assert all("Evidence 0" in c["input"][0]["content"] for c in answers)
    assert len([r for r in _requests(agent) if r["metadata"]["kind"] == "relevance_filter"]) == 1


def test_rag_input_bound_stops_before_memory_extraction(tmp_path):
    transport = Transport()
    workspace = RagProfileWorkspace(tmp_path, transport)

    result = workspace.agent().run("x" * 4001, rag_mode="rewrite_filter")

    assert result.status == "rejected" and result.code == "input_too_long"
    assert transport.calls == []
    assert not any(r["metadata"]["kind"] == "extraction" for r in workspace.state()["requests"])
    workspace.close()


def test_guarded_task_routes_filter_through_same_parent_once(agent_case, tmp_path):
    transport = Fake([response(json.dumps({"scores": {
        "c00": {"score": 2, "reason": "Partial"},
        "c01": {"score": 1, "reason": "Topic"}}})),
        response("Supported [S1]."), response('{"operations": []}')])
    workspace = RagProfileWorkspace(tmp_path / "guarded", transport)
    agent = workspace.agent()
    embedder = Embedder()
    agent._rag_embedder = embedder

    result = agent.run("Find evidence", rag_mode="filter")

    assert result.status == "ok", result
    assert embedder.calls == [["Find evidence"]]
    state = workspace.state()
    parent = next(r for r in state["requests"] if r["metadata"]["kind"] == "answer")
    assert parent["metadata"]["rag"]["mode"] == "filter"
    assert len([r for r in state["requests"] if r["metadata"]["kind"] == "relevance_filter"]) == 1
    assert len([m for m in state["messages"] if m["role"] == "assistant"]) == 1
    machine = transport.calls[0]["text"]["format"]
    assert machine["name"] == "filter"
    generation = transport.calls[1]["text"]["format"]
    assert generation["type"] == "json_schema" and generation["strict"] is True
    schema = generation["schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"answer", "event", "evidence", "goal",
        "current_step", "expected_action", "notes", "plan"}
    assert set(schema["properties"]) == set(schema["required"])
    assert "plan_ready" in schema["properties"]["event"]["enum"]
    assert schema["properties"]["plan"]["items"]["additionalProperties"] is False
    assert set(schema["properties"]["plan"]["items"]["required"]) == {"action", "criterion"}
    assert parent["metadata"]["rag"]["base_generation_payload"]["text"]["format"] == generation
    workspace.close()


def test_plain_guarded_mcp_steps_keep_same_task_schema(tmp_path):
    class ToolThenAnswer(Fake):
        def __init__(self):
            super().__init__([response("Direct answer."), response('{"operations": []}')])
            self.first = True

        def create(self, payload, timeout):
            if self.first and "TASK_RESPONSE_JSON" in payload["instructions"]:
                self.first = False
                self.calls.append(payload)
                tool = response("unused")
                tool["output"] = [{"type": "function_call", "name": "lookup_wikipedia",
                    "call_id": "call-plain", "arguments": json.dumps({"query": "Alpha",
                        "language": "en", "limit": 1})}]
                return tool
            return super().create(payload, timeout)

    class References:
        def fetch(self, provider, query, language, community, *, limit):
            return {"provider": provider, "query": query, "sources": [{"title": "Alpha",
                "excerpt": "Reference", "url": "https://en.wikipedia.org/wiki/Alpha"}],
                "metadata": {}}

    transport = ToolThenAnswer()
    workspace = RagProfileWorkspace(tmp_path, transport)
    agent = workspace.agent()
    agent._retrieval_client = References()

    result = agent.run("Explain alpha", rag_mode="plain")

    assert result.status == "ok", result
    generated = [call for call in transport.calls if "TASK_RESPONSE_JSON" in call["instructions"]]
    assert len(generated) == 2
    assert generated[0]["text"]["format"] == generated[1]["text"]["format"]
    assert generated[0]["text"]["format"]["name"] == "task_response"
    state = workspace.state()
    parent = next(r for r in state["requests"] if r["metadata"]["kind"] == "answer")
    assert parent["metadata"]["rag"]["base_generation_payload"]["text"]["format"] == generated[0]["text"]["format"]
    assert any(r["metadata"]["kind"] == "mcp_step" for r in state["requests"])
    workspace.close()

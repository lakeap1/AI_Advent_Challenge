"""Retrieval ordering, persistence, and separation from agent state calls."""

import json

from agent import Agent, SQLiteStore, load_config
from agent.retrieval import RetrievalError, validate_request
from profiles import ProfileWorkspace
from test_strategies import Fake, response


QUERY = "Cycles glass material"


class Sources:
    def __init__(self, fail=None, empty=False):
        self.calls = []
        self.fail, self.empty = fail, empty

    def fetch(self, provider, query, language, community):
        self.calls.append((provider, query, language, community))
        if provider == self.fail:
            raise RetrievalError("service down")
        sources = [] if self.empty else [dict(title="Example reference", excerpt="EXTERNAL_SENTINEL",
            url=("https://en.wikipedia.org/wiki/Rendering" if provider == "wikipedia" else
                 "https://blender.stackexchange.com/questions/123/example"))]
        return dict(provider=provider, query=query, sources=sources, metadata={"page": 1})


def choice(sources=None):
    return dict(sources=sources or ["wikipedia", "stackexchange"], query=QUERY,
                language="en", community="blender")


def test_retrieval_persists_and_only_main_answer_sees_reference(tmp_path):
    path = tmp_path / "chat.sqlite3"
    transport = Fake([response("Answer with source")])
    source = Sources()
    agent = Agent(load_config(), transport, SQLiteStore(path), retrieval_client=source)
    result = agent.run("How does glass work?", retrieval=choice())
    assert result.status == "ok"
    assert result.retrieval["status"] == "ok"
    assert [r[0] for r in source.calls] == ["wikipedia", "stackexchange"]
    assert len(transport.calls) == 1
    assert "EXTERNAL_SENTINEL" in json.dumps(transport.calls[0]["input"])
    assert "EXTERNAL_SENTINEL" not in json.dumps(agent.state()["messages"])
    assert [r["status"] for r in agent.state()["retrievals"]] == ["ok", "ok"]
    assert agent.state()["summary"]["api_requests"] == 1
    agent.close()
    restored = Agent(load_config(), Fake(), SQLiteStore(path))
    assert [r["provider"] for r in restored.state()["retrievals"]] == ["wikipedia", "stackexchange"]
    assert restored.state()["retrievals"][0]["request_id"] == result.request_id
    restored.close()


def test_wikipedia_query_override_dispatch_persistence_and_context(tmp_path):
    transport = Fake([response("Answer")])
    source = Sources()
    agent = Agent(load_config(), transport, SQLiteStore(tmp_path / "chat.sqlite3"),
                  retrieval_client=source)
    selection = {**choice(), "wikipedia_query": "normal mapping"}
    result = agent.run("Why are my seams visible?", retrieval=selection)
    assert result.status == "ok"
    assert [(call[0], call[1]) for call in source.calls] == [
        ("wikipedia", "normal mapping"), ("stackexchange", QUERY)]
    assert [row["query"] for row in result.retrieval["records"]] == ["normal mapping", QUERY]
    assert [row["query"] for row in agent.state()["retrievals"]] == ["normal mapping", QUERY]
    reference = transport.calls[0]["input"][-2]["content"]
    assert '"query": "normal mapping"' in reference
    assert '"query": "Cycles glass material"' in reference
    assert transport.calls[0]["input"][-1]["content"] == "Why are my seams visible?"
    agent.close()


def test_second_source_failure_keeps_first_and_stops_main(tmp_path):
    source = Sources(fail="stackexchange")
    transport = Fake()
    agent = Agent(load_config(), transport, SQLiteStore(tmp_path / "chat.sqlite3"), retrieval_client=source)
    result = agent.run("Material?", retrieval=choice())
    assert (result.status, result.code) == ("error", "retrieval_failed")
    assert result.usage_status == "not_requested"
    assert transport.calls == []
    assert [r["status"] for r in agent.state()["retrievals"]] == ["ok", "error"]
    assert agent.state()["summary"]["api_requests"] == 0
    agent.close()


def test_empty_and_disabled_are_distinct(tmp_path):
    transport = Fake([response("Without search"), response("No references")])
    source = Sources(empty=True)
    agent = Agent(load_config(), transport, SQLiteStore(tmp_path / "chat.sqlite3"), retrieval_client=source)
    disabled = agent.run("First")
    empty = agent.run("Second", retrieval=choice(["wikipedia"]))
    assert disabled.retrieval is None and empty.retrieval["status"] == "empty"
    assert len(source.calls) == 1 and agent.state()["retrievals"][0]["status"] == "empty"
    assert "REMOTE_REFERENCES" not in json.dumps(transport.calls[0]["input"])
    assert "REMOTE_REFERENCES" in json.dumps(transport.calls[1]["input"])
    agent.close()


def test_output_rejection_after_search_keeps_paid_usage(tmp_path):
    transport = Fake([response("")])
    agent = Agent(load_config(), transport, SQLiteStore(tmp_path / "chat.sqlite3"),
                  retrieval_client=Sources())
    result = agent.run("Question", retrieval=choice(["wikipedia"]))
    assert result.code == "empty_output" and result.status == "error"
    assert result.retrieval["status"] == "ok"
    state = agent.state()
    assert state["summary"]["api_requests"] == 1
    assert state["summary"]["cost_complete"]
    assert state["token_accounting"]["known_total_tokens"] == 110
    assert not any(message["role"] == "assistant" for message in state["messages"])
    agent.close()


def test_invalid_selection_and_input_do_not_call_mcp(tmp_path):
    source = Sources()
    transport = Fake()
    agent = Agent(load_config(), transport, SQLiteStore(tmp_path / "chat.sqlite3"), retrieval_client=source)
    assert agent.run(" ", retrieval=choice()).code == "input_invalid"
    assert agent.run("Question", retrieval={"sources": ["other"], "query": QUERY}).code == "retrieval_invalid"
    assert source.calls == transport.calls == []
    assert agent.state()["retrievals"] == []
    agent.close()


def test_invariant_gate_precedes_retrieval_and_paid_gate_survives_failure(tmp_path):
    checks = response(json.dumps({"checks": [{"id": "R1", "violated": False}]}))
    transport = Fake([checks])
    ws = ProfileWorkspace(tmp_path, transport)
    ws.memory.set_invariants(1, 0, ["Use Cycles."])
    source = Sources(fail="wikipedia")
    ws.agent()._retrieval_client = source
    result = ws.agent().run("How to improve glass?", retrieval=choice(["wikipedia"]))
    assert result.code == "retrieval_failed" and result.retrieval["status"] == "error"
    assert len(transport.calls) == 1 and len(source.calls) == 1
    state = ws.state()
    assert state["summary"]["api_requests"] == 1
    assert state["token_accounting"]["known_total_tokens"] == 110
    assert state["summary"]["cost_complete"]
    assert state["retrievals"][0]["request_id"] == result.request_id
    ws.close()


def test_paused_and_invariant_rejected_do_not_retrieve(tmp_path):
    transport = Fake([response(json.dumps({"checks": [{"id": "R1", "violated": True}]}))])
    ws = ProfileWorkspace(tmp_path, transport)
    ws.memory.set_invariants(1, 0, ["Use Cycles."])
    source = Sources()
    ws.agent()._retrieval_client = source
    assert ws.agent().run("Use another render", retrieval=choice()).code == "invariant_input_conflict"
    assert source.calls == []
    ws.memory.task_action(1, {"action": "pause"})
    assert ws.agent().run("Question", retrieval=choice()).code == "task_paused"
    assert source.calls == []
    ws.close()


def test_reference_not_in_invariant_or_extraction_calls(tmp_path):
    checks = response(json.dumps({"checks": [{"id": "R1", "violated": False}]}))
    transport = Fake([checks, response("Use a broad light."), checks,
                      response('{"operations": []}')])
    ws = ProfileWorkspace(tmp_path, transport)
    ws.memory.set_invariants(1, 0, ["Use Cycles."])
    ws.agent()._retrieval_client = Sources()
    result = ws.agent().run("How to light glass?", retrieval=choice(["wikipedia"]))
    assert result.status == "ok"
    assert len(transport.calls) == 4
    assert all("EXTERNAL_SENTINEL" not in json.dumps(transport.calls[i]) for i in (0, 2, 3))
    assert "EXTERNAL_SENTINEL" in json.dumps(transport.calls[1])
    ws.close()


def test_validation_bounds_and_url_rejection():
    from agent.retrieval import normalize_result
    import pytest
    with pytest.raises(ValueError):
        validate_request({"sources": ["wikipedia"], "query": "x" * 257})
    with pytest.raises(ValueError):
        validate_request({**choice(), "wikipedia_query": "x" * 257})
    with pytest.raises(ValueError):
        validate_request({**choice(), "wikipedia_query": 12})
    assert "wikipedia_query" not in validate_request({**choice(), "wikipedia_query": " "})
    with pytest.raises(RetrievalError):
        normalize_result(dict(provider="wikipedia", query=QUERY, sources=[dict(
            title="Bad", url="javascript:alert(1)", excerpt="bad")]), "wikipedia", QUERY)

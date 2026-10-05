"""MCP outcomes and gates at the public agent boundary."""

import json
import pytest

from agent import Agent, SQLiteStore, load_config
from agent.retrieval import RetrievalError, normalize_result
from profiles import ProfileWorkspace
from test_automatic_mcp import Sources, tool_reply
from test_strategies import Fake, response


def make_agent(tmp_path, replies, source=None):
    transport = Fake(replies)
    agent = Agent(load_config(), transport, SQLiteStore(tmp_path / "chat.sqlite3"), retrieval_client=source or Sources())
    return agent, transport


def two_calls():
    first = tool_reply("lookup_wikipedia", {"query": "normal maps", "language": "en", "limit": 2})
    second = tool_reply("search_stackexchange", {"query": "normal seam", "community": "blender", "limit": 1}, "call_2")
    first["output"].append(second["output"][1])
    return first


def test_two_model_selected_sources_survive_reload(tmp_path):
    source = Sources()
    agent, transport = make_agent(tmp_path, [two_calls(), response("Use consistent tangents.")], source)
    result = agent.run("Why does my normal map have a seam?")
    assert result.status == "ok"
    assert [call[0] for call in source.calls] == ["wikipedia", "stackexchange"]
    assert [call[4] for call in source.calls] == [2, 1]
    continuation = transport.calls[1]["input"]
    assert [item["call_id"] for item in continuation if item.get("type") == "function_call_output"] == ["call_1", "call_2"]
    assert "EXTERNAL_SENTINEL" not in json.dumps(agent.state()["messages"])
    assert agent.state()["summary"]["api_requests"] == 2
    agent.close()
    restored = Agent(load_config(), Fake(), SQLiteStore(tmp_path / "chat.sqlite3"))
    assert [row["provider"] for row in restored.state()["retrievals"]] == ["wikipedia", "stackexchange"]
    assert all(row["request_id"] == result.request_id for row in restored.state()["retrievals"])
    restored.close()


def test_second_source_failure_keeps_first_and_paid_selection(tmp_path):
    source = Sources(fail="stackexchange")
    agent, transport = make_agent(tmp_path, [two_calls()], source)
    result = agent.run("Glass shader problem")
    assert (result.status, result.code) == ("error", "retrieval_failed")
    assert [r["status"] for r in agent.state()["retrievals"]] == ["ok", "error"]
    assert len(transport.calls) == 1
    assert agent.state()["summary"]["api_requests"] == 1
    assert agent.state()["token_accounting"]["known_total_tokens"] == 110
    agent.close()


def test_empty_search_and_direct_answer_are_distinct(tmp_path):
    source = Sources(empty=True)
    agent, transport = make_agent(tmp_path, [response("Hello"),
        tool_reply("lookup_wikipedia", {"query": "rare term", "language": "en", "limit": 1}),
        response("Search found no material.")], source)
    direct = agent.run("Hello")
    empty = agent.run("Find rare term")
    assert direct.retrieval is None and empty.retrieval["status"] == "empty"
    assert len(source.calls) == 1
    assert '"empty": true' in transport.calls[2]["input"][-1]["output"]
    agent.close()


def test_output_rejection_after_search_keeps_both_model_steps(tmp_path):
    agent, _ = make_agent(tmp_path, [
        tool_reply("lookup_wikipedia", {"query": "glass", "language": "en", "limit": 1}), response("")])
    result = agent.run("Explain glass")
    assert result.code == "empty_output" and result.retrieval["status"] == "ok"
    assert agent.state()["summary"]["api_requests"] == 2
    assert agent.state()["token_accounting"]["known_total_tokens"] == 220
    assert not any(m["role"] == "assistant" for m in agent.state()["messages"])
    agent.close()


def test_input_and_pause_stop_before_model_or_mcp(tmp_path):
    source = Sources()
    agent, transport = make_agent(tmp_path, [], source)
    assert agent.run("  ").code == "input_invalid"
    assert transport.calls == source.calls == []
    agent.close()
    ws = ProfileWorkspace(tmp_path / "profile", Fake())
    ws.agent()._retrieval_client = source
    ws.memory.task_action(1, {"action": "pause"})
    assert ws.agent().run("Find references").code == "task_paused"
    assert source.calls == []
    ws.close()


def test_invariant_gate_precedes_selection_and_external_text_stays_out_of_gates(tmp_path):
    checks = response(json.dumps({"checks": [{"id": "R1", "violated": False}]}))
    transport = Fake([checks, tool_reply("lookup_wikipedia", {"query": "glass", "language": "en", "limit": 1}),
                      response("Use a broad light."), checks, response('{"operations": []}')])
    ws = ProfileWorkspace(tmp_path, transport)
    ws.memory.set_invariants(1, 0, ["Use Cycles."])
    ws.agent()._retrieval_client = Sources()
    result = ws.agent().run("How to light glass?")
    assert result.status == "ok"
    assert len(transport.calls) == 5
    assert all("EXTERNAL_SENTINEL" not in json.dumps(transport.calls[i]) for i in (0, 3, 4))
    assert "EXTERNAL_SENTINEL" in json.dumps(transport.calls[2])
    ws.close()


def test_invariant_rejection_never_reaches_tool_choice(tmp_path):
    transport = Fake([response(json.dumps({"checks": [{"id": "R1", "violated": True}]}))])
    ws = ProfileWorkspace(tmp_path, transport)
    ws.memory.set_invariants(1, 0, ["Use Cycles."])
    source = Sources()
    ws.agent()._retrieval_client = source
    assert ws.agent().run("Use another render").code == "invariant_input_conflict"
    assert source.calls == [] and len(transport.calls) == 1
    ws.close()


def test_invalid_reference_url_rejected():
    with pytest.raises(RetrievalError):
        normalize_result(dict(provider="wikipedia", query="glass", sources=[dict(
            title="Bad", url="javascript:alert(1)", excerpt="bad")]), "wikipedia", "glass")

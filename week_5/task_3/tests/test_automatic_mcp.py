"""Observable model-selected retrieval at the Agent.run boundary."""

import json
import pytest

from agent import Agent, SQLiteStore, load_config
from test_strategies import Fake, response


class Sources:
    def __init__(self, fail=None, empty=False):
        self.calls = []
        self.fail, self.empty = fail, empty

    def fetch(self, provider, query, language, community, *, limit=3):
        self.calls.append((provider, query, language, community, limit))
        if provider == self.fail:
            raise RuntimeError("MCP unavailable")
        sources = [] if self.empty else [dict(title="Example reference", excerpt="EXTERNAL_SENTINEL",
            url=("https://en.wikipedia.org/wiki/Rendering" if provider == "wikipedia" else
                 "https://blender.stackexchange.com/questions/123/example"))]
        return dict(provider=provider, query=query, sources=sources, metadata={"page": 1})


def tool_reply(name, arguments, call_id="call_1"):
    result = response()
    result["output"] = [
        {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "ciphertext"},
        {"type": "function_call", "id": "fc_1", "call_id": call_id, "name": name,
         "arguments": json.dumps(arguments)},
    ]
    return result


def test_model_selected_source_is_returned_to_model_and_persisted(tmp_path):
    transport = Fake([
        tool_reply("lookup_wikipedia", {"query": "normal mapping", "language": "en", "limit": 2}),
        response("Normal maps change surface normals. https://en.wikipedia.org/wiki/Rendering"),
    ])
    source = Sources()
    agent = Agent(load_config(), transport, SQLiteStore(tmp_path / "chat.sqlite3"), retrieval_client=source)
    result = agent.run("Why do normal maps affect lighting?")
    assert result.status == "ok"
    assert result.retrieval["records"][0]["query"] == "normal mapping"
    assert source.calls[0][:2] == ("wikipedia", "normal mapping")
    followup = transport.calls[1]["input"]
    assert followup[-1]["call_id"] == "call_1"
    assert "EXTERNAL_SENTINEL" in followup[-1]["output"]
    assert followup[-3]["encrypted_content"] == "ciphertext"
    assert agent.state()["summary"]["api_requests"] == 2
    assert agent.state()["token_accounting"]["known_total_tokens"] == 220
    assert agent.state()["retrievals"][0]["request_id"] == result.request_id
    agent.close()


def test_direct_answer_never_connects_to_mcp(tmp_path):
    transport = Fake([response("Explain the material first.")])
    source = Sources(fail="wikipedia")
    agent = Agent(load_config(), transport, SQLiteStore(tmp_path / "chat.sqlite3"), retrieval_client=source)
    result = agent.run("Help me describe my material")
    assert result.status == "ok" and result.retrieval is None
    assert source.calls == []
    assert transport.calls[0]["tool_choice"] == "auto"
    assert agent.state()["summary"]["api_requests"] == 1
    agent.close()


def test_unknown_tool_stops_before_mcp_and_keeps_paid_step(tmp_path):
    transport = Fake([tool_reply("delete_everything", {"query": "x"})])
    source = Sources()
    agent = Agent(load_config(), transport, SQLiteStore(tmp_path / "chat.sqlite3"), retrieval_client=source)
    result = agent.run("Find a reference")
    assert result.status == "error" and result.code == "invalid_tool_call"
    assert source.calls == []
    assert agent.state()["summary"]["api_requests"] == 1
    assert agent.state()["token_accounting"]["known_total_tokens"] == 110
    agent.close()


@pytest.mark.parametrize("bad", [
    {"query": "glass", "language": "en", "limit": True},
    {"query": "glass", "language": "en", "limit": 4},
    {"query": "glass", "language": "fr", "limit": 1},
    {"query": "glass", "language": "en", "limit": 1, "extra": "x"},
    [],
])
def test_invalid_tool_arguments_never_reach_mcp(tmp_path, bad):
    source = Sources()
    agent = Agent(load_config(), Fake([tool_reply("lookup_wikipedia", bad)]),
                  SQLiteStore(tmp_path / "chat.sqlite3"), retrieval_client=source)
    result = agent.run("Find glass")
    assert result.code == "invalid_tool_call"
    assert source.calls == []
    assert agent.state()["token_accounting"]["known_total_tokens"] == 110
    agent.close()


@pytest.mark.parametrize("change", [
    lambda reply: reply.update(status="incomplete"),
    lambda reply: reply.update(error={"message": "failed"}),
    lambda reply: reply["output"].append({"type": "message", "role": "assistant", "status": "completed",
        "content": [{"type": "refusal", "refusal": "No"}]}),
    lambda reply: reply["output"][1].update(status="incomplete"),
])
def test_refusal_incomplete_or_provider_error_cannot_trigger_mcp(tmp_path, change):
    selection = tool_reply("lookup_wikipedia", {"query": "glass", "language": "en", "limit": 1})
    change(selection)
    source = Sources()
    agent = Agent(load_config(), Fake([selection]), SQLiteStore(tmp_path / "chat.sqlite3"), retrieval_client=source)
    result = agent.run("Find glass")
    assert result.status != "ok" and source.calls == []
    assert agent.state()["summary"]["api_requests"] == 1
    agent.close()


def test_assistant_preamble_continues_with_tool_output(tmp_path):
    selection = tool_reply("lookup_wikipedia", {"query": "glass", "language": "en", "limit": 1})
    selection["output"].insert(1, {"type": "message", "role": "assistant", "status": "completed",
        "content": [{"type": "output_text", "text": "I'll check a reference."}]})
    source = Sources()
    transport = Fake([selection, response("Glass is transparent.")])
    agent = Agent(load_config(), transport, SQLiteStore(tmp_path / "chat.sqlite3"), retrieval_client=source)
    assert agent.run("Explain glass").status == "ok"
    assert len(source.calls) == 1
    assert transport.calls[1]["input"][-3]["content"][0]["text"] == "I'll check a reference."
    agent.close()


def test_three_calls_force_final_step_without_tools(tmp_path):
    selection = tool_reply("lookup_wikipedia", {"query": "glass", "language": "en", "limit": 1})
    selection["output"].append(tool_reply("lookup_wikipedia", {"query": "render", "language": "en", "limit": 1}, "call_2")["output"][1])
    selection["output"].append(tool_reply("search_stackexchange", {"query": "glass shader", "community": "blender", "limit": 1}, "call_3")["output"][1])
    source = Sources()
    transport = Fake([selection, response("Here are three sources.")])
    agent = Agent(load_config(), transport, SQLiteStore(tmp_path / "chat.sqlite3"), retrieval_client=source)
    result = agent.run("Find several references")
    assert result.status == "ok" and len(source.calls) == 3
    assert transport.calls[1]["tool_choice"] == "none"
    assert agent.state()["summary"]["api_requests"] == 2
    agent.close()


def test_four_calls_in_one_response_are_rejected_before_any_mcp_contact(tmp_path):
    selection = tool_reply("lookup_wikipedia", {"query": "glass", "language": "en", "limit": 1})
    for n in range(2, 5):
        selection["output"].append(tool_reply("lookup_wikipedia",
            {"query": f"glass {n}", "language": "en", "limit": 1}, f"call_{n}")["output"][1])
    source = Sources()
    transport = Fake([selection])
    agent = Agent(load_config(), transport, SQLiteStore(tmp_path / "chat.sqlite3"), retrieval_client=source)
    result = agent.run("Find references")
    assert (result.status, result.code) == ("error", "tool_limit")
    assert source.calls == [] and len(transport.calls) == 1
    assert agent.state()["summary"]["api_requests"] == 1
    assert agent.state()["token_accounting"]["known_total_tokens"] == 110
    assert agent.state()["retrievals"] == []
    agent.close()

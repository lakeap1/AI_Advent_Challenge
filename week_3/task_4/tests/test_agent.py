from dataclasses import replace

import pytest

from agent import Agent, SQLiteStore, load_config
from agent.transport import TransportError


def isolated_agent(config, transport):
    return Agent(config, transport, SQLiteStore(":memory:"))


def response(text="Ответ", **overrides):
    return {"status": "completed", "output": [{"type": "message", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": text}]}], **overrides}


class FakeTransport:
    def __init__(self, output=None, error=None):
        self.output = response() if output is None else output
        self.error = error
        self.calls = []

    def create(self, payload, timeout):
        self.calls.append((payload, timeout))
        if self.error:
            raise self.error
        return self.output


@pytest.mark.parametrize("prompt,code", [(None, "input_invalid"), (3, "input_invalid"), ([], "input_invalid"), (" \n ", "input_invalid"), ("x" * 4001, "input_too_long")])
def test_input_rejected_without_provider(prompt, code):
    transport = FakeTransport()
    result = isolated_agent(replace(load_config(), max_input_chars=4000), transport).run(prompt)
    assert (result.status, result.code) == ("rejected", code)
    assert result.text
    assert transport.calls == []


def test_config_controls_request_and_boundary():
    config = replace(load_config(), model="test-model", instructions="Custom instruction", max_input_chars=5, max_output_tokens=99, timeout_seconds=12)
    transport = FakeTransport()
    agent = isolated_agent(config, transport)
    assert agent.run("  12345  ").status == "ok"
    assert agent.run("123456").code == "input_too_long"
    assert transport.calls == [({"model": "test-model", "instructions": "Custom instruction", "input": [{"role": "user", "content": "12345"}], "reasoning": {"effort": config.reasoning_effort}, "max_output_tokens": 99, "store": False, "truncation": "disabled", "service_tier": "default"}, 12)]


@pytest.mark.parametrize("output,code", [
    (response("partial", status="incomplete"), "incomplete"),
    (response("", status="failed"), "provider_error"),
    (response(" \n "), "empty_output"),
    (response(output=[]), "empty_output"),
    (response(output=[{"type": "message", "role": "assistant", "status": "completed", "content": [{"type": "refusal", "refusal": "No"}]}]), "refusal"),
    ([], "invalid_response"),
    ({}, "invalid_response"),
    (response(output=None), "invalid_response"),
    (response(output=[None]), "invalid_response"),
    (response(output=[{"type": "message", "role": "assistant", "status": "completed", "content": "bad"}]), "invalid_response"),
    (response(text=123), "invalid_response"),
    (response(status="queued"), "invalid_response"),
])
def test_output_policy(output, code):
    result = isolated_agent(load_config(), FakeTransport(output)).run("Вопрос")
    assert result.status != "ok"
    assert result.code == code
    assert "partial" not in result.text


def test_all_text_blocks_preserved_and_reasoning_not_exposed():
    output = response("  # Заголовок\n\nФормула: x * y  ")
    output["output"].insert(0, {"type": "reasoning", "summary": []})
    output["output"][1]["content"].append({"type": "output_text", "text": "\nКонец"})
    result = isolated_agent(load_config(), FakeTransport(output)).run("Вопрос")
    assert result.text == "# Заголовок\n\nФормула: x * y  \nКонец"


def test_messages_keep_ordered_context():
    transport = FakeTransport()
    agent = isolated_agent(load_config(), transport)
    agent.run("Первый")
    agent.run("Второй")
    assert [call[0]["input"] for call in transport.calls] == [
        [{"role": "user", "content": "Первый"}],
        [{"role": "user", "content": "Первый"}, {"role": "assistant", "content": "Ответ"}, {"role": "user", "content": "Второй"}],
    ]
    assert len(transport.calls) == 2


@pytest.mark.parametrize("code", ["authentication", "rate_limit", "timeout", "connection", "provider_error", "not_configured"])
def test_transport_errors_are_safe_results(code):
    result = isolated_agent(load_config(), FakeTransport(error=TransportError(code))).run("Вопрос")
    assert (result.status, result.code) == ("error", code)
    assert result.text


def test_default_instructions_and_policy_configuration():
    config = load_config()
    transport = FakeTransport()
    agent = isolated_agent(config, transport)
    try:
        assert agent.run('Что проверить?').status == 'ok'
        instructions = transport.calls[0][0]['instructions']
        assert "без Markdown" in instructions
        assert '{{ANSWER_FORMAT}}' not in instructions
    finally:
        agent.close()
    assert "явно" in config.instructions
    assert config.input_policy == "nonempty_text"
    assert config.output_policy == "completed_text"
    assert config.judge == "disabled"


@pytest.mark.parametrize("field,value", [("max_input_chars", 0), ("timeout_seconds", float("nan")), ("max_output_tokens", True), ("input_policy", "unknown"), ("output_policy", "none"), ("judge", "llm"), ("model", " ")])
def test_invalid_configuration_rejected(field, value):
    with pytest.raises(ValueError):
        replace(load_config(), **{field: value})


def test_toml_is_loaded_and_unknown_keys_rejected(tmp_path):
    from pathlib import Path
    source = Path(__file__).resolve().parents[1] / "agent" / "config.toml"
    path = tmp_path / "custom.toml"
    path.write_text(source.read_text(encoding="utf-8").replace("max_input_chars = 8000000", "max_input_chars = 3"), encoding="utf-8")
    assert load_config(path).max_input_chars == 3
    path.write_text(path.read_text(encoding="utf-8") + '\nunknown = "value"\n', encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(path)

def test_lone_surrogate_input_rejected_before_transport():
    transport = FakeTransport()
    result = isolated_agent(load_config(), transport).run('\ud800')
    assert result.code == 'input_invalid'
    assert transport.calls == []

def test_invalid_unicode_output_is_safe_error():
    result = isolated_agent(load_config(), FakeTransport(response('\ud800'))).run('Question')
    assert result.code == 'invalid_response'

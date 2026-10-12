from dataclasses import replace

import pytest

from agent import load_config
from test_agent import FakeTransport, response, isolated_agent


def billed_response(**changes):
    return response(
        model=load_config().model, service_tier="default",
        usage={"input_tokens": 1000, "output_tokens": 200, "total_tokens": 1200,
               "input_tokens_details": {"cached_tokens": 400, "cache_write_tokens": 0},
               "output_tokens_details": {"reasoning_tokens": 50}},
        **changes,
    )


def test_provider_counts_and_cost_cache_discount_no_double_reasoning():
    result = isolated_agent(load_config(), FakeTransport(billed_response())).run("Вопрос")
    assert result.usage.input_tokens == 1000
    assert result.usage.output_tokens == 200
    assert result.usage.total_tokens == 1200
    assert result.usage.cached_input_tokens == 400
    assert result.usage.reasoning_tokens == 50
    # 600 * .10 + 400 * .01 + 200 * .50, за миллион токенов.
    assert result.cost_usd == "0.000164"
    assert result.usage_status == "reported"


def test_incomplete_response_keeps_billable_usage():
    result = isolated_agent(load_config(), FakeTransport(billed_response(status="incomplete"))).run("Вопрос")
    assert result.code == "incomplete"
    assert result.usage.total_tokens == 1200
    assert result.cost_usd == "0.000164"


@pytest.mark.parametrize("mutation", [
    {"model": "different-model"}, {"service_tier": "priority"},
    {"service_tier": None},
])
def test_unknown_pricing_context_does_not_claim_zero(mutation):
    body = billed_response()
    body.update(mutation)
    result = isolated_agent(load_config(), FakeTransport(body)).run("Вопрос")
    assert result.usage.total_tokens == 1200
    assert result.cost_usd is None


@pytest.mark.parametrize("usage", [None, {}, [], {"input_tokens": True, "output_tokens": 5, "total_tokens": 6}, {"input_tokens": 5, "output_tokens": 5, "total_tokens": 11}])
def test_missing_or_malformed_usage_remains_unknown(usage):
    body = billed_response()
    body["usage"] = usage
    result = isolated_agent(load_config(), FakeTransport(body)).run("Вопрос")
    assert result.status == "ok"
    assert result.usage is None and result.cost_usd is None
    assert result.usage_status == "unavailable"


def test_no_request_means_no_tokens_spent():
    transport = FakeTransport()
    result = isolated_agent(load_config(), transport).run("")
    assert result.usage_status == "not_requested"
    assert result.usage is None
    assert transport.calls == []


@pytest.mark.parametrize("details", [{}, {"cached_tokens": 1001}, {"cached_tokens": 400, "cache_creation_tokens": 100}])
def test_unknown_cache_accounting_means_unknown_cost(details):
    body = billed_response()
    body["usage"]["input_tokens_details"] = details
    result = isolated_agent(load_config(), FakeTransport(body)).run("Вопрос")
    assert result.cost_usd is None


def test_config_rates_change_cost_without_ui_changes():
    config = load_config()
    config = replace(config, pricing=replace(config.pricing, output_usd_per_million="2.00"))
    result = isolated_agent(config, FakeTransport(billed_response())).run("Вопрос")
    assert result.cost_usd == "0.000464"


def test_different_config_model_does_not_reuse_luna_prices():
    config = replace(load_config(), model="different-model")
    result = isolated_agent(config, FakeTransport(billed_response())).run("Вопрос")
    assert result.cost_usd is None

@pytest.mark.parametrize('value', ['NaN', 'Infinity', '-1', True, 'not-a-price'])
def test_invalid_rates_rejected(value):
    config = load_config()
    with pytest.raises(ValueError):
        replace(config.pricing, input_usd_per_million=value)


def test_long_context_cost_is_not_silently_short_context_price():
    body = billed_response()
    body['usage'].update(input_tokens=272001, total_tokens=272201)
    result = isolated_agent(load_config(), FakeTransport(body)).run('Вопрос')
    assert result.usage.input_tokens == 272001
    assert result.cost_usd is None


def test_usage_survives_http_serialization(tmp_path, monkeypatch):
    from app import create_app
    from tests_rag.grounded_fake import GroundedTransport, install_local_index
    install_local_index(monkeypatch)
    client = create_app(data_dir=tmp_path,
                        transport=GroundedTransport(answer_usage=billed_response()['usage'])).test_client()
    result = client.post('/api/ask', json={'prompt': 'Вопрос'}).json
    assert result['status'] == 'ok' and result['text'].endswith('[S1]')
    assert result['usage']['input_tokens'] == 1000
    assert result['usage']['output_tokens'] == 200
    assert result['cost_usd'] == '0.000164'


def test_cache_write_replaces_ordinary_input_and_persists():
    body = billed_response()
    body['usage']['input_tokens_details']['cache_write_tokens'] = 300
    agent = isolated_agent(load_config(), FakeTransport(body))
    result = agent.run('Ассет')
    assert result.usage.cache_write_input_tokens == 300
    # 300 regular*.10 + 400 read*.01 + 300 write*.125 + 200 output*.50.
    assert result.cost_usd == '0.0001715'
    assert agent.state()['requests'][0]['usage']['cache_write_input_tokens'] == 300


@pytest.mark.parametrize('written', [None, -1, True, 601, 1001])
def test_unknown_or_inconsistent_cache_write_does_not_invent_cost(written):
    body = billed_response()
    body['usage']['input_tokens_details']['cache_write_tokens'] = written
    result = isolated_agent(load_config(), FakeTransport(body)).run('Ассет')
    assert result.cost_usd is None
    assert result.usage.input_tokens == 1000


def test_missing_cache_write_field_remains_unknown():
    body = billed_response()
    del body['usage']['input_tokens_details']['cache_write_tokens']
    result = isolated_agent(load_config(), FakeTransport(body)).run('Ассет')
    assert result.usage.cache_write_input_tokens is None
    assert result.cost_usd is None

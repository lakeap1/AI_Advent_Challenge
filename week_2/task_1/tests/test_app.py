import pytest

from agent import Agent, AgentResult, load_config
from app import create_app
from test_agent import FakeTransport


def test_web_integrates_real_agent_contract():
    transport = FakeTransport()
    client = create_app(Agent(load_config(), transport)).test_client()
    assert client.post('/api/ask', json={"prompt": "  "}).status_code == 400
    assert transport.calls == []
    response = client.post('/api/ask', json={"prompt": "Привет"})
    assert response.status_code == 200
    assert response.json == {"status": "ok", "text": "Ответ", "code": "", "usage": None, "cost_usd": None, "usage_status": "unavailable"}
    assert len(transport.calls) == 1


@pytest.mark.parametrize("payload", [[], None, "text", 3])
def test_non_object_json_rejected(payload):
    client = create_app(Agent(load_config(), FakeTransport())).test_client()
    assert client.post('/api/ask', json=payload).status_code == 400


def test_malformed_json_rejected():
    client = create_app(Agent(load_config(), FakeTransport())).test_client()
    result = client.post('/api/ask', data='{', content_type='application/json')
    assert result.status_code == 400
    assert result.json['code'] == 'invalid_request'


@pytest.mark.parametrize("code,http_status", [("timeout", 504), ("rate_limit", 429), ("not_configured", 503), ("authentication", 502), ("refusal", 502)])
def test_error_mapping_without_provider_details(code, http_status):
    class Stub:
        def run(self, prompt):
            return AgentResult("error", "Понятная ошибка", code)
    response = create_app(Stub()).test_client().post('/api/ask', json={"prompt": "Вопрос"})
    assert response.status_code == http_status
    assert response.json['text'] == 'Понятная ошибка'


def test_page_and_assets():
    client = create_app(Agent(load_config(), FakeTransport())).test_client()
    page = client.get('/')
    assert page.status_code == 200
    assert 'Первый агент' in page.get_data(as_text=True)
    assert client.get('/static/app.js').status_code == 200
    assert client.get('/static/app.css').status_code == 200

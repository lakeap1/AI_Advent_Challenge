import pytest
from agent import AgentResult
from agent.storage import StorageError
from app import create_app

class Stub:
    def __init__(self):
        self.calls = []
    def run(self, prompt):
        self.calls.append(prompt)
        return AgentResult('ok', 'Ответ')
    def state(self):
        return {'chat_id': 'saved-chat', 'messages': [], 'requests': [],
                'summary': {'known_cost_usd': '0', 'cost_complete': True,
                            'unknown_cost_requests': 0, 'api_requests': 0}}

def test_state_is_restored_with_new_boot_id():
    first = create_app(Stub()).test_client().get('/api/state')
    second = create_app(Stub()).test_client().get('/api/state')
    assert first.status_code == second.status_code == 200
    assert first.json['chat_id'] == second.json['chat_id'] == 'saved-chat'
    assert first.json['boot_id'] != second.json['boot_id']
    assert first.headers['Cache-Control'] == 'no-store'

def test_web_returns_result_and_state():
    worker = Stub()
    response = create_app(worker).test_client().post('/api/ask', json={'prompt': 'Привет'})
    assert response.status_code == 200
    assert response.json['text'] == 'Ответ'
    assert response.json['state']['chat_id'] == 'saved-chat'
    assert worker.calls == ['Привет']

@pytest.mark.parametrize('payload', [[], None, 'text', 3])
def test_non_object_json_rejected_without_agent(payload):
    worker = Stub()
    response = create_app(worker).test_client().post('/api/ask', json=payload)
    assert response.status_code == 400
    assert response.json['usage_status'] == 'not_requested'
    assert not worker.calls

def test_malformed_json_rejected():
    response = create_app(Stub()).test_client().post('/api/ask', data='{', content_type='application/json')
    assert response.status_code == 400
    assert response.json['code'] == 'invalid_request'

@pytest.mark.parametrize('code,http_status', [('timeout', 504), ('rate_limit', 429), ('not_configured', 503), ('authentication', 502), ('refusal', 502)])
def test_error_mapping(code, http_status):
    worker = Stub()
    worker.run = lambda prompt: AgentResult('error', 'Понятная ошибка', code)
    response = create_app(worker).test_client().post('/api/ask', json={'prompt': 'Вопрос'})
    assert response.status_code == http_status
    assert response.json['text'] == 'Понятная ошибка'

@pytest.mark.parametrize('route', ['state', 'ask'])
def test_storage_failure_is_explicit_and_not_success(route):
    worker = Stub()
    def fail(*args):
        raise StorageError('private path')
    if route == 'state':
        worker.state = fail
        response = create_app(worker).test_client().get('/api/state')
    else:
        worker.run = fail
        response = create_app(worker).test_client().post('/api/ask', json={'prompt': 'Привет'})
    assert response.status_code == 503
    assert response.json['code'] == 'storage_error'
    assert 'private path' not in response.get_data(as_text=True)

def test_page_and_assets():
    client = create_app(Stub()).test_client()
    assert 'День 07' in client.get('/').get_data(as_text=True)
    assert client.get('/static/app.js').status_code == 200
    assert client.get('/static/app.css').status_code == 200

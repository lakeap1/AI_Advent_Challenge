from concurrent.futures import ThreadPoolExecutor
from threading import Event
import time

import httpx
import pytest

from week_6.task_5.app import create_app
from week_6.task_5.private_service.transport import LocalOllamaTransport


TOKEN = 'a' * 32


def reply(text='Ответ.', *, incoming=12, outgoing=8, done=True):
    return {'model': 'qwen3.5:4b', 'message': {'role': 'assistant', 'content': text},
            'done': done, 'prompt_eval_count': incoming, 'eval_count': outgoing}


class Transport:
    def __init__(self, response=None):
        self.response = response or reply()
        self.calls = []

    def __call__(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if path == '/api/tags':
            return {'models': [{'name': 'qwen3.5:4b'}]}
        if path == '/api/version':
            return {'version': '0.40.3'}
        if path == '/api/ps':
            return {'models': [{'name': 'qwen3.5:4b', 'size_vram': 0}]}
        return self.response


def app_for(tmp_path, transport=None, clock=None):
    return create_app(data_dir=tmp_path, token=TOKEN, local_transport=transport or Transport(), clock=clock)


def ask(app, prompt='Свет?', headers=None):
    return app.test_client().post('/api/private/ask', json={'prompt': prompt},
                                  headers=headers or {'Authorization': f'Bearer {TOKEN}'})


def test_authorization_precedes_state_and_inference(tmp_path):
    transport = Transport()
    app = app_for(tmp_path, transport)
    for path in ('/api/private/state', '/api/private/runtime', '/api/private/ask'):
        response = app.test_client().get(path) if path != '/api/private/ask' else ask(app, headers={'Authorization': 'Bearer wrong'})
        assert response.status_code == 401
        assert response.json['code'] == 'unauthorized'
        assert 'messages' not in response.json
    assert transport.calls == []


def test_non_ascii_bearer_is_unauthorized_without_500(tmp_path):
    app = app_for(tmp_path)
    response = app.test_client().get('/api/private/state', headers={'Authorization': 'Bearer ключ'})
    assert response.status_code == 401
    assert response.json['code'] == 'unauthorized'


def test_oversize_json_has_coherent_413_and_never_reaches_model(tmp_path):
    transport = Transport()
    app = app_for(tmp_path, transport)
    response = app.test_client().post('/api/private/ask',
        data='{"prompt":"' + 'x' * 33000 + '"}', content_type='application/json',
        headers={'Authorization': f'Bearer {TOKEN}'})
    assert response.status_code == 413
    assert response.json['code'] == 'request_too_large'
    assert transport.calls == []


def test_success_receipt_and_plain_text_instruction(tmp_path):
    transport = Transport()
    app = app_for(tmp_path, transport)
    response = ask(app)
    assert response.status_code == 200
    assert response.json['answer'] == 'Ответ.'
    assert response.json['receipt']['usage']['total_tokens'] == 20
    assert response.json['state']['ledger']['total_cost_usd'] == '0'
    assert response.json['state']['counters']['inference_calls'] == 1
    payload = transport.calls[0][2]
    assert payload['model'] == 'qwen3.5:4b'
    assert payload['options'] == {'num_ctx': 4096, 'num_predict': 384, 'num_gpu': 0}
    assert payload['think'] is False
    assert 'без Markdown' in payload['messages'][0]['content']
    assert app.test_client().get('/api/private/state', headers={'Authorization': f'Bearer {TOKEN}'}).json['messages'][-1]['content'] == 'Ответ.'


def test_runtime_cpu_policy_does_not_infer_gpu_from_size_vram(tmp_path):
    class PositiveVram(Transport):
        def __call__(self, method, path, payload=None):
            if path == '/api/ps':
                return {'models': [{'name': 'qwen3.5:4b', 'size_vram': 2683746385}]}
            return super().__call__(method, path, payload)
    app = app_for(tmp_path, PositiveVram())
    runtime = app.test_client().get('/api/private/runtime',
                                    headers={'Authorization': f'Bearer {TOKEN}'})
    assert runtime.status_code == 200
    assert runtime.json['status'] == 'ready'
    assert runtime.json['processor'] == 'CPU'
    assert runtime.json['num_gpu'] == 0
    assert runtime.json['size_vram'] == 2683746385
    assert runtime.json['placement'] == 'CPU forced by num_gpu=0; device telemetry not inferred from size_vram'


def test_rate_limit_ignores_forwarded_address_and_recovers(tmp_path):
    now = [100.0]
    app = app_for(tmp_path, clock=lambda: now[0])
    for i in range(10):
        assert ask(app, headers={'Authorization': f'Bearer {TOKEN}', 'X-Forwarded-For': str(i)}).status_code == 200
    limited = ask(app, headers={'Authorization': f'Bearer {TOKEN}', 'X-Forwarded-For': 'another'})
    assert limited.status_code == 429
    assert limited.headers['Retry-After'] == '60'
    assert limited.json['code'] == 'rate_limited'
    now[0] += 60
    assert ask(app).status_code == 200


def test_state_and_runtime_respond_during_inference_and_queue_bounded(tmp_path):
    started, release = Event(), Event()
    class BusyTransport(Transport):
        def __call__(self, method, path, payload=None):
            if path == '/api/chat':
                started.set()
                release.wait(3)
            return super().__call__(method, path, payload)
    app = app_for(tmp_path, BusyTransport())
    auth = {'Authorization': f'Bearer {TOKEN}'}
    with ThreadPoolExecutor(max_workers=5) as pool:
        first = pool.submit(ask, app)
        assert started.wait(2)
        assert app.test_client().get('/api/private/state', headers=auth).status_code == 200
        assert app.test_client().get('/api/private/runtime', headers=auth).status_code == 200
        waiting = [pool.submit(ask, app) for _ in range(3)]
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and app.extensions['private_service']['gateway'].waiting < 3:
            time.sleep(.01)
        assert app.extensions['private_service']['gateway'].waiting == 3
        overflow = ask(app)
        assert overflow.status_code == 503
        assert overflow.json['code'] == 'queue_full'
        release.set()
        assert first.result().status_code == 200
        assert all(item.result().status_code == 200 for item in waiting)
    assert app.extensions['private_service']['gateway'].slots_available == 4


def test_context_rejection_does_not_call_model(tmp_path):
    transport = Transport()
    app = app_for(tmp_path, transport)
    response = ask(app, 'x' * 2001)
    assert response.status_code == 400
    assert response.json['receipt']['usage_status'] == 'not_requested'
    assert response.json['state']['counters']['inference_calls'] == 0
    assert transport.calls == []


def test_lone_surrogate_is_recorded_input_rejection_without_inference(tmp_path):
    transport = Transport()
    app = app_for(tmp_path, transport)
    response = app.test_client().post('/api/private/ask',
        data='{"prompt":"\\ud800"}', content_type='application/json',
        headers={'Authorization': f'Bearer {TOKEN}'})
    assert response.status_code == 400
    assert response.json['status'] == 'rejected'
    assert response.json['receipt']['input_policy'] == 'rejected'
    assert response.json['receipt']['usage_status'] == 'not_requested'
    assert response.json['state']['counters']['inference_calls'] == 0
    assert transport.calls == []


def test_valid_emoji_prompt_still_reaches_model(tmp_path):
    transport = Transport()
    app = app_for(tmp_path, transport)
    response = ask(app, 'Как нарисовать свет? 🎨')
    assert response.status_code == 200
    assert response.json['answer'] == 'Ответ.'
    assert transport.calls[0][2]['messages'][-1]['content'] == 'Как нарисовать свет? 🎨'


def test_wait_timeout_releases_capacity(tmp_path):
    started, release = Event(), Event()
    class Slow(Transport):
        def __call__(self, method, path, payload=None):
            if path == '/api/chat':
                started.set()
                release.wait(2)
            return super().__call__(method, path, payload)
    app = app_for(tmp_path, Slow())
    gateway = app.extensions['private_service']['gateway']
    gateway.WAIT_TIMEOUT = .02
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(ask, app)
        assert started.wait(1)
        second = ask(app)
        assert second.status_code == 503
        assert second.json['code'] == 'queue_timeout'
        assert gateway.waiting == 0
        release.set()
        assert first.result().status_code == 200
    assert gateway.slots_available == 4


def test_malformed_authorized_ask_uses_rate_budget(tmp_path):
    app = app_for(tmp_path)
    auth = {'Authorization': f'Bearer {TOKEN}'}
    for _ in range(10):
        response = app.test_client().post('/api/private/ask', json={'unexpected': 'x'}, headers=auth)
        assert response.status_code == 400
    response = ask(app)
    assert response.status_code == 429
    assert app.extensions['private_service']['gateway'].state()['counters']['inference_calls'] == 0


def test_unknown_usage_blocks_after_restart(tmp_path):
    transport = Transport(reply(incoming=True, outgoing=8))
    first = ask(app_for(tmp_path, transport))
    assert first.status_code == 503
    assert first.json['receipt']['usage_status'] == 'unknown'
    assert first.json['state']['blocked'] is True
    restarted = app_for(tmp_path, Transport())
    second = ask(restarted)
    assert second.status_code == 409
    assert second.json['state']['ledger']['total_cost_usd'] is None
    assert second.json['state']['counters']['inference_calls'] == 0


def test_output_rejection_does_not_count_as_input_policy_rejection(tmp_path):
    app = app_for(tmp_path, Transport(reply(text='x' * 2001)))
    response = ask(app)
    assert response.status_code == 400
    assert response.json['receipt']['input_policy'] == 'accepted'
    assert response.json['receipt']['output_policy'] == 'rejected'
    assert response.json['state']['counters']['context_rejected'] == 0


def test_invalid_token_fails_startup(tmp_path):
    with pytest.raises(ValueError):
        create_app(data_dir=tmp_path, token='short', local_transport=Transport())


def test_ollama_transport_stays_on_loopback_without_proxy_or_redirect(monkeypatch):
    real_client = httpx.Client
    seen = []

    def redirect(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={'Location': 'https://api.openai.com/v1/responses'})

    def client_factory(**options):
        assert options['trust_env'] is False
        assert options['follow_redirects'] is False
        return real_client(transport=httpx.MockTransport(redirect), **options)

    monkeypatch.setenv('HTTP_PROXY', 'http://proxy.invalid:1234')
    monkeypatch.setattr(httpx, 'Client', client_factory)
    with pytest.raises(ValueError, match='перенаправление'):
        LocalOllamaTransport()('POST', '/api/chat', {'model': 'qwen3.5:4b'})
    assert seen == ['http://127.0.0.1:11435/api/chat']
    with pytest.raises(ValueError, match='Недопустимый'):
        LocalOllamaTransport()('POST', '/api/pull', {'model': 'other'})

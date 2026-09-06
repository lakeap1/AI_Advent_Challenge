import itertools
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from openai import APIConnectionError, AuthenticationError, RateLimitError, BadRequestError

from app import MODEL, build_request, create_app


def fake_client(text='Обычный ответ', reason='stop', refusal=None):
    client = Mock()
    client.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text, refusal=refusal), finish_reason=reason)],
        usage=SimpleNamespace(completion_tokens=25))
    return client


@pytest.mark.parametrize('json_format,limit_enabled,stop_enabled', list(itertools.product([False, True], repeat=3)))
def test_each_combination_reaches_api(json_format, limit_enabled, stop_enabled):
    client = fake_client()
    response = create_app(client).test_client().post('/api/chat', json={
        'prompt': '  Объясни HTTPS  ', 'json_format': json_format,
        'limit_enabled': limit_enabled, 'stop_enabled': stop_enabled, 'max_words': 60})
    assert response.status_code == 200
    client.chat.completions.create.assert_called_once()
    args = client.chat.completions.create.call_args.kwargs
    assert args['model'] == 'gpt-4.1-mini'
    assert args['messages'][-1] == {'role': 'user', 'content': 'Объясни HTTPS'}
    assert 'max_completion_tokens' not in args
    assert ('stop' in args) == stop_enabled
    if limit_enabled:
        assert 'не более 60 слов' in args['messages'][0]['content']
    if stop_enabled:
        assert args['stop'] == ['[КОНЕЦ]']
        assert 'все части вопроса' in args['messages'][0]['content']
    if json_format:
        assert 'JSON-объектом' in args['messages'][0]['content']
    if not any((json_format, limit_enabled, stop_enabled)):
        assert args['messages'] == [{'role': 'user', 'content': 'Объясни HTTPS'}]
    assert response.json['request'] == args


@pytest.mark.parametrize('payload', [None, [], {}, {'prompt': ''}, {'prompt': 1},
    {'prompt': ' '}, {'prompt': 'x' * 4001}, {'prompt': 'x', 'stop_enabled': 'false'},
    {'prompt': 'x', 'limit_enabled': True, 'max_words': True},
    {'prompt': 'x', 'limit_enabled': True, 'max_words': 0},
    {'prompt': 'x', 'limit_enabled': True, 'max_words': 1001},
    {'prompt': 'x', 'limit_enabled': True, 'max_words': 23.5}])
def test_invalid_input_never_calls_api(payload):
    client = fake_client()
    response = create_app(client).test_client().post('/api/chat', json=payload)
    assert response.status_code == 400
    client.chat.completions.create.assert_not_called()


def test_stop_does_not_locally_strip_or_rewrite_text():
    text = 'Ответ [КОНЕЦ] остаток\n'
    client = fake_client(text)
    response = create_app(client).test_client().post('/api/chat', json={'prompt': 'x', 'stop_enabled': True})
    assert response.json['text'] == text


@pytest.mark.parametrize('text,valid', [
    ('{"answer":["Первый","Второй","Третий"]}', True),
    ('{"answer":["Первый","Второй"]}', False),
    ('{"answer":["Первый","Второй","Третий","Четвёртый"]}', False),
    ('{"answer":["Первый","Второй",""]}', False),
    ('{"answer":[1]}', False), ('[]', False), ('{"other":[]}', False),
    ('{"answer":["Оборванный', False), ('```json\n{"answer":[]}\n```', False)])
def test_json_validation_reports_actual_output(text, valid):
    client = fake_client(text, reason='length')
    response = create_app(client).test_client().post('/api/chat', json={'prompt': 'x', 'json_format': True})
    assert response.json['text'] == text
    assert response.json['json_ok'] is valid
    assert response.json['finish_reason'] == 'length'


@pytest.mark.parametrize('text,json_format,words,within', [
    ('Раз два три', False, 3, True),
    ('Раз два три четыре', False, 4, False),
    ('{"answer": ["Раз", "два", "три"]}', True, 3, True),
    ('{"answer":["Раз два","три","четыре"]}', True, 4, False),
    ('{"answer":["Раз два', True, None, None),
])
def test_word_limit_checks_content_without_trimming(text, json_format, words, within):
    response = create_app(fake_client(text)).test_client().post('/api/chat', json={
        'prompt': 'x', 'json_format': json_format, 'limit_enabled': True, 'max_words': 3})
    assert response.json['text'] == text
    assert response.json['word_count'] == words
    assert response.json['word_limit_ok'] is within
    assert response.json['output_tokens'] == 25


def test_disabled_word_limit_is_not_enforced():
    response = create_app(fake_client('Раз два')).test_client().post('/api/chat', json={
        'prompt': 'x', 'limit_enabled': False, 'max_words': 1})
    assert response.json['word_limit_ok'] is None


def test_missing_key(monkeypatch):
    monkeypatch.setattr('app.load_dotenv', lambda *args: None)
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    assert create_app().test_client().post('/api/chat', json={'prompt': 'x'}).status_code == 503


@pytest.mark.parametrize('status,error_type', [(401, AuthenticationError), (429, RateLimitError), (400, BadRequestError)])
def test_provider_errors_are_handled(status, error_type):
    client = fake_client()
    client.chat.completions.create.side_effect = error_type('provider failure',
        response=httpx.Response(status, request=httpx.Request('POST', 'https://api.openai.com')), body=None)
    response = create_app(client).test_client().post('/api/chat', json={'prompt': 'x'})
    assert response.status_code == (status if status in (401, 429) else 502)
    assert 'error' in response.json


def test_network_error():
    client = fake_client()
    client.chat.completions.create.side_effect = APIConnectionError(request=httpx.Request('POST', 'https://api.openai.com'))
    assert create_app(client).test_client().post('/api/chat', json={'prompt': 'x'}).status_code == 502


@pytest.mark.parametrize('text,refusal,status', [('', None, 502), (None, 'Отказ', 422)])
def test_empty_and_refused_answer(text, refusal, status):
    assert create_app(fake_client(text, refusal=refusal)).test_client().post('/api/chat', json={'prompt': 'x'}).status_code == status

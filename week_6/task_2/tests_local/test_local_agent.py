from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import httpx
import pytest

from local_agent import LocalChat
from local_agent.transport import LocalTransportError, OllamaTransport


def answer(text='Мягкий свет уменьшает резкие тени.', incoming=12, outgoing=8, done=True):
    return {'model': 'qwen3.5:9b', 'message': {'role': 'assistant', 'content': text},
            'done': done, 'prompt_eval_count': incoming, 'eval_count': outgoing}


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def test_complete_history_is_sent_after_restart(tmp_path):
    fake = FakeTransport(answer('Первый ответ.'), answer('Второй ответ.'))
    first = LocalChat(tmp_path, transport=fake)
    assert first.ask('Первый вопрос')['status'] == 'ok'
    restarted = LocalChat(tmp_path, transport=fake)
    second = restarted.ask('Второй вопрос')
    assert second['status'] == 'ok'
    assert [message['role'] for message in fake.calls[1][2]['messages']] == [
        'system', 'user', 'assistant', 'user']
    assert [message['content'] for message in fake.calls[1][2]['messages'][1:]] == [
        'Первый вопрос', 'Первый ответ.', 'Второй вопрос']
    assert fake.calls[1][2]['stream'] is False
    assert fake.calls[1][2]['think'] is False
    assert fake.calls[1][2]['options'] == {'num_ctx': 8192, 'num_predict': 1024}
    assert 'без Markdown' in fake.calls[1][2]['messages'][0]['content']
    assert len(second['state']['messages']) == 4
    assert second['state']['ledger']['total_cost_usd'] == '0'
    assert second['state']['ledger']['complete'] is True


def test_invalid_input_and_context_limit_stop_before_http(tmp_path):
    fake = FakeTransport()
    chat = LocalChat(tmp_path, transport=fake)
    for prompt in ('', ' ', 'x' * 2001, None):
        assert chat.ask(prompt)['status'] == 'rejected'
    assert fake.calls == []
    assert all(receipt['usage_status'] == 'not_requested' for receipt in chat.state()['receipts'])


def test_full_context_limit_rejects_before_http_without_truncation(tmp_path):
    config = Path(__file__).parents[1] / 'local_agent' / 'config.toml'
    custom = tmp_path / 'short-context.toml'
    custom.write_text(config.read_text(encoding='utf-8').replace('max_context_characters = 16000',
                                                              'max_context_characters = 10'), encoding='utf-8')
    fake = FakeTransport()
    result = LocalChat(tmp_path, config_path=custom, transport=fake).ask('Вопрос')
    assert result['status'] == 'rejected'
    assert fake.calls == []
    assert result['receipt']['usage_status'] == 'not_requested'


def test_unicode_context_byte_bound_rejects_before_runtime_can_trim(tmp_path):
    fake = FakeTransport()
    result = LocalChat(tmp_path, transport=fake).ask('🎨' * 1800)
    assert result['status'] == 'rejected'
    assert fake.calls == []
    assert result['receipt']['usage_status'] == 'not_requested'


def test_output_rejection_keeps_usage_but_no_messages(tmp_path):
    fake = FakeTransport(answer('x' * 2001))
    result = LocalChat(tmp_path, transport=fake).ask('Вопрос')
    assert result['status'] == 'rejected'
    assert result['answer'] is None
    assert result['receipt']['usage']['total_tokens'] == 20
    assert result['receipt']['output_policy'] == 'rejected'
    assert result['state']['messages'] == []
    assert result['state']['ledger']['total_cost_usd'] == '0'
    assert result['receipt']['pricing']['source']
    assert result['receipt']['pricing']['checked_on'] == '2026-10-11'


def test_missing_counters_block_future_calls_after_restart(tmp_path):
    fake = FakeTransport({'model': 'qwen3.5:9b', 'message': {'role': 'assistant', 'content': 'Ответ.'}, 'done': True})
    result = LocalChat(tmp_path, transport=fake).ask('Вопрос')
    assert result['status'] == 'error'
    assert result['receipt']['usage']['input_tokens'] is None
    assert result['state']['ledger']['complete'] is False
    assert result['state']['blocked'] is True
    restarted = LocalChat(tmp_path, transport=fake)
    assert restarted.ask('Следующий вопрос')['status'] == 'blocked'
    assert len(fake.calls) == 1


def test_partial_usage_survives_restart_and_blocks_without_discarding_input(tmp_path):
    fake = FakeTransport({'model': 'qwen3.5:9b', 'message': {'role': 'assistant', 'content': 'Ответ.'},
                          'done': True, 'prompt_eval_count': 17})
    first = LocalChat(tmp_path, transport=fake).ask('Вопрос')
    assert first['receipt']['usage']['input_tokens'] == 17
    assert first['receipt']['usage']['output_tokens'] is None
    assert first['receipt']['usage']['total_tokens'] is None
    assert first['receipt']['usage_status'] == 'unknown'
    assert first['receipt']['cost_usd'] is None
    restarted = LocalChat(tmp_path, transport=fake)
    assert restarted.state()['receipts'][0]['usage']['input_tokens'] == 17
    assert restarted.ask('Ещё')['status'] == 'blocked'
    assert len(fake.calls) == 1


def test_reported_cached_input_tokens_are_recorded(tmp_path):
    response = answer()
    response['prompt_eval_cached_count'] = 5
    receipt = LocalChat(tmp_path, transport=FakeTransport(response)).ask('Вопрос')['receipt']
    assert receipt['usage']['cached_input_tokens'] == 5
    assert receipt['usage_status'] == 'known'


def test_provider_error_field_rejects_response_even_with_valid_text_and_usage(tmp_path):
    response = answer()
    response['error'] = 'runtime failed'
    result = LocalChat(tmp_path, transport=FakeTransport(response)).ask('Вопрос')
    assert result['status'] == 'rejected'
    assert result['answer'] is None
    assert result['receipt']['usage']['total_tokens'] == 20
    assert result['state']['messages'] == []


def test_pending_receipt_survives_interruption_and_blocks_restart(tmp_path):
    def interrupted(method, path, payload=None):
        assert LocalChat(tmp_path, transport=interrupted).state()['receipts'][0]['status'] == 'pending'
        raise KeyboardInterrupt()

    chat = LocalChat(tmp_path, transport=interrupted)
    try:
        chat.ask('Вопрос')
    except KeyboardInterrupt:
        pass
    else:
        assert False, 'expected interruption'
    restarted = LocalChat(tmp_path, transport=FakeTransport())
    assert restarted.state()['blocked'] is True
    assert restarted.ask('Следующий вопрос')['status'] == 'blocked'


def test_unavailable_runtime_is_visible_and_blocks_later_calls(tmp_path):
    fake = FakeTransport(ConnectionError('offline'))
    chat = LocalChat(tmp_path, transport=fake)
    result = chat.ask('Вопрос')
    assert result['status'] == 'error'
    assert 'Ollama' in result['error']
    assert result['receipt']['usage'] is None
    assert LocalChat(tmp_path, transport=fake).ask('Ещё вопрос')['status'] == 'blocked'
    assert len(fake.calls) == 1


def test_concurrent_questions_preserve_order_and_ledger(tmp_path):
    fake = FakeTransport(answer('Первый.'), answer('Второй.'))
    chat = LocalChat(tmp_path, transport=fake)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(chat.ask, ['Первый?', 'Второй?']))
    assert [item['status'] for item in results] == ['ok', 'ok']
    state = LocalChat(tmp_path, transport=fake).state()
    assert len(state['messages']) == 4
    assert len(state['receipts']) == 2
    assert state['ledger']['requests'] == 2
    assert len(fake.calls[1][2]['messages']) == 4


def test_incomplete_response_rejects_output_and_preserves_usage(tmp_path):
    result = LocalChat(tmp_path, transport=FakeTransport(answer(done=False))).ask('Вопрос')
    assert result['status'] == 'rejected'
    assert result['receipt']['usage']['input_tokens'] == 12
    assert result['state']['messages'] == []


def test_transport_uses_loopback_without_proxy_or_redirect(monkeypatch):
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
    with pytest.raises(LocalTransportError, match='перенаправление'):
        OllamaTransport()('POST', '/api/chat', {'model': 'qwen3.5:9b'})
    assert seen == ['http://127.0.0.1:11434/api/chat']

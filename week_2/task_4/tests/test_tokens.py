from dataclasses import replace
import httpx
import pytest
import tiktoken
from agent import Agent, SQLiteStore, load_config
from agent.tokens import TokenCounter
from agent.transport import ResponsesTransport, TransportError
from test_agent import FakeTransport, response


def paid(text='Ответ', status='completed'):
    return response(text, status=status, model='gpt-5.6-luna', service_tier='default', usage={
        'input_tokens': 100, 'output_tokens': 20, 'total_tokens': 120,
        'input_tokens_details': {'cached_tokens': 10, 'cache_write_tokens': 0},
        'output_tokens_details': {'reasoning_tokens': 5}})


def test_text_estimate_has_no_framing_and_accepts_special_literals():
    counter = TokenCounter()
    text = 'Игровой арт 🎨 <|endoftext|>'
    expected = len(tiktoken.get_encoding('o200k_base').encode(text, disallowed_special=()))
    assert counter.count(text) == expected
    metrics = counter.measure(text, [{'role': 'user', 'content': 'Привет'}], 'Правило')
    assert metrics['input_text_tokens_estimate'] == expected + counter.count('Привет') + counter.count('Правило')
    assert metrics['method'] == 'local_text_estimate'


def test_second_turn_metrics_persist_before_transport_and_after_restart(tmp_path):
    db = tmp_path / 'chat.db'
    config = load_config()
    transport = FakeTransport(paid())
    agent = Agent(config, transport, SQLiteStore(db))
    agent.run('  Первый  ')
    before = agent.state()
    preview = agent.preview('Второй')
    assert agent.state() == before
    original = transport.create
    def inspect(payload, timeout):
        pending = agent.state()['requests'][-1]
        assert pending['status'] == 'pending'
        assert pending['metadata']['token_metrics'] == preview['token_metrics']
        assert payload['truncation'] == 'disabled'
        return original(payload, timeout)
    transport.create = inspect
    agent.run('Второй')
    state = agent.state()
    metrics = state['requests'][-1]['metadata']['token_metrics']
    counter = TokenCounter()
    assert metrics['history_tokens_estimate'] == counter.count('Первый') + counter.count('Ответ')
    assert metrics['new_message_tokens_estimate'] == counter.count('Второй')
    assert state['token_accounting']['known_total_tokens'] == 240
    assert state['context']['history_tokens_estimate'] == sum(counter.count(m['content']) for m in state['messages'])
    agent.close()
    restored = Agent(config, FakeTransport(), SQLiteStore(db))
    assert restored.state() == state
    restored.close()


def test_failed_call_keeps_estimates_and_makes_totals_incomplete():
    transport = FakeTransport(paid(status='incomplete'))
    agent = Agent(load_config(), transport, SQLiteStore(':memory:'))
    assert agent.run('Первый').code == 'incomplete'
    transport.error = TransportError('context_length_exceeded', http_status=400, provider_code='context_length_exceeded')
    result = agent.run('Большой бриф')
    state = agent.state()
    assert result.code == 'context_length_exceeded'
    assert state['requests'][-1]['metadata']['token_metrics']['new_message_tokens_estimate'] > 0
    assert state['requests'][-1]['metadata']['provider_error']['http_status'] == 400
    assert state['token_accounting']['known_total_tokens'] == 120
    assert not state['token_accounting']['complete']
    assert not state['summary']['cost_complete']
    assert all(m['role'] == 'user' for m in state['messages'])


@pytest.mark.parametrize('status,provider,expected', [(400,'context_length_exceeded','context_length_exceeded'), (429,'insufficient_quota','insufficient_quota'), (429,'rate_limit_exceeded','rate_limit'), (400,'something_else','provider_error')])
def test_error_codes_are_classified_without_exposing_raw_details(status, provider, expected):
    with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(status, json={'error': {'code':provider,'message':'secret'}}))) as client:
        with pytest.raises(TransportError) as error:
            ResponsesTransport('test-key', client=client).create({}, 5)
    assert error.value.code == expected
    assert error.value.http_status == status
    assert 'secret' not in str(error.value)


def test_preview_rejection_does_not_write_or_call():
    transport = FakeTransport()
    agent = Agent(replace(load_config(), max_input_chars=3), transport, SQLiteStore(':memory:'))
    before = agent.state()
    assert agent.preview('four')['code'] == 'input_too_long'
    assert agent.state() == before
    agent.run('four')
    assert agent.state()['token_accounting']['complete']
    assert agent.state()['token_accounting']['known_total_tokens'] == 0
    assert not transport.calls


def test_legacy_metadata_stays_unknown(tmp_path):
    store = SQLiteStore(tmp_path / 'old.db')
    record = {'status':'pending','text':'','code':'','usage':None,'cost_usd':None,'usage_status':'unavailable','input_policy':{},'output_policy':{},'metadata':{}}
    store.begin(record, 'Прежний текст')
    store.close()
    agent = Agent(load_config(), FakeTransport(), SQLiteStore(tmp_path / 'old.db'))
    state = agent.state()
    assert state['requests'][0]['status'] == 'interrupted'
    assert state['requests'][0]['metadata'].get('token_metrics') is None
    assert state['token_accounting']['unknown_requests'] == 1
    agent.close()

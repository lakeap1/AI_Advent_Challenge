from agent import create_agent, load_config
from test_agent import FakeTransport, response


def test_restart_restores_dialogue_and_sends_full_ordered_context(tmp_path):
    path = tmp_path / 'chat.sqlite3'
    first = create_agent(db_path=path, api_key='')
    first._transport = FakeTransport(response('Запомнил Кедр-731.'))
    first.run('Мой код — Кедр-731')
    before = first.state()
    first.close()

    restored = create_agent(db_path=path, api_key='')
    transport = FakeTransport()
    restored._transport = transport
    assert restored.state() == before
    restored.run('Какой мой код?')
    payload = transport.calls[0][0]
    assert payload['input'] == [
        {'role': 'user', 'content': 'Мой код — Кедр-731'},
        {'role': 'assistant', 'content': 'Запомнил Кедр-731.'},
        {'role': 'user', 'content': 'Какой мой код?'},
    ]
    assert payload['instructions'] == load_config().instructions
    assert payload['store'] is False
    assert len(transport.calls) == 1
    restored.close()

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
from threading import Event

import pytest

from agent import Agent, SQLiteStore
from agent.storage import StorageError
from test_usage import billed_response


def test_new_store_has_empty_stable_chat(tmp_path):
    path = tmp_path / 'nested' / 'chat.sqlite3'
    store = SQLiteStore(path)
    first = store.state()
    assert first['chat_id']
    assert first['messages'] == first['requests'] == []
    assert first['summary'] == {'known_cost_usd': '0', 'cost_complete': True,
                                'unknown_cost_requests': 0, 'api_requests': 0}
    store.close()
    reopened = SQLiteStore(path)
    assert reopened.state() == first
    reopened.close()


@pytest.mark.parametrize('prompt', ['', 'x' * 4001, '\ud800'])
def test_rejected_input_persists_policies_without_user_text(tmp_path, prompt):
    path = tmp_path / 'chat.sqlite3'
    transport = FakeTransport()
    agent = Agent(load_config(), transport, SQLiteStore(path))
    result = agent.run(prompt)
    agent.close()
    restored = SQLiteStore(path)
    state = restored.state()
    record = state['requests'][0]
    assert record['id'] == result.request_id
    assert record['input_policy'] == {'name': 'nonempty_text', 'status': 'rejected'}
    assert record['output_policy'] == {'name': 'completed_text', 'status': 'not_checked'}
    assert record['usage_status'] == 'not_requested'
    assert record['status'] == 'rejected'
    assert state['messages'] == []
    assert state['summary']['api_requests'] == 0
    assert state['summary']['cost_complete'] is True
    assert transport.calls == []
    restored.close()


@pytest.mark.parametrize('output', [billed_response(status='incomplete'), billed_response(output=[
    {'type': 'message', 'role': 'assistant', 'status': 'completed',
     'content': [{'type': 'refusal', 'refusal': 'do not publish this'}]}])])
def test_output_rejection_keeps_cost_but_never_assistant(tmp_path, output):
    path = tmp_path / 'chat.sqlite3'
    transport = FakeTransport(output)
    agent = Agent(load_config(), transport, SQLiteStore(path))
    result = agent.run('Вопрос')
    before = agent.state()
    assert result.output_policy == {'name': 'completed_text', 'status': 'rejected'}
    assert [m['role'] for m in before['messages']] == ['user']
    assert before['requests'][0]['cost_usd'] == '0.000368'
    assert before['requests'][0]['usage']['total_tokens'] == 1200
    assert before['summary']['known_cost_usd'] == '0.000368'
    assert len(transport.calls) == 1
    agent.close()
    reopened = Agent(load_config(), FakeTransport(), SQLiteStore(path))
    assert reopened.state() == before
    reopened.run('Продолжим')
    assert reopened._transport.calls[0][0]['input'] == [
        {'role': 'user', 'content': 'Вопрос'}, {'role': 'user', 'content': 'Продолжим'}]
    reopened.close()


def test_historical_tariff_and_mixed_cost_summary_survive_restart(tmp_path):
    path = tmp_path / 'chat.sqlite3'
    config = load_config()
    first = Agent(config, FakeTransport(billed_response()), SQLiteStore(path))
    first.run('Первый')
    historical = first.state()['requests'][0]
    first.close()
    changed = replace(config, pricing=replace(config.pricing, output_usd_per_million='2.00'))
    transport = FakeTransport(billed_response())
    second = Agent(changed, transport, SQLiteStore(path))
    second.run('Второй')
    transport.output = response('Нет usage')
    second.run('Третий')
    second.run('')
    state = second.state()
    assert state['requests'][0] == historical
    assert state['requests'][1]['cost_usd'] == '0.000528'
    assert state['requests'][1]['metadata']['pricing']['output_usd_per_million'] == '2.00'
    assert historical['metadata']['pricing']['output_usd_per_million'] == '1.20'
    assert historical['metadata']['requested_model'] == historical['metadata']['actual_model'] == config.model
    assert historical['metadata']['requested_service_tier'] == historical['metadata']['actual_service_tier'] == 'default'
    assert historical['metadata']['pricing']['checked_at'] == config.pricing.checked_at
    assert historical['metadata']['pricing']['source'] == config.pricing.source
    assert Decimal(state['summary']['known_cost_usd']) == Decimal('0.000896')
    assert state['summary']['unknown_cost_requests'] == 1
    assert state['summary']['api_requests'] == 3
    assert state['summary']['cost_complete'] is False
    second.close()
    restored = Agent(config, FakeTransport(), SQLiteStore(path))
    assert restored.state() == state
    restored.close()


def test_pending_committed_before_api_and_recovered_without_retry(tmp_path):
    path = tmp_path / 'chat.sqlite3'
    class InterruptedTransport:
        def create(self, payload, timeout):
            with sqlite3.connect(path) as connection:
                assert connection.execute('SELECT status FROM requests').fetchone()[0] == 'pending'
                assert connection.execute('SELECT role,content FROM messages').fetchall() == [('user', 'Сохранить факт')]
            raise KeyboardInterrupt
    first = Agent(load_config(), InterruptedTransport(), SQLiteStore(path))
    with pytest.raises(KeyboardInterrupt):
        first.run('Сохранить факт')
    chat_id = first.state()['chat_id']
    first.close()
    transport = FakeTransport()
    restored = Agent(load_config(), transport, SQLiteStore(path))
    state = restored.state()
    assert transport.calls == []
    assert state['chat_id'] == chat_id
    assert state['requests'][0]['status'] == state['requests'][0]['code'] == 'interrupted'
    assert state['requests'][0]['usage'] is state['requests'][0]['cost_usd'] is None
    assert state['requests'][0]['output_policy']['status'] == 'not_checked'
    assert state['summary']['unknown_cost_requests'] == 1
    assert state['summary']['cost_complete'] is False
    assert [m['role'] for m in state['messages']] == ['user']
    restored.close()


def test_failed_save_rolls_back_answer_and_does_not_return_success(tmp_path):
    path = tmp_path / 'chat.sqlite3'
    store = SQLiteStore(path)
    with sqlite3.connect(path) as connection:
        connection.execute("""CREATE TRIGGER reject_assistant BEFORE INSERT ON messages
            WHEN NEW.role='assistant' BEGIN SELECT RAISE(ABORT, 'simulated disk failure'); END""")
    transport = FakeTransport(billed_response())
    agent = Agent(load_config(), transport, store)
    with pytest.raises(StorageError):
        agent.run('Вопрос')
    state = agent.state()
    assert state['requests'][0]['status'] == 'pending'
    assert [m['role'] for m in state['messages']] == ['user']
    assert len(transport.calls) == 1
    agent.close()
    restored = SQLiteStore(path)
    assert restored.state()['requests'][0]['status'] == 'interrupted'
    restored.close()


def test_failure_to_save_pending_prevents_provider_call(tmp_path):
    path = tmp_path / 'chat.sqlite3'
    store = SQLiteStore(path)
    with sqlite3.connect(path) as connection:
        connection.execute("""CREATE TRIGGER reject_user BEFORE INSERT ON messages
            BEGIN SELECT RAISE(ABORT, 'simulated failure'); END""")
    transport = FakeTransport()
    agent = Agent(load_config(), transport, store)
    with pytest.raises(StorageError):
        agent.run('Вопрос')
    assert transport.calls == []
    assert agent.state()['requests'] == agent.state()['messages'] == []
    agent.close()


def test_corrupted_database_is_not_replaced(tmp_path):
    path = tmp_path / 'broken.sqlite3'
    original = b'This is not SQLite. Keep this evidence.'
    path.write_bytes(original)
    with pytest.raises(StorageError):
        SQLiteStore(path)
    assert path.read_bytes() == original


def test_unavailable_database_and_closed_store_are_explicit_errors(tmp_path):
    with pytest.raises(StorageError):
        SQLiteStore(tmp_path)
    store = SQLiteStore(':memory:')
    store.close()
    with pytest.raises(StorageError):
        store.state()


def test_concurrent_runs_are_serial_and_include_completed_first_response(tmp_path):
    entered, release, second_started = Event(), Event(), Event()
    calls = []
    class BlockingTransport:
        def create(self, payload, timeout):
            calls.append(payload)
            if len(calls) == 1:
                entered.set()
                assert release.wait(5)
            return response(f'Ответ {len(calls)}')
    agent = Agent(load_config(), BlockingTransport(), SQLiteStore(tmp_path / 'chat.sqlite3'))
    def second_run():
        second_started.set()
        return agent.run('Второй')
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(agent.run, 'Первый')
        assert entered.wait(5)
        second = executor.submit(second_run)
        assert second_started.wait(5)
        release.set()
        assert first.result(timeout=5).status == second.result(timeout=5).status == 'ok'
    assert len(calls) == 2
    assert calls[1]['input'] == [
        {'role': 'user', 'content': 'Первый'}, {'role': 'assistant', 'content': 'Ответ 1'},
        {'role': 'user', 'content': 'Второй'}]
    assert [m['role'] for m in agent.state()['messages']] == ['user', 'assistant', 'user', 'assistant']
    agent.close()


def test_missing_key_records_no_api_request(tmp_path):
    agent = create_agent(api_key='', db_path=tmp_path / 'chat.sqlite3')
    result = agent.run('Вопрос')
    assert result.code == 'not_configured'
    state = agent.state()
    assert state['requests'][0]['usage_status'] == 'not_requested'
    assert state['summary']['api_requests'] == 0
    assert state['summary']['cost_complete'] is True
    agent.close()

@pytest.mark.parametrize('key,code', [('', 'not_configured'), ('bad\nkey', 'authentication'),
                                    ('bad\x00key', 'authentication'), ('ключ', 'authentication')])
def test_local_key_rejection_is_not_an_api_request_after_reopen(tmp_path, key, code):
    import httpx
    from agent.transport import ResponsesTransport

    path = tmp_path / 'chat.sqlite3'
    network_calls = []
    def handle(request):
        network_calls.append(request)
        return httpx.Response(401)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        agent = Agent(load_config(), ResponsesTransport(key, client=client), SQLiteStore(path))
        result = agent.run('Вопрос')
        state = agent.state()
        agent.close()
    assert network_calls == []
    assert result.code == code
    assert result.usage_status == 'not_requested'
    assert state['requests'][0]['usage_status'] == 'not_requested'
    assert state['summary']['api_requests'] == 0
    assert state['summary']['cost_complete'] is True
    restored = SQLiteStore(path)
    assert restored.state() == state
    restored.close()


@pytest.mark.parametrize('http_status', [401, 403])
def test_provider_authentication_failure_remains_a_started_request(tmp_path, http_status):
    import httpx
    from agent.transport import ResponsesTransport

    path = tmp_path / 'chat.sqlite3'
    network_calls = []
    def handle(request):
        network_calls.append(request)
        return httpx.Response(http_status)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        agent = Agent(load_config(), ResponsesTransport('test-key', client=client), SQLiteStore(path))
        result = agent.run('Вопрос')
        state = agent.state()
        agent.close()
    assert len(network_calls) == 1
    assert result.code == 'authentication'
    assert result.usage_status == 'unavailable'
    assert state['summary']['api_requests'] == 1
    assert state['summary']['cost_complete'] is False
    restored = SQLiteStore(path)
    assert restored.state() == state
    restored.close()

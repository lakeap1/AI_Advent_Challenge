import json
from dataclasses import replace
from agent import Agent, SQLiteStore, load_config
from agent.memory import MemoryStore
from test_strategies import Fake, response
from test_extraction import operation
import pytest


def make(tmp_path, replies, mode='sliding'):
    memory = MemoryStore(tmp_path / 'memory.sqlite3')
    dialogue = memory.create_task('Материал', 'Портрет', mode)
    fake = Fake(replies)
    agent = Agent(replace(load_config(), context_mode=mode), fake, SQLiteStore(tmp_path / 'chat.sqlite3'),
                  memory_store=memory, task_id=dialogue['task_id'], dialogue_id=dialogue['id'])
    return agent, fake, memory, dialogue


def test_auto_extract_before_answer_and_exact_trace(tmp_path):
    agent, fake, memory, dialogue = make(tmp_path, [response(json.dumps({'operations': [operation()]})), response('Проверьте Cycles')])
    result = agent.run('Использую Cycles')
    assert result.status == 'ok' and len(fake.calls) == 2
    assert 'current_message' in fake.calls[0]['input'][0]['content']
    assert 'Слой working' in fake.calls[1]['input'][0]['content']
    assert 'Cycles' in fake.calls[1]['input'][0]['content']
    state = agent.state()
    assert state['summary']['api_requests'] == 2
    assert state['token_accounting']['known_total_tokens'] == 220
    context = next(r for r in state['requests'] if r['id'] == result.request_id)['metadata']['context']
    assert memory.resolve(context['memory_refs'])[0]['value'] == 'Cycles'
    assert [m['role'] for m in state['messages']] == ['user', 'assistant']


def test_failed_extraction_stops_answer_and_keeps_cost(tmp_path):
    agent, fake, memory, dialogue = make(tmp_path, [response(json.dumps({'operations': [operation(evidence='НЕ БЫЛО')]}))])
    result = agent.run('Использую Cycles')
    assert result.code == 'extraction_failed' and len(fake.calls) == 1
    assert agent.state()['summary']['api_requests'] == 1
    assert agent.state()['token_accounting']['known_total_tokens'] == 110
    assert memory.layers(dialogue['task_id'])['working'] == []
    assert all(m['role'] != 'assistant' for m in agent.state()['messages'])


def test_disabled_layers_and_input_rejection(tmp_path):
    agent, fake, memory, dialogue = make(tmp_path, [response('{"operations": []}'), response()])
    memory.save(dialogue['task_id'], {k:v for k,v in operation().items() if k not in ('op','evidence')})
    assert agent.run(' ').status == 'rejected'
    assert not fake.calls
    preview = agent.preview('Что проверить?', use_working=False)
    assert agent.preview('Что проверить?')['token_metrics']['input_text_tokens_estimate'] > preview['token_metrics']['input_text_tokens_estimate']
    assert agent.run('Что проверить?', use_working=False).status == 'ok'
    assert 'Cycles' not in json.dumps(fake.calls[-1]['input'])


@pytest.mark.parametrize('mode', ['sliding', 'facts', 'branching'])
def test_explicit_layers_preserve_each_strategy(tmp_path, mode):
    replies = [response('{"operations": []}')]
    if mode == 'facts':
        replies.append(response('{"свет":"мягкий"}'))
    replies.append(response('Объяснение'))
    agent, fake, memory, dialogue = make(tmp_path, replies, mode)
    memory.save(dialogue['task_id'], {k:v for k,v in operation().items() if k not in ('op','evidence')})
    assert agent.run('Свет мягкий. Что проверить?').status == 'ok'
    assert fake.calls[-1]['input'][0]['content'].startswith('Слой working')
    assert len(fake.calls) == (3 if mode == 'facts' else 2)
    if mode == 'facts':
        assert 'Справочные facts' in fake.calls[-1]['input'][1]['content']
    if mode == 'branching':
        cp = agent.checkpoint('Общий свет')
        agent.branch(cp['id'], 'Вариант')
        assert memory.layers(dialogue['task_id'])['working']


def test_output_rejection_after_extraction_keeps_both_costs(tmp_path):
    agent, fake, memory, dialogue = make(tmp_path, [response('{"operations": []}'), {**response('Недописано'), 'status': 'incomplete'}])
    assert agent.run('Вопрос').status != 'ok'
    state = agent.state()
    assert state['summary']['api_requests'] == 2
    assert state['token_accounting']['known_total_tokens'] == 220
    assert not any(m['role'] == 'assistant' for m in state['messages'])


def test_storage_failure_after_extraction_keeps_reported_usage(tmp_path, monkeypatch):
    from agent.storage import StorageError
    agent, fake, memory, dialogue = make(tmp_path, [response(json.dumps({'operations':[operation()]}))])
    def fail(*args): raise StorageError('Недоступно')
    monkeypatch.setattr(memory, 'apply_operations', fail)
    assert agent.run('Cycles').code == 'extraction_failed'
    assert agent.state()['token_accounting']['known_total_tokens'] == 110
    assert len(fake.calls) == 1

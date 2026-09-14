import json
from dataclasses import replace
import pytest
from agent import Agent, SQLiteStore, load_config


def response(text='Ответ', **overrides):
    return dict(status='completed', model='gpt-5.6-luna', service_tier='default',
        output=[dict(type='message', role='assistant', status='completed',
                     content=[dict(type='output_text', text=text)])],
        usage=dict(input_tokens=100, output_tokens=10, total_tokens=110,
                   input_tokens_details=dict(cached_tokens=0, cache_write_tokens=0),
                   output_tokens_details=dict(reasoning_tokens=0)), **overrides)


class Fake:
    def __init__(self, replies=None):
        self.calls = []
        self.replies = list(replies or [])
    def create(self, payload, timeout):
        self.calls.append(payload)
        return self.replies.pop(0) if self.replies else response()


def make(mode='sliding', replies=None, path=':memory:'):
    fake = Fake(replies)
    return Agent(replace(load_config(), context_mode=mode), fake, SQLiteStore(path)), fake


def test_sliding_payload_excludes_archive_and_counts_current_question():
    agent, fake = make()
    for i in range(6):
        assert agent.run(f'Вопрос {i}').status == 'ok'
    sent = fake.calls[-1]['input']
    assert len(sent) == 6
    assert sent[-1]['content'] == 'Вопрос 5'
    assert 'Вопрос 0' not in json.dumps(sent, ensure_ascii=False)
    assert len(agent.state()['messages']) == 12
    assert fake.calls[-1]['store'] is False
    assert 'previous_response_id' not in fake.calls[-1]
    assert 'без Markdown' in fake.calls[-1]['instructions']


def test_facts_updated_before_answer_and_old_value_replaced():
    agent, fake = make('facts', [response('{"цвет":"синий"}'), response(),
                                response('{"цвет":"красный"}'), response()])
    agent.run('Цвет синий')
    agent.run('Теперь красный')
    assert agent.state()['memory']['facts'] == {'цвет': 'красный'}
    assert agent.state()['memory']['revisions'] == 2
    assert json.loads(fake.calls[2]['input'][0]['content'])['facts'] == {'цвет': 'синий'}
    assert 'красный' in fake.calls[3]['input'][0]['content']
    assert agent.state()['summary']['api_requests'] == 4
    assert agent.state()['token_accounting']['known_total_tokens'] == 440


@pytest.mark.parametrize('bad', ['не JSON', '[]', '{"a": 1}', '{"a":"x","a":"y"}', '{"a":"' + 'x'*501 + '"}'])
def test_invalid_facts_stops_answer_and_preserves_usage(bad):
    agent, fake = make('facts', [response(bad)])
    result = agent.run('Расскажи про UV')
    assert result.status != 'ok'
    assert len(fake.calls) == 1
    assert agent.state()['memory']['facts'] == {}
    assert agent.state()['summary']['api_requests'] == 1
    assert agent.state()['token_accounting']['known_total_tokens'] == 110
    assert all(m['role'] != 'assistant' for m in agent.state()['messages'])


def test_branches_checkpoint_is_immutable_and_usage_not_duplicated(tmp_path):
    path = tmp_path / 'chat.sqlite3'
    agent, fake = make('branching', path=path)
    agent.run('Общая цель')
    cp = agent.checkpoint('Старт')
    agent.run('Только в исходной')
    a = agent.branch(cp['id'], 'A')
    agent.run('Ветка A')
    b = agent.branch(cp['id'], 'B')
    agent.run('Ветка B')
    assert 'Ветка A' not in json.dumps(fake.calls[-1], ensure_ascii=False)
    assert 'Только в исходной' not in json.dumps(fake.calls[-1], ensure_ascii=False)
    agent.switch(a['id'])
    assert [m['content'] for m in agent.state()['messages'] if m['role'] == 'user'] == ['Общая цель', 'Ветка A']
    assert agent.state()['summary']['api_requests'] == 4
    agent.close()
    restored, _ = make('branching', path=path)
    assert restored.state()['active_branch'] == a['id']
    assert len(restored.state()['checkpoints']) == 1
    restored.switch(b['id'])
    assert restored.state()['messages'][-2]['content'] == 'Ветка B'


def test_rejected_input_and_invalid_branch_never_call_api():
    agent, fake = make()
    assert agent.run(' ').status == 'rejected'
    assert len(fake.calls) == 0
    assert agent.state()['requests'][0]['usage_status'] == 'not_requested'
    with pytest.raises(ValueError): agent.checkpoint('x')


def test_facts_restore_and_window(tmp_path):
    agent, fake = make('facts', [response('{"цель":"свет"}'), response()], tmp_path / 'f.db')
    agent.run('Цель свет'); agent.close()
    agent, fake = make('facts', [response('{"цель":"свет"}'), response()], tmp_path / 'f.db')
    agent.run('Как начать?')
    assert 'свет' in fake.calls[-1]['input'][0]['content']


def test_rejected_output_never_becomes_history_and_keeps_cost():
    bad = response()
    bad['status'] = 'incomplete'
    agent, fake = make(replies=[bad])
    assert agent.run('UV').status != 'ok'
    assert len(agent.state()['messages']) == 1
    assert agent.state()['summary']['known_cost_usd'] != '0'

def test_preview_separates_current_question_from_selected_history():
    agent, transport = make()
    agent.run('Сохрани раннее условие')
    question = 'Почему тень мягкая?'
    metrics = agent.preview(question)['token_metrics']
    assert metrics['new_message_tokens_estimate'] == agent._counter.count(question)
    assert metrics['history_tokens_estimate'] == agent._counter.history(agent.state()['messages'])
    assert metrics['input_text_tokens_estimate'] == sum(metrics[key] for key in ('new_message_tokens_estimate', 'history_tokens_estimate', 'instructions_tokens_estimate'))

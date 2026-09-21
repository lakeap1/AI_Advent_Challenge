import json

import pytest

from app import create_app
from agent import load_config
from profiles import ProfileWorkspace
from test_strategies import Fake, response
from test_task_state import fields
from state_helpers import apply_checked, transition


@pytest.mark.parametrize('mode', ['sliding', 'facts', 'branching'])
@pytest.mark.parametrize('answer_format', ['plain', 'steps', 'markdown'])
def test_resume_new_dialogue_has_state_profile_and_accounting(tmp_path, mode, answer_format):
    replies = [response('{"operations": []}')]
    if mode == 'facts':
        replies.append(response('{}'))
    replies.append(response('Проверьте один параметр roughness.'))
    fake = Fake(replies)
    ws = ProfileWorkspace(tmp_path, fake)
    ws.edit_profile(dict(style='Кратко', format=answer_format, constraints='Только Cycles'))
    apply_checked(ws.memory, **fields())
    transition(ws.memory, 'execution', current_step='Сравнить блик', expected_action='Изменить roughness на 0.4')
    ws.memory.task_action(1, dict(action='pause'))
    blocked = ws.agent().run('Продолжим?')
    assert blocked.code == 'task_paused' and blocked.usage_status == 'not_requested'
    assert not fake.calls
    assert ws.agent().preview('Продолжим?')['code'] == 'task_paused'
    ws.close()
    ws = ProfileWorkspace(tmp_path, fake)
    ws.memory.task_action(1, dict(action='resume'))
    ws.memory.create_dialogue('Продолжение', 1, mode)
    result = ws.agent().run('Что сделать сейчас?', use_working=False, use_long_term=False)
    assert result.status == 'ok'
    instructions = next(c['instructions'] for c in fake.calls if 'TASK_RESPONSE_JSON' in c['instructions'])
    assert '{{ANSWER_FORMAT}}' not in instructions
    assert ('без Markdown' in instructions) == (answer_format != 'markdown')
    assert ('##' in instructions) == (answer_format == 'markdown')
    config = load_config()
    assert all(call['model'] == config.model for call in fake.calls)
    assert fake.calls[-1]['reasoning']['effort'] == config.reasoning_effort
    for value in ['TASK_STATE', 'Кратко', 'Только Cycles', 'Убрать пластиковый вид керамики',
                  'Сравнить блик', 'Изменить roughness на 0.4', 'свет пока не менять']:
        assert value in instructions
    assert 'TASK_STATE' not in fake.calls[0]['instructions']
    if mode == 'facts':
        assert 'TASK_STATE' not in fake.calls[1]['instructions']
    record = next(r for r in ws.state()['requests'] if r['id'] == result.request_id)
    assert record['metadata']['context']['task_state']['stage'] == ws.state()['task_state']['stage']
    assert ws.state()['summary']['api_requests'] == len(replies) + 1
    assert ws.state()['token_accounting']['known_total_tokens'] == 110 * (len(replies) + 1)
    assert len(ws.state()['messages']) == 2
    ws.close()


def test_api_state_actions_profiles_and_stale_identity(tmp_path):
    fake = Fake([])
    app = create_app(data_dir=tmp_path, transport=fake)
    client = app.test_client()
    def action(**data):
        return client.post('/api/task-state/action', json=dict(task_id=1, profile_id=1, **data))
    apply_checked(app.extensions['workspace'].memory, **fields())
    assert action(action='save', **fields()).status_code == 400
    original = client.get('/api/state').json['task_state']
    for data in [dict(task_id=2,profile_id=1,action='pause'), dict(task_id=1,profile_id=2,action='pause'),
                 dict(task_id=True,profile_id=1,action='pause'),dict(task_id=1,action='pause')]:
        assert client.post('/api/task-state/action',json=data).status_code == 400
        assert client.get('/api/state').json['task_state'] == original
    assert action(action='pause').status_code == 200
    assert client.post('/api/ask', json=dict(prompt='Продолжим?',task_id=1,profile_id=1)).json['code'] == 'task_paused'
    client.post('/api/profiles', json=dict(name='Другой'))
    assert action(action='resume').status_code == 400
    assert client.get('/api/state').json['task_state']['paused'] is False
    assert client.post('/api/ask',json=dict(prompt='X',task_id=1,profile_id=1)).status_code == 400
    client.post('/api/profiles/select',json=dict(profile_id=1))
    assert action(action='resume').status_code == 200
    assert client.get('/api/state').json['task_state'] == original
    assert not fake.calls
    app.extensions['workspace'].close()


def test_policy_rejection_never_changes_stage_but_keeps_billed_usage(tmp_path):
    fake = Fake([response('{"operations": []}'), {**response('Отклонённый текст'), 'status': 'incomplete'}])
    ws = ProfileWorkspace(tmp_path, fake)
    before = ws.state()['task_state']
    assert ws.agent().run(' ').status == 'rejected'
    assert not fake.calls
    result = ws.agent().run('Помоги с материалом')
    assert result.status != 'ok' and result.usage is not None and result.cost_usd is not None
    assert ws.state()['task_state'] == before
    assert ws.state()['summary']['api_requests'] == 2
    assert ws.state()['token_accounting']['known_total_tokens'] == 220
    assert all(m['role'] != 'assistant' for m in ws.state()['messages'])
    ws.close()


def test_model_done_text_does_not_transition_and_terminal_blocks_calls(tmp_path):
    fake = Fake([response('{"operations": []}'), response('Задача завершена.')])
    ws = ProfileWorkspace(tmp_path, fake)
    assert ws.agent().run('Что думаешь?').status == 'ok'
    assert ws.state()['task_state']['stage'] == 'planning'
    apply_checked(ws.memory, **fields())
    for target in ['execution', 'validation', 'done']:
        transition(ws.memory, target, current_step='Шаг', expected_action='Действие')
    assert ws.agent().run('Продолжай').code == 'task_done'
    assert len(fake.calls) == 2
    ws.close()

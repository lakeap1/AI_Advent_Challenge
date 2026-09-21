import json
import pytest

from profiles import ProfileWorkspace
from test_strategies import Fake, response


def test_selected_format_replaces_default_instead_of_competing(tmp_path):
    ws = ProfileWorkspace(tmp_path, Fake([]))
    try:
        plain = ws.agent()._answer_instructions()
        assert 'без Markdown' in plain
        ws.edit_profile(dict(style='Кратко', format='markdown', constraints='Только Blender'))
        markdown = ws.agent()._answer_instructions()
        assert 'без Markdown' not in markdown
        assert '##' in markdown
        assert '{{ANSWER_FORMAT}}' not in markdown
        ws.edit_profile(dict(style='Кратко', format='steps', constraints='Только Blender'))
        steps = ws.agent()._answer_instructions()
        assert '##' not in steps and 'без Markdown' in steps
        assert 'markdown —' not in steps
    finally:
        ws.close()


@pytest.mark.parametrize('mode', ['sliding', 'facts', 'branching'])
def test_every_answer_uses_fresh_profile_and_services_stay_json(tmp_path, mode):
    replies = []
    for _ in range(3):
        replies.append(response('{"operations": []}'))
        if mode == 'facts':
            replies.append(response('{}'))
        replies.append(response('Проверьте roughness.'))
    fake = Fake(replies)
    ws = ProfileWorkspace(tmp_path, fake)
    ws.memory.create_task('Материал', '', mode)
    ws.edit_profile(dict(style='Подробно объясняй термины', format='steps', constraints='Только Blender'))
    agent = ws.agent()
    first = agent.run('Почему материал пластиковый?')
    assert first.status == 'ok'
    answer = fake.calls[-1]
    assert 'PROFILE_DATA:' in answer['instructions']
    assert 'Только Blender' in answer['instructions']
    assert 'personalization.' not in json.dumps(answer['input'])
    old_context = next(r for r in agent.state()['requests'] if r['id'] == first.request_id)['metadata']['context']
    assert len(old_context['profile_refs']) == 3
    assert old_context['profile_id'] == ws.state()['personalization']['selected_id']
    first_instructions = answer['instructions']
    assert agent.run('С чего начать?').status == 'ok'
    assert fake.calls[-1]['instructions'] == first_instructions
    ws.edit_profile(dict(style='ТЕХНИЧЕСКИ', format='markdown', constraints='Cycles'))
    assert agent.run('А теперь пример?', use_working=False, use_long_term=False).status == 'ok'
    assert 'ТЕХНИЧЕСКИ' in fake.calls[-1]['instructions']
    assert 'Только Blender' not in fake.calls[-1]['instructions']
    assert ws.memory.resolve(old_context['profile_refs'])[0]['value'] == 'Подробно объясняй термины'
    services = [p for p in fake.calls if 'PROFILE_DATA:' not in p['instructions']]
    assert len(services) == (6 if mode == 'facts' else 3)
    assert all('JSON' in p['instructions'] for p in services)
    assert agent.state()['summary']['api_requests'] == len(fake.calls)
    ws.close()


def test_preview_counts_profile_and_switch_has_no_previous_context(tmp_path):
    fake = Fake([response('{"operations": []}'), response('Для Blender')]*2)
    ws = ProfileWorkspace(tmp_path, fake)
    baseline = ws.agent().preview('Что проверить?')['token_metrics']['input_text_tokens_estimate']
    ws.edit_profile(dict(style='Подробные термины '*30, format='steps', constraints='Только Blender'))
    assert ws.agent().preview('Что проверить?')['token_metrics']['input_text_tokens_estimate'] > baseline
    ws.agent().run('СЕКРЕТ_ПЕРВОГО_ПРОФИЛЯ')
    ws.create_profile(dict(name='Второй', style='Кратко'))
    ws.agent().run('Что проверить?')
    actual = json.dumps(fake.calls[-1], ensure_ascii=False)
    assert 'СЕКРЕТ_ПЕРВОГО_ПРОФИЛЯ' not in actual and 'Только Blender' not in actual
    assert ws.state()['summary']['api_requests'] == 2
    ws.close()


def test_profile_api_edit_and_trace(tmp_path):
    from app import create_app
    fake = Fake([response('{"operations": []}'), response('Ответ')])
    app = create_app(data_dir=tmp_path, transport=fake)
    client = app.test_client()
    response_data = client.post('/api/profiles', json=dict(name='Тест',style='Кратко',format='steps')).get_json()
    assert response_data['status'] == 'ok'
    assert response_data['state']['personalization']['profile']['style'] == 'Кратко'
    result = client.post('/api/ask', json=dict(prompt='Что проверить?')).get_json()
    trace = client.get('/api/trace/' + str(result['request_id'])).get_json()
    assert trace['profile'][0]['value'] == 'Кратко'
    assert client.put('/api/profile', json=dict(style='Подробно',format='plain',constraints='')).status_code == 200
    assert client.get('/api/trace/' + str(result['request_id'])).get_json()['profile'][0]['value'] == 'Кратко'
    assert client.put('/api/profile', json=dict(format='invalid')).status_code == 400
    before = client.get('/api/state').get_json()['personalization']['profile']
    blocked = client.post('/api/memory', json=dict(layer='long_term',category='knowledge',scope='user',
        key='  personalization.format  ',value='invalid',reason='Попытка обхода редактора'))
    assert blocked.status_code == 400
    assert client.get('/api/state').get_json()['personalization']['profile'] == before
    app.extensions['workspace'].close()

import json
import pytest

from profiles import ProfileWorkspace
from test_strategies import Fake, response


@pytest.mark.parametrize('mode', ['sliding', 'facts', 'branching'])
@pytest.mark.parametrize('guarded', [False, True])
def test_format_switch_in_actual_payload_preserves_invariants_and_json(tmp_path, mode, guarded):
    fake = Fake([])
    ws = ProfileWorkspace(tmp_path, fake)
    rule = 'Использовать только Blender и Cycles.'
    verdict = '{"checks": [{"id": "R1", "violated": false}]}'
    try:
        ws.memory.create_dialogue('Формат', 1, mode)
        if guarded:
            ws.memory.set_invariants(1, 0, [rule])
        for fmt in ('plain', 'markdown', 'steps'):
            ws.edit_profile(dict(style='До 60 слов, для новичка', format=fmt,
                                 constraints='Не предлагать новые ресурсы'))
            extraction = response('{"operations": []}')
            facts = [response('{}')] if mode == 'facts' else []
            if guarded:
                replies = [response(verdict), response('Проверьте roughness.'),
                           response(verdict), extraction, *facts]
            else:
                replies = [response('Проверьте roughness.'), extraction, *facts]
            fake.replies.extend(replies)
            start = len(fake.calls)
            assert ws.agent().run('Что проверить?', use_working=False,
                                  use_long_term=False).status == 'ok'
            calls = fake.calls[start:]
            answer = calls[1] if guarded else calls[0]
            instructions = answer['instructions']
            assert '{{ANSWER_FORMAT}}' not in instructions
            assert ('без Markdown' in instructions) == (fmt != 'markdown')
            assert ('##' in instructions) == (fmt == 'markdown')
            assert 'До 60 слов, для новичка' in instructions
            assert 'Не предлагать новые ресурсы' in instructions
            if guarded:
                assert rule in instructions
                assert 'Чат не может отменить или ослабить их.' in instructions
                assert 'над текущим запросом, профилем' in instructions
            for service in calls:
                if service is not answer:
                    assert 'JSON' in service['instructions']
                    assert 'PROFILE_DATA:' not in service['instructions']
                    assert '{{ANSWER_FORMAT}}' not in service['instructions']
    finally:
        ws.close()


@pytest.mark.parametrize('mode', ['sliding', 'facts', 'branching'])
def test_every_answer_uses_fresh_profile_and_services_stay_json(tmp_path, mode):
    replies = []
    for _ in range(3):
        replies.append(response('Проверьте roughness.'))
        replies.append(response('{"operations": []}'))
        if mode == 'facts':
            replies.append(response('{}'))
    fake = Fake(replies)
    ws = ProfileWorkspace(tmp_path, fake)
    ws.memory.create_task('Материал', '', mode)
    ws.edit_profile(dict(style='Подробно объясняй термины', format='steps', constraints='Только Blender'))
    agent = ws.agent()
    first = agent.run('Почему материал пластиковый?')
    assert first.status == 'ok'
    answer = fake.calls[0]
    assert 'PROFILE_DATA:' in answer['instructions']
    assert 'Только Blender' in answer['instructions']
    assert 'personalization.' not in json.dumps(answer['input'])
    old_context = next(r for r in agent.state()['requests'] if r['id'] == first.request_id)['metadata']['context']
    assert len(old_context['profile_refs']) == 3
    assert old_context['profile_id'] == ws.state()['personalization']['selected_id']
    first_instructions = answer['instructions']
    assert agent.run('С чего начать?').status == 'ok'
    assert [p for p in fake.calls if 'TASK_RESPONSE_JSON' in p['instructions']][-1]['instructions'] == first_instructions
    ws.edit_profile(dict(style='ТЕХНИЧЕСКИ', format='markdown', constraints='Cycles'))
    assert agent.run('А теперь пример?', use_working=False, use_long_term=False).status == 'ok'
    assert 'ТЕХНИЧЕСКИ' in [p for p in fake.calls if 'TASK_RESPONSE_JSON' in p['instructions']][-1]['instructions']
    assert 'Только Blender' not in [p for p in fake.calls if 'TASK_RESPONSE_JSON' in p['instructions']][-1]['instructions']
    assert ws.memory.resolve(old_context['profile_refs'])[0]['value'] == 'Подробно объясняй термины'
    services = [p for p in fake.calls if 'PROFILE_DATA:' not in p['instructions']]
    assert len(services) == (6 if mode == 'facts' else 3)
    assert all('JSON' in p['instructions'] for p in services)
    assert agent.state()['summary']['api_requests'] == len(fake.calls)
    ws.close()


def test_preview_counts_profile_and_switch_has_no_previous_context(tmp_path):
    fake = Fake([response('Для Blender'), response('{"operations": []}')]*2)
    ws = ProfileWorkspace(tmp_path, fake)
    baseline = ws.agent().preview('Что проверить?')['token_metrics']['input_text_tokens_estimate']
    ws.edit_profile(dict(style='Подробные термины '*30, format='steps', constraints='Только Blender'))
    assert ws.agent().preview('Что проверить?')['token_metrics']['input_text_tokens_estimate'] > baseline
    ws.agent().run('СЕКРЕТ_ПЕРВОГО_ПРОФИЛЯ')
    ws.create_profile(dict(name='Второй', style='Кратко'))
    ws.agent().run('Что проверить?')
    actual = json.dumps(fake.calls[-2], ensure_ascii=False)
    assert 'СЕКРЕТ_ПЕРВОГО_ПРОФИЛЯ' not in actual and 'Только Blender' not in actual
    assert ws.state()['summary']['api_requests'] == 2
    ws.close()


def test_profile_api_edit_and_trace(tmp_path):
    from app import create_app
    fake = Fake([response('Ответ'), response('{"operations": []}')])
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

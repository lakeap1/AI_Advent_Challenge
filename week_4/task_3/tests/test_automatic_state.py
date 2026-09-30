"""Автоматические события проверяются без ручного переключения этапов."""
import json

import pytest

from profiles import ProfileWorkspace
from test_strategies import response
from state_helpers import report_for, apply_checked, prepare_saved_plan, transition


class RawTransport:
    def __init__(self, envelopes):
        self.envelopes = list(envelopes)
        self.calls = []

    def create(self, payload, timeout):
        self.calls.append(payload)
        if 'TASK_STAGE_VALIDATION' in payload['instructions']:
            data = json.loads(payload['input'][0]['content'])
            return response(json.dumps(report_for(data, passed=data['requested_event'] == 'result_reported', confirmed=data['requested_event'] == 'finish')))
        if 'TASK_RESPONSE_JSON' not in payload['instructions']:
            return response('{"operations": []}')
        return response(self.envelopes.pop(0))


def envelope(event='stay', evidence='', **changes):
    data = dict(answer='Сравните ширину блика при неизменном свете.', event=event,
                evidence=evidence, goal='Убрать пластиковый вид керамики',
                current_step='Сравнить roughness 0.2 и 0.4',
                expected_action='Сообщите результат сравнения', notes='Свет не менять.',
                plan=[] if event == 'revise_plan' else [dict(action='Сравнить roughness 0.2 и 0.4',
                      criterion='Описать изменение ширины блика при том же свете')])
    return json.dumps({**data, **changes}, ensure_ascii=False)


def test_automatic_lifecycle_pause_restart_new_dialogue(tmp_path):
    fake = RawTransport([envelope('stay', 'Керамика'),
                         envelope('plan_ready', 'Подтверждаю сохранённый план.'),
                         envelope('result_reported', 'Блик стал шире'),
                         envelope('finish', 'Результат устраивает')])
    ws = ProfileWorkspace(tmp_path, fake)
    result = ws.agent().run('Керамика выглядит пластиковой, помоги проверить roughness.')
    assert result.status == 'ok' and result.text.startswith('Сравните')
    assert ws.state()['task_state']['stage'] == 'planning'
    result = ws.agent().run('Подтверждаю сохранённый план.')
    assert result.status == 'ok'
    assert ws.state()['task_state']['stage'] == 'execution'
    assert ws.state()['task_state']['goal'] == 'Убрать пластиковый вид керамики'
    ws.memory.task_action(1, {'action': 'pause'})
    assert ws.agent().run('Продолжай').code == 'task_paused'
    assert len(fake.calls) == 5
    ws.close()
    ws = ProfileWorkspace(tmp_path, fake)
    assert ws.state()['task_state']['paused']
    ws.memory.task_action(1, {'action': 'resume'})
    ws.memory.create_dialogue('Продолжение', 1, 'sliding')
    assert ws.agent().run('Блик стал шире, выглядит лучше.').status == 'ok'
    assert ws.state()['task_state']['stage'] == 'validation'
    assert 'roughness 0.2' in next(c['instructions'] for c in reversed(fake.calls) if 'TASK_RESPONSE_JSON' in c['instructions'])
    assert ws.agent().run('Результат устраивает, можно завершить.').status == 'ok'
    state = ws.state()
    assert state['task_state']['stage'] == 'done'
    assert state['task_state']['expected_action'] == 'Действий не ожидается.'
    assert ws.agent().run('Ещё вопрос').code == 'task_done'
    assert len(fake.calls) == 11
    assert all(not m['content'].startswith('{') for m in state['messages'] if m['role'] == 'assistant')
    last = next(r for r in state['requests'] if r['metadata'].get('state_update', {}).get('event') == 'finish')
    assert last['metadata']['state_update']['before']['stage'] == 'validation'
    assert last['metadata']['state_update']['after']['stage'] == 'done'
    ws.close()


@pytest.mark.parametrize('bad', [
    'Обычный текст без машинного контракта', '[]',
    envelope('finish', 'Проверь'), envelope('unknown'),
    envelope('plan_ready', 'вымышленная цитата'),
    envelope(goal=''), envelope(notes='x'*6001), envelope(event=[]),
    envelope(stage='done'), envelope(paused=True),
    envelope().replace('"event": "stay"', '"event": "stay", "event": "finish"'),
])
def test_invalid_envelope_does_not_mutate_and_accounts_cost(tmp_path, bad):
    fake = RawTransport([bad])
    ws = ProfileWorkspace(tmp_path, fake)
    before = ws.state()['task_state']
    result = ws.agent().run('Проверь керамику.')
    assert result.status == 'rejected' and result.code == 'invalid_task_response'
    assert result.usage is not None and result.cost_usd is not None
    assert result.output_policy['status'] == 'rejected'
    assert ws.state()['task_state'] == before
    assert len(fake.calls) == 1
    assert ws.state()['summary']['api_requests'] == 1
    assert not any(m['role'] == 'assistant' for m in ws.state()['messages'])
    ws.close()


def test_stale_state_is_not_overwritten(tmp_path):
    from agent.task_protocol import parse_task_response
    ws = ProfileWorkspace(tmp_path, RawTransport([]))
    prepare_saved_plan(ws.memory, goal='Убрать пластиковый вид керамики',
                       current_step='Сравнить roughness 0.2 и 0.4',
                       expected_action='Сообщите результат сравнения', notes='Свет не менять.',
                       plan=[dict(action='Сравнить roughness 0.2 и 0.4',
                                  criterion='Описать изменение ширины блика при том же свете')])
    old = ws.state()['task_state']
    update = parse_task_response(envelope('plan_ready', 'Проверь'), old, 'Проверь керамику.')
    ws.memory.task_action(1, {'action': 'pause'})
    with pytest.raises(ValueError):
        ws.memory.apply_event(1, update, old)
    assert ws.state()['task_state']['paused']
    ws.close()


def test_public_api_has_only_pause_resume(tmp_path):
    from app import create_app
    app = create_app(data_dir=tmp_path, transport=RawTransport([]))
    client = app.test_client()
    for action in ('save', 'transition'):
        result = client.post('/api/task-state/action', json=dict(task_id=1, profile_id=1, action=action))
        assert result.status_code == 400
        assert 'автоматически' in result.json['text']
    app.extensions['workspace'].close()


def test_replan_and_rework_are_automatic_and_notes_persist(tmp_path):
    turns = [('План готов', 'stay', 'planning'),
             ('Подтверждаю план', 'plan_ready', 'execution'),
             ('Изменим цель', 'revise_plan', 'planning'),
             ('Другой план', 'stay', 'planning'),
             ('Подтверждаю другой план', 'plan_ready', 'execution'),
             ('Получил результат', 'result_reported', 'validation'),
             ('Нужна ещё попытка', 'revise_work', 'execution')]
    fake = RawTransport([envelope(event, prompt, notes='Свет фиксирован; наблюдение ' + prompt)
                         for prompt, event, stage in turns])
    ws = ProfileWorkspace(tmp_path, fake)
    for prompt, event, stage in turns:
        assert ws.agent().run(prompt).status == 'ok'
        assert ws.state()['task_state']['stage'] == stage
        assert prompt in ws.state()['task_state']['notes']
    saved = ws.state()['task_state']
    ws.close()
    ws = ProfileWorkspace(tmp_path, fake)
    assert ws.state()['task_state'] == saved
    ws.close()


def test_early_completion_event_and_fabricated_result_are_rejected(tmp_path):
    fake = RawTransport([envelope('result_reported', 'Я уже сделал рендер')])
    ws = ProfileWorkspace(tmp_path, fake)
    from test_task_state import fields
    apply_checked(ws.memory, **fields())
    transition(ws.memory, 'execution', current_step='Сделать проверку', expected_action='Результат пользователя')
    before = ws.state()['task_state']
    assert ws.agent().run('Я ещё не делал рендер. Что менять?').code == 'invalid_task_response'
    assert ws.state()['task_state'] == before
    ws.close()

"""Готовность к выполнению проверяется по данным плана, а не названию события."""
import json
import sqlite3

import pytest

from agent.task_protocol import parse_task_response
from agent.task_state import StateMemoryStore
from profiles import ProfileWorkspace
from test_automatic_state import RawTransport, envelope


PLAN = [dict(action='Сравнить две тональные схемы', criterion='Назвать главный акцент и объяснить контраст')]


@pytest.mark.parametrize('plan', [None, {}, 'План готов', [], [None], ['Действие'],
    [dict(action='Сравнить')], [dict(action=' ', criterion='Проверить')],
    [dict(action='Сравнить', criterion='')], [dict(action='X', criterion=42)],
    [dict(action='X', criterion='Y', extra=True)],
    [dict(action='x' * 501, criterion='Y')], [dict(action='X', criterion='\ud800')], PLAN * 7])
def test_plan_ready_rejects_bad_plan_and_retains_cost(tmp_path, plan):
    # ensure_ascii=True позволяет передать некорректный Unicode как JSON escape.
    bad = json.loads(envelope('plan_ready', 'Композиция'))
    bad['plan'] = plan
    fake = RawTransport([json.dumps(bad, ensure_ascii=True)])
    ws = ProfileWorkspace(tmp_path, fake)
    before = ws.state()['task_state']
    result = ws.agent().run('Композиция: помоги разобраться.')
    assert result.code == 'invalid_task_response'
    assert result.output_policy['status'] == 'rejected'
    assert result.usage and result.cost_usd is not None
    assert ws.state()['task_state'] == before
    assert ws.state()['summary']['api_requests'] == len(fake.calls) == 1
    assert not any(m['role'] == 'assistant' for m in ws.state()['messages'])
    ws.close()


def test_plan_required_not_just_event_name(tmp_path):
    data = json.loads(envelope('plan_ready', 'Композиция'))
    data.pop('plan', None)
    ws = ProfileWorkspace(tmp_path, RawTransport([json.dumps(data)]))
    assert ws.agent().run('Композиция').code == 'invalid_task_response'
    assert ws.state()['task_state']['stage'] == 'planning'
    ws.close()


def test_plan_persists_after_restart_and_new_dialogue(tmp_path):
    fake = RawTransport([envelope('plan_ready', 'Композиция', plan=PLAN), envelope(plan=PLAN)])
    ws = ProfileWorkspace(tmp_path, fake)
    assert ws.agent().run('Композиция').status == 'ok'
    saved = ws.state()['task_state']
    assert saved['stage'] == 'execution' and saved['plan'] == PLAN
    ws.memory.task_action(1, {'action': 'pause'})
    ws.close()
    ws = ProfileWorkspace(tmp_path, fake)
    ws.memory.task_action(1, {'action': 'resume'})
    ws.memory.create_dialogue('Другая история', 1, 'sliding')
    assert ws.state()['task_state'] == saved
    assert ws.agent().run('Что дальше?', use_working=False, use_long_term=False).status == 'ok'
    assert PLAN[0]['criterion'] in next(c['instructions'] for c in reversed(fake.calls) if 'TASK_RESPONSE_JSON' in c['instructions'])
    ws.memory.create_task('Новая задача')
    assert ws.state()['task_state']['plan'] == []
    ws.close()


def test_replanning_requires_new_plan_and_cannot_erase_active_plan(tmp_path):
    fake = RawTransport([envelope('stay', plan=[]), envelope('plan_ready', 'Начать', plan=PLAN),
        envelope('stay', plan=[]), envelope('revise_plan', 'Пересмотрим', plan=[]),
        envelope('plan_ready', 'Начать', plan=[]), envelope('plan_ready', 'Начать', plan=PLAN)])
    ws = ProfileWorkspace(tmp_path, fake)
    assert ws.agent().run('Уточни задачу').status == 'ok'
    assert ws.state()['task_state']['stage'] == 'planning'
    assert ws.agent().run('Начать').status == 'ok'
    assert ws.agent().run('Что дальше?').code == 'invalid_task_response'
    assert ws.state()['task_state']['plan'] == PLAN
    assert ws.agent().run('Пересмотрим').status == 'ok'
    assert ws.state()['task_state']['plan'] == []
    assert ws.agent().run('Начать').code == 'invalid_task_response'
    assert ws.state()['task_state']['stage'] == 'planning'
    assert ws.agent().run('Начать').status == 'ok'
    ws.close()


def test_storage_guard_cannot_be_bypassed(tmp_path):
    ws = ProfileWorkspace(tmp_path, RawTransport([]))
    old = ws.state()['task_state']
    update = json.loads(envelope('plan_ready', 'Начать', plan=[]))
    with pytest.raises(ValueError):
        ws.memory.apply_event(1, update, old)
    with pytest.raises(ValueError):
        ws.memory.task_action(1, dict(action='transition', target='execution',
            current_step='Шаг', expected_action='Проверить'))
    assert ws.state()['task_state'] == old
    ws.close()


def test_old_schema_upgrade_preserves_state_and_allows_inserts(tmp_path):
    path = tmp_path / 'old.sqlite3'
    store = StateMemoryStore(path)
    store.create_task('Старая задача')
    store.close()
    with sqlite3.connect(path) as db:
        # Точная форма прежней таблицы, без новой колонки и с прежним этапом.
        db.execute('DROP TABLE task_states')
        db.execute('''CREATE TABLE task_states(task_id INTEGER PRIMARY KEY REFERENCES tasks(id),
            goal TEXT NOT NULL, stage TEXT NOT NULL, current_step TEXT NOT NULL,
            expected_action TEXT NOT NULL, notes TEXT NOT NULL, paused INTEGER NOT NULL)''')
        db.execute("INSERT INTO task_states VALUES(1,'Цель','execution','Шаг','Действие','Заметки',1)")
    store = StateMemoryStore(path)
    saved = store.task_state(1)
    assert saved['stage'] == 'execution' and saved['paused'] is True
    assert saved['notes'] == 'Заметки' and saved['plan'] == []
    store.create_task('Новая задача')
    assert store.task_state(2)['plan'] == []
    store.close()
    store = StateMemoryStore(path)
    assert store.task_state(1) == saved
    store.close()

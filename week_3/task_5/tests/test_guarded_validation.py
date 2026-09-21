"""Взаимодействие готовности этапов с инвариантами и отложенной памятью."""
import json
import sqlite3

import pytest

from agent import load_config
from agent.task_state import StateMemoryStore
from agent.task_validation import pending_steps
from profiles import ProfileWorkspace
from state_helpers import apply_checked, prepare_saved_plan
from test_automatic_state import envelope
from test_guarded_state import checks, RULES
from test_strategies import response


PLAN = [dict(action='Сравнить тональные отношения', criterion='Назвать главный акцент'),
        dict(action='Объяснить контраст', criterion='Обосновать выбор акцента')]
PROMPT = 'Главный акцент — светлый силуэт.'


class StrictSequence:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def create(self, payload, timeout):
        self.calls.append(payload)
        return self.replies.pop(0)


def review(marker='Светлый силуэт'):
    steps = pending_steps(PLAN)
    steps[0].update(status='passed', result=marker, evidence=PROMPT, reason='Акцент назван')
    return dict(plan_ready=True, steps=steps, completion_confirmed=False, reason='Нужно объяснить контраст')


@pytest.mark.parametrize('mode', ['sliding', 'facts', 'branching'])
@pytest.mark.parametrize('conflict', [False, True])
def test_partial_results_commit_only_after_invariant_gate(tmp_path, mode, conflict):
    marker = 'СЕКРЕТНЫЙ запрещённый цвет' if conflict else 'Светлый силуэт'
    fake = StrictSequence([checks(), response(envelope('result_reported', PROMPT, plan=PLAN)),
                           response(json.dumps(review(marker), ensure_ascii=False)), checks(conflict)])
    ws = ProfileWorkspace(tmp_path, fake)
    prepare_saved_plan(ws.memory, plan=PLAN, goal='Убрать пластиковый вид керамики')
    apply_checked(ws.memory, 'plan_ready', plan=PLAN, goal='Убрать пластиковый вид керамики')
    ws.memory.set_invariants(1, 0, RULES)
    ws.memory.create_dialogue('Продолжение', 1, mode)
    ws.edit_profile(dict(style='Кратко', format='steps', constraints='Объяснять теорию'))
    before = ws.state()['task_state']
    layers, facts, profile = ws.memory.layers(1), ws.agent()._store.memory(), ws.agent()._profile()
    result = ws.agent().run(PROMPT)
    assert result.code == ('invariant_output_conflict' if conflict else 'stage_not_ready')
    after = ws.state()['task_state']
    assert after['stage'] == before['stage'] == 'execution'
    assert ws.memory.layers(1) == layers and ws.agent()._store.memory() == facts
    assert ws.agent()._profile() == profile
    assert not any(m['role'] == 'assistant' for m in ws.state()['messages'])
    assert len(fake.calls) == ws.state()['summary']['api_requests'] == 4
    assert ws.state()['token_accounting']['known_total_tokens'] == 440
    assert ws.state()['summary']['cost_complete']
    validator_input = json.loads(fake.calls[2]['input'][0]['content'])
    assert set(validator_input) == {'current_message', 'goal', 'plan', 'stage', 'requested_event', 'previous_results'}
    assert validator_input['plan'] == PLAN
    guard = json.loads(fake.calls[3]['input'][0]['content'])
    assert json.loads(guard['text'])['validation']['steps'][0]['result'] == marker
    config = load_config()
    assert all(c['model'] == config.model and c['reasoning']['effort'] == config.reasoning_effort for c in fake.calls)
    if conflict:
        assert after == before
        assert marker not in json.dumps(ws.state(), ensure_ascii=False)
    else:
        assert [s['status'] for s in after['step_results']] == ['passed', 'pending']
        assert after['validation']['allowed'] is False
        for key in ('goal', 'plan', 'notes', 'current_step', 'expected_action'):
            assert after[key] == before[key]
    ws.close()
    restored = ProfileWorkspace(tmp_path, StrictSequence([]))
    assert restored.state()['task_state'] == after
    assert len(restored.state()['invariants']['rules']) == 5
    restored.close()


def test_input_conflict_does_not_invoke_evaluator(tmp_path):
    fake = StrictSequence([checks(True)])
    ws = ProfileWorkspace(tmp_path, fake)
    prepare_saved_plan(ws.memory, plan=PLAN)
    apply_checked(ws.memory, 'plan_ready', plan=PLAN)
    ws.memory.set_invariants(1, 0, RULES)
    before = ws.state()['task_state']
    assert ws.agent().run('Добавь красный акцент').code == 'invariant_input_conflict'
    assert ws.state()['task_state'] == before and len(fake.calls) == 1
    ws.close()


def test_validation_parse_interruption_keeps_usage_without_unchecked_report(tmp_path, monkeypatch):
    from agent import core
    report = dict(plan_ready=True, steps=pending_steps(PLAN), completion_confirmed=False, reason='НЕПРОВЕРЕННЫЙ ОТЧЁТ')
    fake = StrictSequence([checks(), response(envelope('plan_ready', 'Композиция', plan=PLAN)),
                           response(json.dumps(report, ensure_ascii=False))])
    ws = ProfileWorkspace(tmp_path, fake)
    ws.memory.set_invariants(1, 0, RULES)
    draft = json.loads(envelope('stay', 'Композиция', plan=PLAN))
    prepare_saved_plan(ws.memory, **{key: draft[key] for key in
                                     ('goal', 'current_step', 'expected_action', 'notes', 'plan')})
    before = ws.state()['task_state']
    def interrupt(*args):
        raise KeyboardInterrupt()
    monkeypatch.setattr(core, 'parse_validation', interrupt)
    with pytest.raises(KeyboardInterrupt):
        ws.agent().run('Композиция')
    ws.close()
    ws = ProfileWorkspace(tmp_path, StrictSequence([]))
    record = next(r for r in ws.state()['requests'] if r['metadata']['kind'] == 'validation')
    assert record['status'] == 'interrupted'
    assert record['usage']['total_tokens'] == 110 and record['cost_usd'] is not None
    assert ws.state()['summary']['unknown_cost_requests'] == 0
    assert ws.state()['task_state'] == before
    assert report['reason'] not in json.dumps(ws.state(), ensure_ascii=False)
    ws.close()


def test_legacy_plan_gains_pending_without_rewriting_old_stage(tmp_path):
    path = tmp_path / 'legacy.sqlite3'
    store = StateMemoryStore(path)
    store.create_task('Композиция')
    store.close()
    with sqlite3.connect(path) as db:
        db.execute('ALTER TABLE task_states DROP COLUMN step_results')
        db.execute('ALTER TABLE task_states DROP COLUMN validation')
        db.execute("UPDATE task_states SET plan=?,stage='validation',paused=1,notes='Прежние решения'",
                   (json.dumps(PLAN),))
    store = StateMemoryStore(path)
    state = store.task_state(1)
    assert state['step_results'] == pending_steps(PLAN) and state['validation'] is None
    assert state['stage'] == 'validation' and state['paused']
    assert state['notes'] == 'Прежние решения'
    store.close()
    store = StateMemoryStore(path)
    assert store.task_state(1) == state
    store.close()

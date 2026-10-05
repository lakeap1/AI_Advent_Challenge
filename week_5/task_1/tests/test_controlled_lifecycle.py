"""Проверки переходов на явных отчётах; не оценка качества модели."""
import json
from copy import deepcopy

import pytest

from agent.task_state import StateMemoryStore, FIELDS
from agent.task_validation import pending_steps, validation_input


PLAN = [dict(action='Сравнить мягкость тени', criterion='Объяснить влияние размера источника')]
PROMPT = 'Утверждаю этот план.'


@pytest.fixture
def store(tmp_path):
    value = StateMemoryStore(tmp_path / 'memory.sqlite3')
    value.create_task('Мягкость тени')
    yield value
    value.close()


def update_for(state, event='stay', **changes):
    return dict(answer='Ответ', event=event, evidence=PROMPT,
                **{k: state[k] for k in (*FIELDS, 'plan')}, **changes)


def save_plan(store):
    state = store.task_state(1)
    update = update_for(state)
    update['plan'] = deepcopy(PLAN)
    store.apply_event(1, update, state, prompt=PROMPT)
    return store.task_state(1)


def approve(store):
    state = store.task_state(1)
    update = update_for(state, 'plan_ready')
    data = validation_input(state, update, PROMPT)
    report = dict(plan_ready=True, plan_approved=True, approval_evidence=PROMPT,
                  steps=pending_steps(state['plan']), completion_confirmed=False, reason='План принят.')
    return store.apply_event(1, update, state, prompt=PROMPT, receipt=dict(input=data, report=report))


def test_saved_plan_approval_survives_pause_and_restart(store, tmp_path):
    save_plan(store)
    approved = approve(store)
    assert approved['stage'] == 'execution'
    assert approved['plan_approval']['evidence'] == PROMPT
    assert approved['plan_approval']['digest']
    store.task_action(1, dict(action='pause'))
    paused = store.task_state(1)
    reopened = StateMemoryStore(tmp_path / 'memory.sqlite3')
    assert reopened.task_state(1) == paused
    with pytest.raises(ValueError):
        reopened.apply_event(1, update_for(paused), paused, prompt=PROMPT)
    assert reopened.task_state(1) == paused
    reopened.task_action(1, dict(action='resume'))
    assert reopened.task_state(1) == approved
    reopened.close()


def test_plan_proposal_cannot_approve_itself(store):
    before = store.task_state(1)
    update = update_for(before, 'plan_ready')
    update['plan'] = PLAN
    with pytest.raises(ValueError, match='план'):
        store.apply_event(1, update, before, prompt=PROMPT)
    assert store.task_state(1) == before


def test_missing_explicit_approval_rejected_even_when_plan_ready(store):
    state = save_plan(store)
    update = update_for(state, 'plan_ready')
    data = validation_input(state, update, 'Начинай без согласования.')
    update['evidence'] = 'Начинай без согласования.'
    report = dict(plan_ready=True, plan_approved=False, approval_evidence='',
                  steps=pending_steps(PLAN), completion_confirmed=False, reason='Утверждения нет.')
    with pytest.raises(ValueError, match='утвержд'):
        store.apply_event(1, update, state, prompt=data['current_message'], receipt=dict(input=data, report=report))
    assert store.task_state(1)['stage'] == 'planning'
    assert store.task_state(1)['plan_approval'] is None


def test_revised_goal_or_plan_cannot_use_current_approval(store):
    save_plan(store)
    state = store.task_state(1)
    update = update_for(state, 'plan_ready')
    update['plan'] = [dict(action='Другой шаг', criterion='Другой критерий')]
    with pytest.raises(ValueError, match='план'):
        store.apply_event(1, update, state, prompt=PROMPT)
    assert store.task_state(1) == state


def test_revise_plan_clears_approval(store):
    save_plan(store)
    state = approve(store)
    update = update_for(state, 'revise_plan')
    update['plan'] = []
    after = store.apply_event(1, update, state, prompt=PROMPT)
    assert after['stage'] == 'planning'
    assert after['plan_approval'] is None
    assert after['validation'] is None


@pytest.mark.parametrize('event', ['finish', 'result_reported', 'unknown', 'done', None, {}])
def test_illegal_transition_keeps_every_state_field(store, event):
    state = save_plan(store)
    with pytest.raises(ValueError):
        store.apply_event(1, update_for(state, event), state, prompt=PROMPT)
    assert store.task_state(1) == state


def test_stale_receipt_cannot_approve_new_message(store):
    state = save_plan(store)
    update = update_for(state, 'plan_ready')
    report = dict(plan_ready=True, plan_approved=True, approval_evidence=PROMPT,
                  steps=pending_steps(PLAN), completion_confirmed=False, reason='Готов.')
    receipt = dict(input=validation_input(state, update, 'Другой запрос'), report=report)
    with pytest.raises(ValueError, match='другому'):
        store.apply_event(1, update, state, prompt=PROMPT, receipt=receipt)
    assert store.task_state(1) == state

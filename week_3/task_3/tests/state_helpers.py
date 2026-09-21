"""Явные фикстуры проверяющего: не имитируют оценку смысла моделью."""
import json
from agent.task_state import FIELDS
from agent.task_validation import pending_steps, validation_input, needs_validation


def report_for(data, *, passed=False, confirmed=False):
    steps = data['previous_results'] or pending_steps(data['plan'])
    if passed:
        steps = [dict(step=i + 1, status='passed', result='Зафиксирован результат сравнения',
                      evidence=data['current_message'], reason='Тестовый результат соответствует критерию')
                 for i in range(len(data['plan']))]
    return dict(plan_ready=True, steps=steps, completion_confirmed=confirmed, reason='Проверка по тестовому сценарию')


def apply_checked(store, event='stay', **changes):
    before = store.task_state(1)
    prompt = 'Блик стал шире. Результат устраивает.'
    update = dict(answer='Ответ', event=event, evidence=prompt,
                  **{key: before[key] for key in (*FIELDS, 'plan')})
    update.update(changes)
    if event == 'revise_plan':
        update['plan'] = []
    receipt = None
    if needs_validation(before, update):
        data = validation_input(before, update, prompt)
        receipt = dict(input=data, report=report_for(data, passed=event in ('result_reported', 'finish'), confirmed=event == 'finish'))
    return store.apply_event(1, update, before, prompt=prompt, receipt=receipt)


def transition(store, target, **changes):
    events = {('planning','execution'): 'plan_ready', ('execution','planning'): 'revise_plan',
              ('execution','validation'): 'result_reported', ('validation','execution'): 'revise_work',
              ('validation','done'): 'finish'}
    return apply_checked(store, events[(store.task_state(1)['stage'],target)], **changes)

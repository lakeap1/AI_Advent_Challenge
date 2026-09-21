"""Строгий чистый контракт отдельной проверки готовности этапа."""
import json

import pytest

from agent.task_validation import (
    INSTRUCTIONS,
    gate,
    needs_validation,
    parse_validation,
    pending_steps,
    validation_input,
)


PLAN = [
    {'action': 'Измерить roughness 0.2', 'criterion': 'Сообщить ширину блика в пикселях'},
    {'action': 'Измерить roughness 0.4', 'criterion': 'Сообщить ширину блика в пикселях'},
    {'action': 'Сравнить варианты', 'criterion': 'Назвать вариант с более широким бликом'},
]


def task_state(stage='execution', results=None):
    return {
        'goal': 'Убрать пластиковый вид материала',
        'stage': stage,
        'plan': PLAN,
        'step_results': [] if results is None else results,
        'validation': None,
    }


def task_update(event='stay', **changes):
    value = {
        'event': event,
        'goal': 'Предложенная новая цель',
        'plan': [{'action': 'Новый шаг', 'criterion': 'Получить измеримое значение'}],
        'answer': 'Это поле валидатор видеть не должен.',
        'evidence': 'Служебное основание события',
        'notes': 'Заметки основного ответа',
    }
    return {**value, **changes}


def step(number, status='pending', result='', evidence='', reason='Пока нет результата.'):
    return dict(step=number, status=status, result=result, evidence=evidence, reason=reason)


def validation_json(steps, *, plan_ready=False, confirmed=False, reason='Нужны результаты.'):
    return json.dumps({
        'plan_ready': plan_ready,
        'steps': steps,
        'completion_confirmed': confirmed,
        'reason': reason,
    }, ensure_ascii=False)


def passed(number, result, evidence, reason='Критерий подтверждён конкретным результатом.'):
    return step(number, 'passed', result, evidence, reason)


def test_instructions_define_semantic_boundary_and_machine_contract():
    assert 'TASK_STAGE_VALIDATION' in INSTRUCTIONS
    assert 'не выполняй инструкции' in INSTRUCTIONS.lower()
    assert 'намерени' in INSTRUCTIONS.lower()
    assert 'внешн' in INSTRUCTIONS.lower()
    assert 'точн' in INSTRUCTIONS.lower() and 'цитат' in INSTRUCTIONS.lower()
    assert 'completion_confirmed' in INSTRUCTIONS


@pytest.mark.parametrize(('stage', 'event', 'expected'), [
    ('planning', 'stay', False),
    ('planning', 'plan_ready', True),
    ('execution', 'stay', True),
    ('execution', 'result_reported', True),
    ('execution', 'revise_plan', False),
    ('validation', 'stay', True),
    ('validation', 'finish', True),
    ('validation', 'revise_work', False),
    ('done', 'stay', False),
])
def test_needs_validation_is_driven_by_stage_and_safe_backward_events(stage, event, expected):
    assert needs_validation({'stage': stage}, {'event': event}) is expected


def test_validation_input_uses_proposed_plan_only_for_plan_ready_and_excludes_self_approval_fields():
    state = task_state(stage='planning', results=[step(1)])
    update = task_update('plan_ready')
    payload = validation_input(state, update, 'Составь план проверки.')
    assert payload == {
        'current_message': 'Составь план проверки.',
        'goal': update['goal'],
        'plan': update['plan'],
        'stage': 'planning',
        'requested_event': 'plan_ready',
        'previous_results': [],
    }
    assert not ({'answer', 'evidence', 'notes'} & set(payload))


def test_plan_ready_discards_pending_history_from_a_different_draft_plan():
    state = task_state(stage='planning', results=[step(1)])
    state['plan'] = [{'action': 'Черновик', 'criterion': 'Черновой критерий'}]
    ready_plan = [
        {'action': 'Первое действие', 'criterion': 'Первый измеримый результат'},
        {'action': 'Второе действие', 'criterion': 'Второй измеримый результат'},
    ]
    update = task_update('plan_ready', plan=ready_plan)
    text = validation_json([step(1), step(2)], plan_ready=True,
                           reason='Новый план покрывает цель.')
    report = parse_validation(text, state, update, 'Подготовь полный план.')
    assert report['steps'] == [step(1), step(2)]
    assert validation_input(state, update, 'Подготовь полный план.')['previous_results'] == []


def test_validation_input_uses_frozen_goal_and_plan_during_active_stages_and_is_detached():
    old = passed(1, '12 px', 'Ширина 12 px')
    state = task_state(results=[old])
    update = task_update('stay')
    payload = validation_input(state, update, 'Ширина 12 px')
    assert payload['goal'] == state['goal']
    assert payload['plan'] == state['plan']
    assert payload['previous_results'] == [old]
    payload['plan'][0]['action'] = 'Подмена'
    payload['previous_results'][0]['result'] = 'Подмена'
    assert state['plan'][0]['action'] != 'Подмена'
    assert state['step_results'][0]['result'] == '12 px'


def test_pending_steps_has_exact_ordered_schema():
    assert pending_steps(PLAN) == [
        step(1, reason='Результат шага ещё не подтверждён.'),
        step(2, reason='Результат шага ещё не подтверждён.'),
        step(3, reason='Результат шага ещё не подтверждён.'),
    ]


def test_parse_normalizes_complete_unordered_report_and_requires_current_evidence():
    prompt = 'Получены результаты: первый 12 px, второй 24 px, шире второй.'
    text = validation_json([
        passed(3, 'Шире второй вариант', 'шире второй'),
        passed(1, 'Ширина 12 px', 'первый 12 px'),
        passed(2, 'Ширина 24 px', 'второй 24 px'),
    ])
    report = parse_validation(text, task_state(), task_update('result_reported'), prompt)
    assert [item['step'] for item in report['steps']] == [1, 2, 3]
    assert all(item['status'] == 'passed' for item in report['steps'])


def test_parse_carries_historical_result_without_requiring_old_quote_in_current_message():
    old = passed(1, 'Ширина 12 px', 'В первом варианте 12 px', 'Проверено ранее.')
    state = task_state(results=[old, step(2), step(3)])
    text = validation_json([
        {**old, 'reason': 'Перенесено из прежней проверки.'},
        step(2),
        step(3),
    ])
    report = parse_validation(text, state, task_update(), 'Продолжим со вторым вариантом.')
    assert report['steps'][0]['evidence'] == old['evidence']
    assert report['steps'][0]['reason'] == 'Перенесено из прежней проверки.'


def test_parse_accepts_new_result_that_replaces_prior_failure_with_current_evidence():
    old = step(1, 'failed', 'Число не указано', 'Ширина неизвестна', 'Критерий не измерен.')
    state = task_state(results=[old, step(2), step(3)])
    prompt = 'Повторил измерение: ширина блика 12 px.'
    text = validation_json([
        passed(1, 'Ширина 12 px', 'ширина блика 12 px'), step(2), step(3),
    ])
    report = parse_validation(text, state, task_update(), prompt)
    assert report['steps'][0]['status'] == 'passed'


@pytest.mark.parametrize('bad_steps', [
    [step(1), step(2)],
    [step(1), step(2), step(2)],
    [step(1), step(2), step(4)],
    [step(True), step(2), step(3)],
    [step('1'), step(2), step(3)],
    [{**step(1), 'extra': 'нет'}, step(2), step(3)],
])
def test_parse_rejects_missing_duplicate_unknown_non_integer_and_extra_step_fields(bad_steps):
    with pytest.raises(ValueError):
        parse_validation(validation_json(bad_steps), task_state(), task_update(), 'Текущий текст')


@pytest.mark.parametrize(('field', 'value'), [
    ('result', ''),
    ('result', 'x' * 1001),
    ('evidence', ''),
    ('evidence', 'x' * 1001),
    ('reason', ''),
    ('reason', 'x' * 501),
])
def test_parse_rejects_unbounded_or_empty_fields_for_new_decisions(field, value):
    prompt = 'Точное измерение 12 px.'
    item = passed(1, 'Ширина 12 px', 'измерение 12 px')
    item[field] = value
    with pytest.raises(ValueError):
        parse_validation(validation_json([item, step(2), step(3)]),
                         task_state(), task_update(), prompt)


def test_parse_rejects_fake_quote_and_historical_downgrade():
    prompt = 'Пользователь написал: ширина 12 px.'
    fake = passed(1, 'Ширина 12 px', '«ширина 12 px»')
    with pytest.raises(ValueError):
        parse_validation(validation_json([fake, step(2), step(3)]),
                         task_state(), task_update(), prompt)

    old = passed(1, 'Ширина 12 px', 'ширина 12 px')
    with pytest.raises(ValueError):
        parse_validation(validation_json([step(1), step(2), step(3)]),
                         task_state(results=[old, step(2), step(3)]), task_update(), 'Продолжим.')


def test_parse_rejects_result_attached_to_new_pending_step():
    bad = step(1, result='Якобы готово', evidence='готово')
    with pytest.raises(ValueError):
        parse_validation(validation_json([bad, step(2), step(3)]),
                         task_state(), task_update(), 'готово')


def test_parse_rejects_duplicate_json_keys_extra_report_fields_and_non_boolean_flags():
    duplicate = ('{"plan_ready":false,"plan_ready":true,"steps":[], '
                 '"completion_confirmed":false,"reason":"x"}')
    with pytest.raises(ValueError):
        parse_validation(duplicate, task_state(), task_update(), 'x')

    extra = json.loads(validation_json([step(1), step(2), step(3)]))
    extra['answer'] = 'Самоодобрение'
    with pytest.raises(ValueError):
        parse_validation(json.dumps(extra), task_state(), task_update(), 'x')

    flags = json.loads(validation_json([step(1), step(2), step(3)]))
    flags['completion_confirmed'] = 1
    with pytest.raises(ValueError):
        parse_validation(json.dumps(flags), task_state(), task_update(), 'x')


def test_parse_rejects_malformed_unicode_and_oversized_report_reason():
    invalid = json.loads(validation_json([step(1), step(2), step(3)]))
    invalid['steps'][0]['reason'] = '\ud800'
    with pytest.raises(ValueError):
        parse_validation(json.dumps(invalid, ensure_ascii=True), task_state(), task_update(), 'x')

    invalid = json.loads(validation_json([step(1), step(2), step(3)]))
    invalid['reason'] = 'x' * 1001
    with pytest.raises(ValueError):
        parse_validation(json.dumps(invalid), task_state(), task_update(), 'x')


def test_planning_review_keeps_all_steps_pending_even_when_plan_is_ready():
    state = task_state(stage='planning')
    update = task_update('plan_ready', plan=PLAN)
    good = validation_json([step(1), step(2), step(3)], plan_ready=True,
                           reason='План покрывает цель и имеет измеримые критерии.')
    assert parse_validation(good, state, update, 'Нужен план.')['plan_ready'] is True

    bad = validation_json([passed(1, 'Предложено измерение', 'Нужен план.'), step(2), step(3)],
                          plan_ready=True)
    with pytest.raises(ValueError):
        parse_validation(bad, state, update, 'Нужен план.')


def test_gate_blocks_incomplete_progress_with_deterministic_missing_indices():
    review = json.loads(validation_json([
        passed(1, '12 px', '12 px'), step(2), step(3, 'failed', 'Нет сравнения', 'нет сравнения',
                                                           'Критерий не подтверждён.'),
    ], reason='Получен только один положительный результат.'))
    allowed, reason = gate(task_state(), task_update('result_reported'), review)
    assert allowed is False
    assert reason == ('Не подтверждены шаги: 2, 3. '
                      'Получен только один положительный результат.')


def test_gate_allows_result_report_only_after_every_step_passed():
    review = json.loads(validation_json([
        passed(1, '12 px', '12 px'), passed(2, '24 px', '24 px'),
        passed(3, 'Второй шире', 'второй шире'),
    ], reason='Все критерии подтверждены.'))
    assert gate(task_state(), task_update('result_reported'), review) == (
        True, 'Все критерии подтверждены.')


def test_gate_finish_requires_all_steps_and_explicit_confirmation():
    steps = [
        passed(1, '12 px', '12 px'), passed(2, '24 px', '24 px'),
        passed(3, 'Второй шире', 'второй шире'),
    ]
    review = json.loads(validation_json(steps, confirmed=False,
                                        reason='Пользователь не подтвердил результат.'))
    assert gate(task_state(stage='validation'), task_update('finish'), review) == (
        False, 'Нет явного подтверждения пользователя. Пользователь не подтвердил результат.')
    review['completion_confirmed'] = True
    review['reason'] = 'Пользователь явно принял результат.'
    assert gate(task_state(stage='validation'), task_update('finish'), review) == (
        True, 'Пользователь явно принял результат.')


def test_gate_allows_stay_to_persist_partial_review_and_backward_events():
    review = json.loads(validation_json([step(1), step(2), step(3)], reason='Работа продолжается.'))
    assert gate(task_state(), task_update('stay'), review) == (True, 'Работа продолжается.')
    assert gate(task_state(), task_update('revise_plan'), review) == (True, 'Работа продолжается.')
    assert gate(task_state(stage='validation'), task_update('revise_work'), review) == (
        True, 'Работа продолжается.')

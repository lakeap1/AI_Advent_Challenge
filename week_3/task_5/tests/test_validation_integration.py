import json
from dataclasses import replace
import pytest
from profiles import ProfileWorkspace
from agent.task_state import StageNotReady
from agent.task_validation import pending_steps, validation_input
from agent.transport import TransportError
from test_automatic_state import RawTransport, envelope
from test_strategies import response
from state_helpers import apply_checked, prepare_saved_plan

PLAN = [dict(action=f'Проверить {i}', criterion=f'Измерение {i} должно быть 10') for i in range(1, 4)]
PROMPT = 'Измерение 1 = 10; измерение 2 = 10; измерение 3 = 10'


def review(count=0, *, confirmed=False):
    steps = pending_steps(PLAN)
    for i in range(count):
        steps[i].update(status='passed', result=f'Измерение {i + 1} равно 10',
                        evidence=PROMPT.split('; ')[i], reason='Критерий достигнут')
    return dict(plan_ready=True, steps=steps, completion_confirmed=confirmed, reason='Проверены предоставленные измерения')


def save_envelope_plan(workspace, **changes):
    draft = json.loads(envelope('stay', 'Начать', plan=PLAN, **changes))
    prepare_saved_plan(workspace.memory, **{key: draft[key] for key in
                                             ('goal', 'current_step', 'expected_action', 'notes', 'plan')})


def approval_review(prompt='Начать'):
    return {**review(), 'plan_approved': True, 'approval_evidence': prompt}


class ReviewedTransport(RawTransport):
    def __init__(self, envelopes, reports):
        super().__init__(envelopes)
        self.reports = list(reports)
    def create(self, payload, timeout):
        if 'TASK_STAGE_VALIDATION' in payload['instructions']:
            self.calls.append(payload)
            item = self.reports.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return super().create(payload, timeout)


def test_partial_progress_blocks_transition_persists_and_then_completes(tmp_path):
    messages = [envelope('plan_ready', 'Начать', plan=PLAN),
                envelope('result_reported', 'Измерение 1 = 10', plan=PLAN),
                envelope('result_reported', 'измерение 2 = 10', plan=PLAN),
                envelope('finish', 'Устраивает', plan=PLAN),
                envelope('finish', 'Устраивает', plan=PLAN)]
    fake = ReviewedTransport(messages, [response(json.dumps(r)) for r in
        [approval_review(), review(1), review(3), review(3), review(3, confirmed=True)]])
    ws = ProfileWorkspace(tmp_path, fake)
    save_envelope_plan(ws)
    assert ws.agent().run('Начать').status == 'ok'
    before = ws.state()['task_state']
    result = ws.agent().run('Измерение 1 = 10')
    assert result.code == 'stage_not_ready'
    saved = ws.state()['task_state']
    assert saved['stage'] == 'execution'
    assert [s['status'] for s in saved['step_results']] == ['passed', 'pending', 'pending']
    assert saved['current_step'] == before['current_step']
    assert '2, 3' in result.text
    assert ws.state()['summary']['api_requests'] == 5
    assert len([m for m in ws.state()['messages'] if m['role'] == 'assistant']) == 1
    ws.close()
    ws = ProfileWorkspace(tmp_path, fake)
    ws.memory.create_dialogue('Продолжение', 1, 'sliding')
    assert ws.state()['task_state'] == saved
    assert ws.agent().run(PROMPT).status == 'ok'
    assert ws.state()['task_state']['stage'] == 'validation'
    assert ws.agent().run('Устраивает').code == 'stage_not_ready'
    assert ws.state()['task_state']['stage'] == 'validation'
    assert ws.agent().run('Устраивает').status == 'ok'
    assert ws.state()['task_state']['stage'] == 'done'
    assert len(fake.calls) == 13
    ws.close()


@pytest.mark.parametrize('bad', [response('not JSON'), response('{}'),
    {**response('{}'), 'status': 'incomplete'}, TransportError('timeout')])
def test_checker_failure_preserves_state_accounts_both_calls_and_never_retries(tmp_path, bad):
    fake = ReviewedTransport([envelope('plan_ready', 'Начать', plan=PLAN)], [bad])
    ws = ProfileWorkspace(tmp_path, fake)
    save_envelope_plan(ws)
    before = ws.state()['task_state']
    result = ws.agent().run('Начать')
    assert result.code == 'validation_failed'
    assert result.usage is not None
    assert ws.state()['task_state'] == before
    assert len(fake.calls) == 2
    records = ws.state()['requests']
    checker = next(r for r in records if r['metadata']['kind'] == 'validation')
    assert checker['metadata']['parent_request_id'] == result.request_id
    assert checker['usage_status'] == ('unavailable' if isinstance(bad, Exception) else 'reported')
    assert not any(m['role'] == 'assistant' for m in ws.state()['messages'])
    ws.close()


def test_storage_requires_current_receipt_and_forbids_manual_bypass(tmp_path):
    ws = ProfileWorkspace(tmp_path, RawTransport([]))
    save_envelope_plan(ws)
    before = ws.state()['task_state']
    update = json.loads(envelope('plan_ready', 'Начать', plan=PLAN))
    for data in [dict(action='transition', target='execution'), dict(action='save'),
                 dict(action='pause', stage='done')]:
        with pytest.raises(ValueError): ws.memory.task_action(1, data)
    with pytest.raises(ValueError): ws.memory.apply_event(1, update, before, prompt='Начать')
    data = validation_input(before, update, 'Другое сообщение')
    with pytest.raises(ValueError):
        ws.memory.apply_event(1, update, before, prompt='Начать', receipt=dict(input=data, report=review()))
    assert ws.state()['task_state'] == before
    ws.close()


def test_criteria_and_goal_cannot_change_to_bypass_unfinished_work(tmp_path):
    fake = ReviewedTransport([envelope('result_reported', 'Измерение', plan=PLAN[:1]),
                              envelope('result_reported', 'Измерение', plan=PLAN, goal='Проще')], [])
    ws = ProfileWorkspace(tmp_path, fake)
    prepare_saved_plan(ws.memory, plan=PLAN)
    apply_checked(ws.memory, 'plan_ready', plan=PLAN)
    before = ws.state()['task_state']
    for _ in range(2):
        assert ws.agent().run('Измерение').code == 'invalid_task_response'
        assert ws.state()['task_state'] == before
    assert len(fake.calls) == 2  # malformed proposal stops before evaluator
    ws.close()


def test_validation_input_policy_stops_extra_call(tmp_path):
    fake = ReviewedTransport([envelope('plan_ready', 'Начать', plan=PLAN)], [])
    ws = ProfileWorkspace(tmp_path, fake)
    save_envelope_plan(ws)
    agent = ws.agent()
    agent._config = replace(agent._config, validation_max_input_chars=10)
    assert agent.run('Начать').code == 'validation_failed'
    assert len(fake.calls) == 1
    checker = next(r for r in agent.state()['requests'] if r['metadata']['kind'] == 'validation')
    assert checker['usage_status'] == 'not_requested'
    assert checker['input_policy']['status'] == 'rejected'
    ws.close()


def test_legacy_validation_without_plan_can_return_to_planning(tmp_path):
    ws = ProfileWorkspace(tmp_path, RawTransport([]))
    with ws.memory.transaction() as db:
        db.execute("UPDATE task_states SET stage='validation' WHERE task_id=1")
    apply_checked(ws.memory, 'revise_work')
    assert ws.state()['task_state']['stage'] == 'execution'
    apply_checked(ws.memory, 'revise_plan')
    assert ws.state()['task_state']['stage'] == 'planning'
    ws.close()



def test_validation_dispatches_its_own_policies(tmp_path):
    from agent.core import AgentResult, nonempty_text
    fake = ReviewedTransport([envelope('plan_ready', 'Начать', plan=PLAN)], [response(json.dumps(approval_review()))])
    ws = ProfileWorkspace(tmp_path, fake)
    save_envelope_plan(ws)
    agent = ws.agent()
    observed = []
    def own_input(text, config):
        observed.append('input')
        return nonempty_text(text, config)
    def own_output(response):
        observed.append('output')
        return AgentResult('rejected', 'Отклонено отдельной политикой', 'separate_policy')
    agent._validation_input_policy = own_input
    agent._validation_output_policy = own_output
    assert agent.run('Начать').code == 'validation_failed'
    assert observed == ['input', 'output']
    record = next(r for r in agent.state()['requests'] if r['metadata']['kind'] == 'validation')
    assert record['code'] == 'separate_policy' and record['usage_status'] == 'reported'
    assert agent.state()['task_state']['stage'] == 'planning'
    ws.close()


def test_validation_missing_usage_is_unknown_not_free(tmp_path):
    raw = response(json.dumps(approval_review()))
    raw.pop('usage')
    fake = ReviewedTransport([envelope('plan_ready', 'Начать', plan=PLAN)], [raw])
    ws = ProfileWorkspace(tmp_path, fake)
    save_envelope_plan(ws)
    assert ws.agent().run('Начать').status == 'ok'
    record = next(r for r in ws.state()['requests'] if r['metadata']['kind'] == 'validation')
    assert record['usage'] is None and record['cost_usd'] is None
    assert record['usage_status'] == 'unavailable'
    assert ws.state()['summary']['cost_complete'] is False
    ws.close()

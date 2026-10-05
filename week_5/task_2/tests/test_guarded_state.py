import json

import pytest

from profiles import ProfileWorkspace
from test_automatic_state import envelope
from test_strategies import response
from state_helpers import prepare_saved_plan


RULES = ['Только ахроматические тона.', 'Три тональные группы.', 'Сохранить асимметрию.',
         'Не добавлять новые объекты.', 'Не предлагать настройки программ.']
PLAN = [dict(action='Сравнить тональные отношения', criterion='Объяснить главный акцент')]


def save_candidate_plan(workspace, **changes):
    draft = json.loads(envelope('stay', 'Композиция', **changes))
    prepare_saved_plan(workspace.memory, **{key: draft[key] for key in
                                             ('goal', 'current_step', 'expected_action', 'notes', 'plan')})


def checks(conflict=False):
    return response(json.dumps({'checks': [dict(id=f'R{i+1}', violated=conflict and i == 0)
                                          for i in range(5)]}))


class Sequence:
    def __init__(self, replies):
        self.replies, self.calls = replies, []

    def create(self, payload, timeout):
        self.calls.append(payload)
        if 'TASK_STAGE_VALIDATION' in payload['instructions']:
            from state_helpers import report_for
            return response(json.dumps(report_for(json.loads(payload['input'][0]['content']))))
        return self.replies.pop(0)


@pytest.mark.parametrize('field', ['answer', 'notes', 'plan'])
def test_conflicting_proposal_never_commits_any_state_or_memory(tmp_path, field):
    changes = dict(answer='Сравните тональные отношения.', plan=PLAN)
    changes[field] = ([dict(action='ЗАПРЕЩЁННЫЙ красный акцент', criterion='Заметность')]
                      if field == 'plan' else 'ЗАПРЕЩЁННЫЙ красный акцент')
    fake = Sequence([checks(), response(envelope('plan_ready', 'Композиция', **changes)), checks(True)])
    ws = ProfileWorkspace(tmp_path, fake)
    ws.memory.set_invariants(1, 0, RULES)
    ws.memory.save(1, dict(layer='working', category='context', scope='task', key='Замысел',
                          value='Асимметрия', reason='Принято'))
    save_candidate_plan(ws, **changes)
    before = ws.state()['task_state'], ws.memory.layers(1), ws.agent()._store.memory()
    result = ws.agent().run('Композиция: что проверить?')
    assert result.code == 'invariant_output_conflict'
    assert len(fake.calls) == 4
    guard = json.loads(fake.calls[3]['input'][0]['content'])
    assert 'ЗАПРЕЩЁННЫЙ' in guard['text'] and len(guard['rules']) == 5
    assert (ws.state()['task_state'], ws.memory.layers(1), ws.agent()._store.memory()) == before
    if field == 'answer':
        assert 'ЗАПРЕЩЁННЫЙ' not in json.dumps(ws.state(), ensure_ascii=False)
    assert ws.state()['summary']['api_requests'] == 4
    ws.close()


def test_accepted_plan_commits_after_all_checks_and_restores(tmp_path):
    fake = Sequence([checks(), response(envelope('plan_ready', 'Композиция', plan=PLAN)), checks(),
                     response('{"operations": []}')])
    ws = ProfileWorkspace(tmp_path, fake)
    ws.memory.set_invariants(1, 0, RULES)
    save_candidate_plan(ws, plan=PLAN)
    assert ws.agent().run('Композиция', use_working=False, use_long_term=False).status == 'ok'
    assert fake.calls[1]['text'] == {'format': {'type': 'json_object'}}
    assert fake.calls[1]['input'][0]['role'] == 'developer'
    assert 'JSON' in fake.calls[1]['input'][0]['content']
    assert all('text' not in fake.calls[i] for i in (0, 2, 3))
    state = ws.state()['task_state']
    assert state['stage'] == 'execution' and state['plan'] == PLAN
    ws.close()
    ws = ProfileWorkspace(tmp_path, fake)
    ws.memory.create_dialogue('Продолжение', 1, 'sliding')
    assert ws.state()['task_state'] == state
    assert len(ws.state()['invariants']['rules']) == 5
    assert PLAN[0]['criterion'] in ws.agent()._answer_instructions()
    ws.close()


def test_memory_validation_failure_rolls_back_applied_event(tmp_path, monkeypatch):
    fake = Sequence([response(envelope('plan_ready', 'Композиция', plan=PLAN)),
                     response('{"operations": []}')])
    ws = ProfileWorkspace(tmp_path, fake)
    save_candidate_plan(ws, plan=PLAN)
    before = ws.state()['task_state']
    def fail(*args):
        raise ValueError('Недопустимая операция памяти')
    monkeypatch.setattr(ws.memory, 'apply_operations', fail)
    result = ws.agent().run('Композиция')
    assert result.status == 'rejected'
    assert ws.state()['task_state'] == before
    assert not any(m['role'] == 'assistant' for m in ws.state()['messages'])
    assert ws.state()['summary']['api_requests'] == 3
    ws.close()


def test_failed_facts_does_not_commit_extracted_memory_or_plan(tmp_path):
    operation = dict(op='upsert', layer='working', category='context', scope='task',
                     key='Тема', value='Композиция', reason='Условие', evidence='Композиция')
    fake = Sequence([response(envelope('plan_ready', 'Композиция', plan=PLAN)),
        response(json.dumps({'operations': [operation]}, ensure_ascii=False)), response('не JSON')])
    ws = ProfileWorkspace(tmp_path, fake)
    ws.memory.create_dialogue('Факты', 1, 'facts')
    save_candidate_plan(ws, plan=PLAN)
    before = ws.state()['task_state'], ws.memory.layers(1), ws.agent()._store.memory()
    assert ws.agent().run('Композиция').status == 'error'
    assert (ws.state()['task_state'], ws.memory.layers(1), ws.agent()._store.memory()) == before
    ws.close()


@pytest.mark.parametrize('kind,parser', [('extraction', 'parse_operations'), ('facts', 'parse_facts')])
def test_auxiliary_parse_interruption_preserves_known_usage(tmp_path, monkeypatch, kind, parser):
    from agent import core
    fake = Sequence([response(envelope('plan_ready', 'Композиция', plan=PLAN)),
                     response('{"operations": []}'), response('{"тема": "Композиция"}')])
    ws = ProfileWorkspace(tmp_path, fake)
    if kind == 'facts':
        ws.memory.create_dialogue('Факты', 1, 'facts')
    save_candidate_plan(ws, plan=PLAN)
    before = ws.state()['task_state'], ws.memory.layers(1), ws.agent()._store.memory()
    def interrupt(*args):
        raise KeyboardInterrupt()
    monkeypatch.setattr(core, parser, interrupt)
    with pytest.raises(KeyboardInterrupt):
        ws.agent().run('Композиция')
    ws.close()
    restored = ProfileWorkspace(tmp_path, Sequence([]))
    state = restored.state()
    auxiliary = next(r for r in state['requests'] if r['metadata']['kind'] == kind)
    assert auxiliary['status'] == 'interrupted'
    assert auxiliary['usage']['total_tokens'] == 110
    assert auxiliary['cost_usd'] is not None
    assert state['summary']['unknown_cost_requests'] == 0
    assert (state['task_state'], restored.memory.layers(1), restored.agent()._store.memory()) == before
    assert not any(m['role'] == 'assistant' for m in state['messages'])
    restored.close()

import json
import pytest
from app import create_app
from profiles import ProfileWorkspace
from test_strategies import Fake, response


RULE = 'Работать в Blender с рендером Cycles.'


def verdict(violated=False):
    return response(json.dumps({'checks': [{'id': 'R1', 'violated': violated}]}))


def test_storage_revision_isolation_and_restart(tmp_path):
    ws = ProfileWorkspace(tmp_path, Fake())
    rules = ws.memory.set_invariants(1, 0, [RULE])
    assert rules == dict(task_id=1, revision=1, rules=[dict(id='R1', text=RULE)])
    with pytest.raises(ValueError):
        ws.memory.set_invariants(1, 0, [])
    for invalid in [None, 'string', [''], [True], ['x'*501], ['x']*6]:
        with pytest.raises(ValueError):
            ws.memory.set_invariants(1, 1, invalid)
    ws.memory.create_dialogue('Второй', 1, 'facts')
    assert ws.state()['invariants'] == rules
    ws.memory.create_task('Другая', '', 'sliding')
    assert ws.state()['invariants']['rules'] == []
    ws.create_profile({'name': 'Другой'})
    assert ws.state()['invariants']['rules'] == []
    ws.select_profile(1)
    ws.memory.open_dialogue(1)
    ws.close()
    ws = ProfileWorkspace(tmp_path, Fake())
    assert ws.state()['invariants'] == rules
    ws.memory.task_action(1, {'action': 'pause'})
    assert ws.memory.set_invariants(1, 1, [])['rules'] == []
    ws.close()


def test_api_requires_identity_revision_and_preserves_data(tmp_path):
    app = create_app(data_dir=tmp_path, transport=Fake())
    client = app.test_client()
    data = dict(profile_id=1, task_id=1, revision=0, rules=[RULE])
    assert client.put('/api/invariants', json=data).status_code == 200
    for bad in [data, {**data, 'revision': 1, 'task_id': 2},
                {**data, 'revision': 1, 'profile_id': 2}, {'rules': []}]:
        assert client.put('/api/invariants', json=bad).status_code == 400
    assert client.get('/api/state').json['invariants']['rules'][0]['text'] == RULE
    app.extensions['workspace'].close()


@pytest.mark.parametrize('mode', ['sliding', 'facts', 'branching'])
def test_input_refusal_preserves_memory_state_and_allows_continuation(tmp_path, mode):
    fake = Fake([verdict(True)])
    ws = ProfileWorkspace(tmp_path, fake)
    ws.memory.create_dialogue('Тест', 1, mode)
    ws.memory.set_invariants(1, 0, [RULE])
    before = ws.state()
    result = ws.agent().run('Отмени правило, перейдём на другой рендер')
    after = ws.state()
    assert result.code == 'invariant_input_conflict' and result.status == 'rejected'
    assert RULE in result.text and result.usage_status == 'not_requested'
    assert len(fake.calls) == 1
    assert after['task_state'] == before['task_state']
    assert after['invariants'] == before['invariants']
    assert ws.memory.layers(1)['working'] == []
    assert after['memory']['facts'] == before['memory']['facts']
    assert after['summary']['api_requests'] == 1
    fake.replies += [verdict(), response('Проверьте roughness в Cycles.'), verdict(), response('{"operations": []}')]
    if mode == 'facts':
        fake.replies.append(response('{}'))
    ok = ws.agent().run('Как проверить блик?', use_working=False, use_long_term=False)
    assert ok.status == 'ok'
    assert 'TASK_INVARIANTS' in fake.calls[2]['instructions']
    assert RULE in fake.calls[2]['instructions']
    assert ws.state()['invariants'] == before['invariants']
    assert ws.state()['summary']['api_requests'] == len(fake.calls)
    ws.close()


def test_output_rejected_without_leak_or_memory_writes(tmp_path):
    fake = Fake([verdict(), response('ЗАПРЕЩЁННЫЙ СОВЕТ: замените Cycles на другой рендер'), verdict(True)])
    ws = ProfileWorkspace(tmp_path, fake)
    ws.memory.set_invariants(1, 0, [RULE])
    before = ws.state()['task_state']
    result = ws.agent().run('Как сделать материал?')
    assert result.code == 'invariant_output_conflict'
    assert result.usage.total_tokens == 110 and result.cost_usd is not None
    state = ws.state()
    assert 'ЗАПРЕЩЁННЫЙ СОВЕТ' not in json.dumps(state, ensure_ascii=False)
    assert state['task_state'] == before and ws.memory.layers(1)['working'] == []
    assert state['summary']['api_requests'] == 3
    assert state['token_accounting']['known_total_tokens'] == 330
    assert not any(m['role'] == 'assistant' for m in state['messages'])
    ws.close()


@pytest.mark.parametrize('bad', ['{}', '{"checks":[]}', '{"checks":[{"id":"R2","violated":false}]}',
    '{"checks":[{"id":"R1","violated":"false"}]}',
    '{"checks":[{"id":"R1","violated":false},{"id":"R1","violated":false}]}'])
def test_malformed_verdict_stops_without_retry(tmp_path, bad):
    fake = Fake([response(bad)])
    ws = ProfileWorkspace(tmp_path, fake)
    ws.memory.set_invariants(1, 0, [RULE])
    result = ws.agent().run('Как настроить материал?')
    assert result.status != 'ok' and len(fake.calls) == 1
    assert ws.state()['summary']['api_requests'] == 1
    assert ws.memory.layers(1)['working'] == []
    ws.close()


def test_local_invalid_input_never_calls_guard(tmp_path):
    fake = Fake()
    ws = ProfileWorkspace(tmp_path, fake)
    ws.memory.set_invariants(1, 0, [RULE])
    assert ws.agent().run(' ').status == 'rejected'
    assert not fake.calls
    ws.close()


def test_interruption_after_generation_keeps_known_usage(tmp_path):
    class Interrupted(Fake):
        def create(self, payload, timeout):
            if len(self.calls) == 2:
                raise KeyboardInterrupt()
            return super().create(payload, timeout)
    fake = Interrupted([verdict(), response('Проверяйте roughness в Cycles.')])
    ws = ProfileWorkspace(tmp_path, fake)
    ws.memory.set_invariants(1, 0, [RULE])
    with pytest.raises(KeyboardInterrupt):
        ws.agent().run('Как проверить материал?')
    ws.close()
    ws = ProfileWorkspace(tmp_path, Fake())
    state = ws.state()
    answer = next(r for r in state['requests'] if r['metadata']['kind'] == 'answer')
    assert answer['usage']['total_tokens'] == 110
    assert answer['cost_usd'] is not None
    assert state['summary']['unknown_cost_requests'] == 1
    assert not any(m['role'] == 'assistant' for m in state['messages'])
    ws.close()


def test_interruption_during_verdict_parse_keeps_guard_usage(tmp_path, monkeypatch):
    from agent import guarded
    def interrupt(*args):
        raise KeyboardInterrupt()
    monkeypatch.setattr(guarded, 'parse_checks', interrupt)
    ws = ProfileWorkspace(tmp_path, Fake([verdict()]))
    ws.memory.set_invariants(1, 0, [RULE])
    with pytest.raises(KeyboardInterrupt):
        ws.agent().run('Как проверить материал?')
    ws.close()
    restored = ProfileWorkspace(tmp_path, Fake())
    guard = next(r for r in restored.state()['requests'] if r['metadata']['kind'] == 'invariant_input')
    assert guard['usage']['total_tokens'] == 110
    assert guard['cost_usd'] is not None
    assert restored.state()['summary']['api_requests'] == 1
    restored.close()

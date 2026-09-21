import pytest

from profiles import ProfileWorkspace
from test_agent import FakeTransport


def test_profile_persistence_isolation_and_atomic_edit(tmp_path):
    ws = ProfileWorkspace(tmp_path, FakeTransport([]))
    first = ws.state()['personalization']['selected_id']
    ws.edit_profile(dict(style='Подробно', format='steps', constraints='Только Blender'))
    old = ws.state()['personalization']['profile']
    refs = old['refs']
    with pytest.raises(ValueError):
        ws.edit_profile(dict(style='Кратко', format='invalid', constraints=''))
    assert ws.state()['personalization']['profile'] == old
    ws.create_profile(dict(name='Опытный', style='Кратко', format='markdown', constraints='Cycles'))
    second = ws.state()['personalization']['selected_id']
    assert second != first
    assert ws.state()['messages'] == []
    ws.select_profile(first)
    assert ws.state()['personalization']['profile']['style'] == 'Подробно'
    ws.edit_profile(dict(style='Без вступления', format='plain', constraints=''))
    assert ws.memory.resolve(refs)[0]['value'] == 'Подробно'
    ws.close()
    reopened = ProfileWorkspace(tmp_path, FakeTransport([]))
    assert reopened.state()['personalization']['selected_id'] == first
    assert reopened.state()['personalization']['profile']['style'] == 'Без вступления'
    reopened.close()


def test_profile_only_editor_can_change_and_auto_proposals_are_reported(tmp_path):
    ws = ProfileWorkspace(tmp_path, FakeTransport([]))
    task = ws.memory.workspace()['active_dialogue']['task_id']
    p = ws.state()['personalization']['profile']
    entry = ws.memory.resolve(p['refs'])[0]
    for action in [lambda: ws.memory.save(task, entry),
                   lambda: ws.memory.deactivate(task, entry['id']),
                   lambda: ws.memory.unlock(task, entry['id']),
                   lambda: ws.memory.move(task, entry['id'], dict(layer='working', scope='task', category='context'))]:
        with pytest.raises(ValueError):
            action()
    operation = dict(op='upsert',layer='long_term',category='profile',scope='user',
                     key='стиль',value='Только JSON',reason='Предпочтение',evidence='Только JSON')
    changed, skipped = ws.memory.apply_operations(task, [operation], 'test')
    assert changed == [] and len(skipped) == 1
    assert ws.state()['personalization']['profile'] == p
    ws.close()


def test_bad_profile_selection_does_not_mutate(tmp_path):
    ws = ProfileWorkspace(tmp_path, FakeTransport([]))
    before = ws.state()['personalization']
    for value in [True, None, '1', -1, 999]:
        with pytest.raises(ValueError):
            ws.select_profile(value)
    for data in [dict(name=''),dict(name='X',format=[]),dict(name='X',style='x'*1001)]:
        with pytest.raises(ValueError):
            ws.create_profile(data)
    assert ws.state()['personalization'] == before
    ws.close()


def test_whitespace_cannot_bypass_reserved_keys(tmp_path):
    ws = ProfileWorkspace(tmp_path, FakeTransport([]))
    task = ws.memory.workspace()['active_dialogue']['task_id']
    before = ws.memory.profile()
    forged = dict(layer='long_term',category='knowledge',scope='user',
                  key=' \tpersonalization.format\n',value='invalid',reason='Обход')
    with pytest.raises(ValueError):
        ws.memory.save(task, forged)
    changes, skipped = ws.memory.apply_operations(task, [{**forged,'op':'upsert'}], 'test')
    assert changes == [] and len(skipped) == 1
    assert ws.memory.profile() == before
    ws.close()

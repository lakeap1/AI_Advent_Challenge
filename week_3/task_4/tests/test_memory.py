import pytest

from agent.memory import MemoryStore


def entry(**changes):
    return dict(layer='working', category='context', scope='task', key='рендер',
                value='Cycles', reason='Условие текущей задачи', **changes)


def test_layers_revision_restart_and_task_isolation(tmp_path):
    path = tmp_path / 'memory.sqlite3'
    store = MemoryStore(path)
    task = store.create_task('Свет', 'Портрет', 'sliding')['task_id']
    first = store.save(task, entry())
    second = store.save(task, {**entry(), 'value': 'Eevee'})
    assert first['revision'] != second['revision']
    assert store.resolve([{'id': first['id'], 'revision': first['revision']}])[0]['value'] == 'Cycles'
    store.save(task, dict(layer='long_term', category='profile', scope='user', key='язык', value='русский', reason='Профиль'))
    store.save(task, dict(layer='long_term', category='decision', scope='project', key='палитра', value='тёплая', reason='Принято'))
    other = store.create_task('Другое', 'Другой проект', 'sliding')['task_id']
    assert store.layers(other)['working'] == []
    assert [x['key'] for x in store.layers(other)['long_term']] == ['язык']
    store.create_dialogue('Продолжение', task, 'facts')
    store.close()
    store = MemoryStore(path)
    assert store.workspace()['active_dialogue']['task_id'] == task
    assert store.layers(task)['working'][0]['value'] == 'Eevee'
    store.deactivate(task, second['id'])
    assert store.layers(task)['working'] == []
    assert store.resolve([{'id': first['id'], 'revision': first['revision']}])[0]['value'] == 'Cycles'


def test_auto_batch_is_atomic_and_manual_records_locked(tmp_path):
    store = MemoryStore(tmp_path / 'm.sqlite3')
    task = store.create_task('Свет', '', 'sliding')['task_id']
    store.save(task, entry())
    operation = {**entry(), 'op': 'upsert', 'evidence': 'Eevee', 'value': 'Eevee'}
    changed, skipped = store.apply_operations(task, [operation], 'dialogue:1/request:2')
    assert not changed and skipped
    assert store.layers(task)['working'][0]['value'] == 'Cycles'
    with pytest.raises(ValueError):
        store.apply_operations(task, [{**operation, 'key': 'новый'}, {**operation, 'key': 'bad', 'scope': 'user'}], 'source')
    assert [x['key'] for x in store.layers(task)['working']] == ['рендер']


@pytest.mark.parametrize('change', [{'scope': 'user'}, {'value': ''}, {'value': '\ud800'}, {'key': 'x'*81}])
def test_invalid_memory_does_not_write(tmp_path, change):
    store = MemoryStore(tmp_path / 'm.sqlite3')
    task = store.create_task('Задача', '', 'sliding')['task_id']
    with pytest.raises(ValueError):
        store.save(task, {**entry(), **change})
    assert store.layers(task)['working'] == []

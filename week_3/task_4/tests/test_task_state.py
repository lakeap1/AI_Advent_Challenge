import pytest

from agent.task_state import StateMemoryStore
from state_helpers import apply_checked, transition


def fields(**extra):
    return dict(goal='Убрать пластиковый вид керамики', current_step='Проверить roughness',
                expected_action='Сделать пробный рендер', notes='Cycles; свет пока не менять',
                plan=[dict(action='Проверить roughness', criterion='Сравнить блик с исходным рендером')], **extra)


@pytest.fixture
def memory(tmp_path):
    store = StateMemoryStore(tmp_path / 'memory.sqlite3')
    store.create_task('Керамика')
    yield store
    store.close()


@pytest.mark.parametrize('stage', ['planning', 'execution', 'validation'])
def test_pause_restores_every_stage_and_blocks_mutations(memory, tmp_path, stage):
    apply_checked(memory, **fields())
    for target in ['execution', 'validation'][:['planning', 'execution', 'validation'].index(stage)]:
        transition(memory, target, current_step='Сравнить блик', expected_action='Сообщить наблюдение')
    before = memory.task_state(1)
    memory.task_action(1, dict(action='pause'))
    for data in [dict(action='save', **fields()), dict(action='transition', target='execution')]:
        with pytest.raises(ValueError):
            memory.task_action(1, data)
    reopened = StateMemoryStore(tmp_path / 'memory.sqlite3')
    assert reopened.task_state(1) == {**before, 'paused': True}
    reopened.task_action(1, dict(action='resume'))
    assert reopened.task_state(1) == before
    reopened.close()


def test_all_transitions_and_terminal_state(memory):
    for target in ['execution', 'planning', 'execution', 'validation', 'execution', 'validation', 'done']:
        if memory.task_state(1)['stage'] == 'planning':
            apply_checked(memory, **fields())
        transition(memory, target, current_step='Шаг', expected_action='Действие')
        assert memory.task_state(1)['stage'] == target
    before = memory.task_state(1)
    assert before['expected_action'] == 'Действий не ожидается.'
    for action in ['pause', 'resume', 'save', 'transition']:
        with pytest.raises(ValueError):
            memory.task_action(1, dict(action=action, target='planning'))
        assert memory.task_state(1) == before


@pytest.mark.parametrize('data', [
    dict(action='transition', target='done'), dict(action='transition', target=[]),
    dict(action=[]), dict(action='resume'), dict(action='save', **{**fields(), 'notes': 'x' * 6001}),
    dict(action='save', **{**fields(), 'goal': '\ud800'}),
    dict(action='transition', target='execution', current_step='OK', expected_action=''),
    dict(action='save', **fields(), stage='done'),
])
def test_invalid_action_atomic(memory, data):
    before = memory.task_state(1)
    with pytest.raises(ValueError):
        memory.task_action(1, data)
    assert memory.task_state(1) == before


def test_tasks_dialogues_and_migration(memory, tmp_path):
    apply_checked(memory, **fields())
    old = memory.task_state(1)
    memory.create_dialogue('Ещё один', 1, 'facts')
    assert memory.task_state(1) == old
    memory.create_task('Свет')
    assert memory.task_state(2)['goal'] == 'Свет'
    assert memory.task_state(1) == old
    with pytest.raises(ValueError):
        memory.task_state(999)
    from agent.personalization import ProfileMemoryStore
    legacy = ProfileMemoryStore(tmp_path / 'legacy.sqlite3')
    legacy.create_task('Прежняя задача')
    legacy.close()
    upgraded = StateMemoryStore(tmp_path / 'legacy.sqlite3')
    assert upgraded.task_state(1)['goal'] == 'Прежняя задача'
    upgraded.close()


@pytest.mark.parametrize('bad', [[], {'bad': 1}, 'x' * 501, '\ud800', ''])
def test_done_rejects_malformed_optional_fields(memory, bad):
    apply_checked(memory, **fields())
    for target in ['execution', 'validation']:
        transition(memory, target, current_step='Шаг', expected_action='Действие')
    before = memory.task_state(1)
    with pytest.raises(ValueError):
        memory.task_action(1, dict(action='transition', target='done', current_step=bad))
    assert memory.task_state(1) == before

import json
import pytest
from agent.extraction import parse_operations


def operation(**patch):
    return {**dict(op='upsert', layer='working', category='context', scope='task', key='рендер',
                value='Cycles', reason='Инструмент текущей задачи', evidence='Cycles'), **patch}


def test_exact_evidence_and_strict_categories():
    item = operation()
    assert parse_operations(json.dumps({'operations': [item]}), 'Использую Cycles', 8) == [item]
    for bad in [{**item, 'evidence': 'Eevee'}, {**item, 'scope': 'user'}, {**item, 'value': '\ud800'}, {**item, 'op': 'execute'}]:
        with pytest.raises(ValueError):
            parse_operations(json.dumps({'operations': [bad]}), 'Использую Cycles', 8)


@pytest.mark.parametrize('raw', ['[]', '{}', '{"operations":[],"operations":[]}', '{"operations":[],"extra":1}', 'null'])
def test_malformed_or_ambiguous_json_rejected(raw):
    with pytest.raises(ValueError):
        parse_operations(raw, 'Вопрос', 8)


def test_duplicate_targets_and_limit_rejected():
    for ops in [[operation(), operation()], [operation(key=str(i)) for i in range(9)]]:
        with pytest.raises(ValueError):
            parse_operations(json.dumps({'operations': ops}), 'Cycles', 8)

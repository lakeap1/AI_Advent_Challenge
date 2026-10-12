"""Strict, bounded state patch for an ordinary graphics conversation."""

import json


LIST_FIELDS = ('clarifications', 'constraints', 'terms')
MAX_OPERATIONS = 12
MAX_RECORDS = 32
MAX_KEY = 80
MAX_VALUE = 1000
MAX_STATE = 16000
MAX_QUERY = 2000


def empty_state():
    return {'goal': '', 'clarifications': [], 'constraints': [], 'terms': [],
            'revision': 0, 'provenance': []}


def _text(value, limit, *, allow_empty=False):
    if type(value) is not str or len(value) > limit or (not allow_empty and not value.strip()):
        raise ValueError('Недопустимый текст состояния диалога.')
    try:
        value.encode('utf-8')
    except UnicodeEncodeError:
        raise ValueError('Некорректный Unicode состояния диалога.') from None
    return value.strip()


def validate_preparation(state, proposal, message):
    """Validate the complete model envelope before any state write or retrieval."""
    if type(proposal) is not dict or set(proposal) != {'revision', 'search_query', 'operations'}:
        raise ValueError('Неверные поля подготовки диалога.')
    if type(proposal['revision']) is not int or proposal['revision'] != state['revision']:
        raise ValueError('Устаревшая revision состояния диалога.')
    query = _text(proposal['search_query'], MAX_QUERY)
    operations = proposal['operations']
    if type(operations) is not list or len(operations) > MAX_OPERATIONS:
        raise ValueError('Превышен лимит операций диалога.')
    for operation in operations:
        if type(operation) is not dict or set(operation) != {'action', 'field', 'key', 'value', 'evidence'}:
            raise ValueError('Неверные поля операции диалога.')
        if operation['action'] not in ('set', 'remove') or operation['field'] not in ('goal', *LIST_FIELDS):
            raise ValueError('Недопустимая операция диалога.')
        key = _text(operation['key'], MAX_KEY, allow_empty=operation['field'] == 'goal')
        if operation['field'] == 'goal' and key:
            raise ValueError('Цель не имеет ключа.')
        value = _text(operation['value'], MAX_VALUE, allow_empty=operation['action'] == 'remove')
        if operation['action'] == 'remove' and value:
            raise ValueError('Удаление не содержит нового значения.')
        evidence = _text(operation['evidence'], MAX_VALUE)
        if evidence not in message:
            raise ValueError('Evidence отсутствует в текущем сообщении пользователя.')
    return query


def apply_preparation(state, proposal, message, *, request_id):
    """Return a new state. Caller atomically persists it with expected revision."""
    validate_preparation(state, proposal, message)
    if type(request_id) is not int or request_id <= 0:
        raise ValueError('Неверный request_id происхождения.')
    next_state = json.loads(json.dumps(state, ensure_ascii=False))
    if not proposal['operations']:
        return next_state
    revision = state['revision'] + 1
    for operation in proposal['operations']:
        action, field = operation['action'], operation['field']
        key, value, evidence = (operation[name].strip() for name in ('key', 'value', 'evidence'))
        event = {'revision': revision, 'action': action, 'field': field,
                 'key': key, 'value': value, 'evidence': evidence,
                 'source_request_id': request_id}
        if field == 'goal':
            next_state['goal'] = value if action == 'set' else ''
        else:
            rows = next_state[field]
            rows[:] = [row for row in rows if row['key'].casefold() != key.casefold()]
            if action == 'set':
                rows.append({'key': key, 'value': value, 'evidence': evidence,
                             'source_request_id': request_id})
            if len(rows) > MAX_RECORDS:
                raise ValueError('Превышен лимит записей состояния диалога.')
        next_state['provenance'].append(event)
    next_state['revision'] = revision
    if len(json.dumps(next_state, ensure_ascii=False, separators=(',', ':'), allow_nan=False)) > MAX_STATE:
        raise ValueError('Превышен размер состояния диалога.')
    return next_state

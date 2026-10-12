"""Deterministic user/state authority for ordinary RAG, without model rewrites."""

import tiktoken


QUERY_POLICY = 'literal_current_user_active_state_recent_users_v1'
MAX_QUERY_CHARS = 8000
MAX_QUERY_TOKENS = 8000


def active_context(state, history):
    """Project active values only; provenance is an audit trail, not active context."""
    projection = {'goal': state['goal'], 'revision': state['revision']}
    for field in ('constraints', 'terms', 'clarifications'):
        projection[field] = [{'key': row['key'], 'value': row['value']}
                             for row in state[field]]
    recent_users = [{'id': row['id'], 'content': row['content']}
                    for row in history[-6:] if row['role'] == 'user']
    return {'state': projection, 'recent_user_messages': recent_users}


def build_question_context(prompt, state, history):
    """Retain the entire literal question and add whole optional entries in order.

    Filter/final context remains complete even when an optional embedding entry
    is omitted. The separate query budget measures the actual serialized text.
    """
    context = active_context(state, history)
    try:
        encoding = tiktoken.get_encoding('cl100k_base')
    except Exception as error:
        raise ValueError('Не удалось загрузить cl100k_base для проверки бюджета запроса.') from error

    def tokens(text):
        return len(encoding.encode(text, disallowed_special=()))

    prompt.encode('utf-8')
    if len(prompt) > MAX_QUERY_CHARS or tokens(prompt) > MAX_QUERY_TOKENS:
        raise ValueError('Текущая реплика превышает бюджет поискового запроса '
                         '(8000 символов / 8000 токенов cl100k_base).')
    candidates = []
    if context['state']['goal']:
        candidates.append(('state.goal', 'goal:\n' + context['state']['goal']))
    for row in reversed(context['recent_user_messages']):
        candidates.append((f'history.user.{row["id"]}', 'recent_user:\n' + row['content']))
    for field in ('constraints', 'terms', 'clarifications'):
        for row in context['state'][field]:
            candidates.append((f'state.{field}.{row["key"]}',
                               field + ':\n' + row['key'] + '\n' + row['value']))
    query = prompt
    included, omitted = [], []
    for ref, text in candidates:
        proposed = query + '\n\n' + text
        proposed.encode('utf-8')
        if len(proposed) <= MAX_QUERY_CHARS and tokens(proposed) <= MAX_QUERY_TOKENS:
            query = proposed
            included.append(ref)
        else:
            omitted.append(ref)
    policy = {'name': QUERY_POLICY, 'encoding': 'cl100k_base',
              'max_chars': MAX_QUERY_CHARS, 'max_tokens': MAX_QUERY_TOKENS,
              'chars': len(query), 'tokens': tokens(query),
              'included_entry_refs': included, 'omitted_entry_refs': omitted,
              'omitted_entry_count': len(omitted)}
    return query, context, policy

"""Literal context budgets use the real embedding tokenizer and whole entries."""

import pytest
import tiktoken

from agent.conversation_state import empty_state
from rag.question_context import active_context, build_question_context


def row(key, value):
    return {'key': key, 'value': value, 'evidence': 'Old contradictory provenance',
            'source_request_id': 1}


def test_current_4000_chars_is_retained_in_full_with_literal_special_text():
    prefix = '  "Quoted"\n<|endoftext|> '
    prompt = prefix + 'a' * (4000 - len(prefix))
    assert len(prompt) == 4000
    query, context, policy = build_question_context(prompt, empty_state(), [])
    assert query == prompt
    assert policy['chars'] == 4000
    assert policy['tokens'] == len(tiktoken.get_encoding('cl100k_base').encode(
        query, disallowed_special=()))
    assert policy['omitted_entry_count'] == 0


def test_query_priority_and_whole_omission_do_not_reduce_filter_context():
    state = empty_state()
    state['goal'] = 'Goal' * 250
    state['constraints'] = [row('large', 'Z' * 1000), row('small', 'active value')]
    state['terms'] = [row('term', 'active definition')]
    state['clarifications'] = [row('detail', 'active clarification')]
    history = [{'id': 1, 'role': 'user', 'content': 'outside last six'},
               {'id': 2, 'role': 'user', 'content': 'oldest user ' + 'D' * 800},
               {'id': 3, 'role': 'assistant', 'content': 'Invented assistant owner'},
               {'id': 4, 'role': 'user', 'content': 'older user ' + 'B' * 1500},
               {'id': 5, 'role': 'assistant', 'content': 'Generated model wording'},
               {'id': 6, 'role': 'user', 'content': 'recent user ' + 'C' * 1500},
               {'id': 7, 'role': 'assistant', 'content': 'Assistant-only condition'}]
    prompt = 'Q' * 4000
    query, context, policy = build_question_context(prompt, state, history)
    assert query.startswith(prompt + '\n\ngoal:\n' + state['goal'])
    assert history[5]['content'] in query
    assert history[3]['content'] not in query  # Never partially serialize an entry.
    assert 'B' not in query
    assert policy['omitted_entry_refs'] == ['history.user.4', 'state.constraints.large']
    assert policy['omitted_entry_count'] == 2
    assert policy['included_entry_refs'] == ['state.goal', 'history.user.6', 'history.user.2',
        'state.constraints.small', 'state.terms.term', 'state.clarifications.detail']
    assert 'outside last six' not in query and 'Assistant' not in query
    assert 'Old contradictory provenance' not in query
    assert context['state']['constraints'] == [{'key': 'large', 'value': 'Z' * 1000},
                                               {'key': 'small', 'value': 'active value'}]
    assert len(context['recent_user_messages']) == 3
    assert history[3]['content'] in str(context)
    assert 'provenance' not in context['state']
    assert len(query) <= 8000 and policy['tokens'] <= 8000


@pytest.mark.parametrize('prompt', ['x' * 8001, '\U0001f9ec' * 4000], ids=['chars', 'tokens'])
def test_oversized_current_question_is_explicitly_rejected_without_truncation(prompt):
    with pytest.raises(ValueError, match='бюджет поискового запроса'):
        build_question_context(prompt, empty_state(), [])


def test_active_projection_excludes_cancelled_provenance_values():
    state = empty_state()
    state.update(goal='Current goal', constraints=[row('geometry', 'Current value')],
        provenance=[{'value': 'Cancelled value', 'evidence': 'Cancelled condition'}])
    context = active_context(state, [])
    assert context['state']['constraints'] == [{'key': 'geometry', 'value': 'Current value'}]
    assert 'Cancelled' not in str(context) and 'Old contradictory' not in str(context)
    query, _, _ = build_question_context('Explain current setup.', state, [])
    assert 'Current value' in query and 'Cancelled' not in query


def test_optional_entry_is_omitted_whole_by_tokens_even_when_characters_fit():
    state = empty_state()
    state['goal'] = '\U0001f9ec' * 100
    prompt = '\U0001f9ec' * 2600
    query, context, policy = build_question_context(prompt, state, [])
    assert len(prompt + '\n\ngoal:\n' + state['goal']) < 8000
    assert query == prompt and policy['tokens'] == 7800
    assert policy['omitted_entry_refs'] == ['state.goal']
    assert context['state']['goal'] == state['goal']

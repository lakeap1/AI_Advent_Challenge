"""Typed goal conditions at wire, expansion, atomic state and ledger boundaries."""
from copy import deepcopy
import json

import pytest
from jsonschema import ValidationError, validate

from app import create_app
from agent.conversation_state import apply_preparation, empty_state
from rag.conversation import PREPARATION_FORMAT
from tests_rag.test_question_parts import PartsTransport, parts_index, response


PROMPT = ('My goal is a published guide without changing the structure. '
          'Use open formats. Check lighting and display.')


def condition(key='structure', value='Keep the structure', evidence='without changing the structure',
              action='set'):
    return dict(action=action, key=key, value=value, evidence=evidence)


def plain(field='constraints', key='formats', value='Open formats', evidence='Use open formats',
          action='set'):
    return dict(action=action, field=field, key=key, value=value, evidence=evidence)


def goal(conditions=None, value='a published guide', evidence='My goal is a published guide', action='set'):
    body = dict(action=action, field='goal', key='',
                conditions=[] if conditions is None else conditions)
    if action == 'set':
        body['desired_outcome_evidence'] = value
    else:
        body.update(value=value, evidence=evidence)
    return body


class GoalTransport(PartsTransport):
    def __init__(self, operations, *, revision=None):
        super().__init__()
        self.operations = operations
        self.revision = revision
        self.proposal = None

    def create(self, payload, timeout):
        if payload['text']['format']['name'] != 'conversation_preparation':
            return super().create(payload, timeout)
        self.calls.append(payload)
        data = json.loads(payload['input'][0]['content'])
        self.proposal = {'revision': data['state']['revision'] if self.revision is None else self.revision,
            'operations': deepcopy(self.operations),
            'question_parts': [{'evidence': 'lighting'}, {'evidence': 'display'}]}
        return response(self.proposal)


def request_rows(result):
    rows = result['state']['requests']
    return (next(row for row in rows if row['metadata']['kind'] == 'answer'),
            next(row for row in rows if row['metadata']['kind'] == 'conversation_preparation'))


def test_enriched_goal_expands_and_persists_separate_conditions_one_revision_without_raw_mutation(
        tmp_path, parts_index):
    operations = [goal([condition(), condition('formats', 'Open formats', 'Use open formats')])]
    fake = GoalTransport(operations)
    app = create_app(data_dir=tmp_path, transport=fake)
    try:
        result = app.test_client().post('/api/ask', json={'prompt': PROMPT}).json
        assert result['status'] == 'ok', result
        parent, prep = request_rows(result)
        state = result['state']['conversation_state']
        assert state['goal'] == 'a published guide'
        assert {row['key']: row['value'] for row in state['constraints']} == {
            'structure': 'Keep the structure', 'formats': 'Open formats'}
        assert state['revision'] == 1
        assert [row['field'] for row in state['provenance']] == ['goal', 'constraints', 'constraints']
        assert all(row['source_request_id'] == parent['id'] and row['revision'] == 1
                   for row in state['provenance'])
        assert all(row['source_request_id'] == parent['id'] for row in state['constraints'])
        assert fake.operations == operations and fake.proposal['operations'] == operations
        assert prep['metadata']['raw_proposal'] == fake.proposal
        assert json.loads(prep['metadata']['raw_preparation_text']) == fake.proposal
        validate(fake.proposal, fake.calls[0]['text']['format']['schema'])
        assert prep['output_policy']['name'] == 'conversation_preparation_v3_2'
        assert fake.calls[0]['max_output_tokens'] == prep['metadata']['requested_max_output_tokens'] == 4000
        assert fake.calls[0]['model'] == 'gpt-6-luna' and fake.calls[0]['reasoning']['effort'] == 'high'
        assert len(fake.calls) == 4 and result['state']['summary']['api_requests'] == 5
        assert result['state']['summary']['cost_complete']
        assert parts_index.calls == [[parent['metadata']['rag']['search_query']]]
        assert 'memory-validator-compatibility' not in json.dumps(result)
        assert 'memory-validator-compatibility' not in json.dumps(fake.calls)
        rows = result['state']['requests']
    finally:
        app.extensions['workspace'].close()
    reopened = create_app(data_dir=tmp_path, transport=GoalTransport([]))
    try:
        restored = reopened.test_client().get('/api/state').json
        assert restored['conversation_state'] == state and restored['requests'] == rows
    finally:
        reopened.extensions['workspace'].close()


@pytest.mark.parametrize('count', [0, 11])
def test_goal_without_conditions_and_expansion_exactly_twelve_are_accepted(tmp_path, parts_index, count):
    fake = GoalTransport([goal([condition(f'condition-{i}') for i in range(count)])])
    app = create_app(data_dir=tmp_path, transport=fake)
    try:
        result = app.test_client().post('/api/ask', json={'prompt': PROMPT}).json
        assert result['status'] == 'ok', result
        assert result['state']['conversation_state']['goal'] == 'a published guide'
        assert len(result['state']['conversation_state']['constraints']) == count
        assert len(result['state']['conversation_state']['provenance']) == 1 + count
    finally:
        app.extensions['workspace'].close()


def seed(agent):
    old = 'Old goal with requirements included. Keep old structure. Keep format.'
    state = apply_preparation(empty_state(), {'revision': 0, 'search_query': old,
        'operations': [dict(action='set', field='goal', key='', value='Old goal with requirements included',
                            evidence='Old goal with requirements included'),
                       plain('constraints', 'structure', 'Old structure', 'Keep old structure'),
                       plain('constraints', 'formats', 'Keep format', 'Keep format')]}, old, request_id=1)
    agent._store.save_conversation_state(state, expected_revision=0, request_id=1)
    return state


def test_goal_replacement_nested_remove_and_goal_removal_do_not_implicitly_delete_constraints(
        tmp_path, parts_index):
    replacement = PROMPT.replace('without changing the structure', 'with the old structure requirement removed')
    fake = GoalTransport([goal([condition('structure', '', 'old structure requirement removed', 'remove')])])
    app = create_app(data_dir=tmp_path, transport=fake)
    try:
        before = seed(app.extensions['workspace'].agent())
        client = app.test_client()
        first = client.post('/api/ask', json={'prompt': replacement}).json
        assert first['status'] == 'ok', first
        state = first['state']['conversation_state']
        assert state['goal'] == 'a published guide' and state['revision'] == 2
        assert [row['key'] for row in state['constraints']] == ['formats']
        assert state['provenance'][:len(before['provenance'])] == before['provenance']
        fake.operations = [goal([], '', 'Cancel my goal', 'remove')]
        removed = client.post('/api/ask', json={'prompt': 'Cancel my goal. Check lighting and display.'}).json
        assert removed['status'] == 'ok', removed
        assert removed['state']['conversation_state']['goal'] == ''
        assert removed['state']['conversation_state']['constraints'] == state['constraints']
        assert removed['state']['conversation_state']['revision'] == 3
    finally:
        app.extensions['workspace'].close()


def test_existing_goal_string_is_retained_when_no_new_goal_is_assigned(tmp_path, parts_index):
    fake = GoalTransport([])
    app = create_app(data_dir=tmp_path, transport=fake)
    try:
        before = seed(app.extensions['workspace'].agent())
        result = app.test_client().post('/api/ask', json={'prompt': 'Return to my goal. Check lighting and display.'}).json
        assert result['status'] == 'ok'
        assert result['state']['conversation_state'] == before
    finally:
        app.extensions['workspace'].close()


INVALID = [
    [goal([condition(), condition('late', 'Unsupported', 'not in current text')])],
    [goal([condition(f'condition-{i}') for i in range(11)]), plain()],
    [plain(key='STRUCTURE'), plain(key=' structure ', value='Different')],
    [goal([condition(), condition(' STRUCTURE ', 'Different')])],
    [goal([condition()]), plain(key='Structure')],
    [goal(), goal(value='published guide')],
    [{key: value for key, value in goal().items() if key != 'conditions'}],
    [goal({'action': 'set'})],
    [goal([condition() for _ in range(12)])],
    [goal([{'action': 'set', 'key': 'structure', 'value': 'Current'}])],
    [goal([{**condition(), 'field': 'constraints'}])],
    [goal([{**condition(), 'action': 'unknown'}])],
    [goal([{**condition(), 'key': True}])],
    [goal([{**condition(), 'value': 1}])],
    [goal([{**condition(), 'evidence': None}])],
    [goal([condition(action='remove', value='Not empty')])],
    [goal([condition()], value='', action='remove')],
    [goal([], value='Not empty', action='remove')],
    [{**goal(), 'key': ' '}],
    [{**plain(), 'conditions': []}],
    [goal(value='')],
    [goal(value=' \t')],
    [goal(value=None)],
    [goal(value=True)],
    [goal(value={'result': 'a published guide'})],
    [goal(value='a paraphrased guide')],
    [goal(value='\na published guide')],
    [{key: value for key, value in goal().items() if key != 'desired_outcome_evidence'}],
    [{**goal(), 'value': 'a published guide'}],
    [{**goal(), 'evidence': 'a published guide'}],
]


@pytest.mark.parametrize('operations', INVALID)
def test_invalid_expansion_or_shape_rejects_whole_patch_before_embedding_and_retains_paid_usage(
        tmp_path, parts_index, operations):
    fake = GoalTransport(operations)
    app = create_app(data_dir=tmp_path, transport=fake)
    try:
        before = seed(app.extensions['workspace'].agent())
        result = app.test_client().post('/api/ask', json={'prompt': PROMPT}).json
        assert result['status'] == 'rejected' and result['code'] == 'invalid_preparation', result
        assert result['state']['conversation_state'] == before
        assert parts_index.calls == [] and len(fake.calls) == 1
        _, prep = request_rows(result)
        assert prep['usage'] == {'input_tokens': 100, 'output_tokens': 10, 'total_tokens': 110,
            'cached_input_tokens': 0, 'cache_write_input_tokens': 0, 'reasoning_tokens': 2}
        assert prep['cost_usd'] == '0.000015' and prep['output_policy']['status'] == 'rejected'
        assert result['state']['summary']['api_requests'] == 1
        assert result['state']['summary']['cost_complete']
        assert fake.operations == operations and fake.proposal['operations'] == operations
        assert all(row['role'] == 'user' for row in result['state']['messages'])
    finally:
        app.extensions['workspace'].close()


def test_wire_goal_requires_conditions_supports_remove_and_preserves_non_goal_contract():
    schema = PREPARATION_FORMAT['schema']
    def proposal(operation):
        return {'revision': 0, 'operations': [operation], 'question_parts': [{'evidence': 'lighting'}]}
    validate(proposal(goal([condition(action='remove', value='')])), schema)
    validate(proposal(goal([], '', 'My goal is a published guide', 'remove')), schema)
    for field in ('constraints', 'terms', 'clarifications'):
        validate(proposal(plain(field)), schema)
        validate(proposal(plain(field, value='', action='remove')), schema)
    for invalid in [
        {key: value for key, value in goal().items() if key != 'conditions'},
        goal([], 'nonempty', action='remove'), goal([condition()], '', action='remove'),
        {**plain(), 'conditions': []}]:
        with pytest.raises(ValidationError):
            validate(proposal(invalid), schema)


def test_same_key_in_different_fields_is_not_a_duplicate_target(tmp_path, parts_index):
    fake = GoalTransport([plain('constraints', 'shared'), plain('terms', 'shared')])
    app = create_app(data_dir=tmp_path, transport=fake)
    try:
        result = app.test_client().post('/api/ask', json={'prompt': PROMPT}).json
        assert result['status'] == 'ok'
        state = result['state']['conversation_state']
        assert state['constraints'][0]['key'] == state['terms'][0]['key'] == 'shared'
    finally:
        app.extensions['workspace'].close()


@pytest.mark.parametrize('span', ['a published guide', 'a  published guide 🚀'])
def test_extractive_goal_span_is_the_exact_legacy_value_and_evidence_before_retrieval(
        tmp_path, parts_index, span):
    prompt = PROMPT.replace('a published guide', span)
    operation = {'action': 'set', 'field': 'goal', 'key': '',
        'desired_outcome_evidence': span, 'conditions': [condition()]}
    fake = GoalTransport([operation])
    app = create_app(data_dir=tmp_path, transport=fake)
    try:
        result = app.test_client().post('/api/ask', json={'prompt': prompt}).json
        assert result['status'] == 'ok', result
        parent, prep = request_rows(result)
        state = result['state']['conversation_state']
        assert state['goal'] == span
        assert state['provenance'][0] == {'action': 'set', 'field': 'goal', 'key': '',
            'value': span, 'evidence': span, 'source_request_id': parent['id'], 'revision': 1}
        assert state['constraints'][0]['value'] == 'Keep the structure'
        assert state['constraints'][0]['evidence'] == 'without changing the structure'
        assert state['constraints'][0]['source_request_id'] == parent['id']
        assert prep['metadata']['raw_proposal'] == fake.proposal
        assert json.loads(prep['metadata']['raw_preparation_text']) == fake.proposal
        assert fake.proposal['operations'] == [operation]
        validate(fake.proposal, fake.calls[0]['text']['format']['schema'])
        assert prep['output_policy']['name'] == 'conversation_preparation_v3_2'
        assert span in parts_index.calls[0][0]
        assert len(fake.calls) == 4 and len(parts_index.calls) == 1
        assert result['state']['summary']['cost_complete']
        rows = result['state']['requests']
    finally:
        app.extensions['workspace'].close()
    reopened = create_app(data_dir=tmp_path, transport=GoalTransport([]))
    try:
        restored = reopened.test_client().get('/api/state').json
        assert restored['conversation_state'] == state and restored['requests'] == rows
    finally:
        reopened.extensions['workspace'].close()


def test_old_free_goal_wire_is_rejected_atomically_and_billed_without_retrieval(tmp_path, parts_index):
    old_wire = {'action': 'set', 'field': 'goal', 'key': '', 'value': 'A published guide',
        'evidence': 'My goal is a published guide', 'conditions': [condition()]}
    fake = GoalTransport([old_wire])
    app = create_app(data_dir=tmp_path, transport=fake)
    try:
        before = seed(app.extensions['workspace'].agent())
        result = app.test_client().post('/api/ask', json={'prompt': PROMPT}).json
        assert result['status'] == 'rejected' and result['code'] == 'invalid_preparation', result
        assert result['state']['conversation_state'] == before
        assert len(fake.calls) == 1 and parts_index.calls == []
        _, prep = request_rows(result)
        assert prep['usage']['total_tokens'] == 110 and prep['usage']['reasoning_tokens'] == 2
        assert prep['cost_usd'] == '0.000015' and prep['output_policy']['status'] == 'rejected'
        assert result['state']['summary']['api_requests'] == 1
        assert result['state']['summary']['cost_complete']
        assert all(row['role'] == 'user' for row in result['state']['messages'])
        assert fake.proposal['operations'] == [old_wire]
        with pytest.raises(ValidationError):
            validate(fake.proposal, fake.calls[0]['text']['format']['schema'])
    finally:
        app.extensions['workspace'].close()


def test_literal_outcome_over_legacy_limit_rejects_before_state_write(tmp_path, parts_index):
    span = 'x' * 1001
    prompt = f'My goal is {span}. Check lighting and display.'
    fake = GoalTransport([{'action': 'set', 'field': 'goal', 'key': '',
        'desired_outcome_evidence': span, 'conditions': []}])
    app = create_app(data_dir=tmp_path, transport=fake)
    try:
        before = seed(app.extensions['workspace'].agent())
        result = app.test_client().post('/api/ask', json={'prompt': prompt}).json
        assert result['status'] == 'rejected' and result['code'] == 'invalid_preparation'
        assert result['state']['conversation_state'] == before
        assert len(fake.calls) == 1 and parts_index.calls == []
        _, prep = request_rows(result)
        assert prep['usage']['total_tokens'] == 110 and prep['cost_usd'] == '0.000015'
        assert prep['output_policy']['status'] == 'rejected'
        with pytest.raises(ValidationError):
            validate(fake.proposal, fake.calls[0]['text']['format']['schema'])
    finally:
        app.extensions['workspace'].close()

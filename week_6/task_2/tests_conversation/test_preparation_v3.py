"""Preparation V3 at the provider, durable ledger and downstream boundaries."""
import json

import pytest
from jsonschema import ValidationError, validate

from app import create_app
from rag.conversation import PREPARATION_FORMAT, PREPARATION_POLICY
from rag.question_parts import validate_question_parts
from tests_rag.test_question_parts import PartsTransport, PROMPT, parts_index, response


class V3Transport(PartsTransport):
    def __init__(self, mutate=None):
        super().__init__()
        self.mutate = mutate
        self.proposal = None

    def create(self, payload, timeout):
        if payload['text']['format']['name'] != 'conversation_preparation':
            return super().create(payload, timeout)
        self.calls.append(payload)
        data = json.loads(payload['input'][0]['content'])
        self.proposal = {'revision': data['state']['revision'], 'operations': [
            {'action': 'set', 'field': 'goal', 'key': '',
             'desired_outcome_evidence': 'an image', 'conditions': []}],
            'question_parts': [{'evidence': 'lighting'}, {'evidence': 'display'}]}
        if self.mutate:
            self.mutate(self.proposal)
        return response(self.proposal)


def test_v3_wire_reaches_real_embedding_filters_final_and_keeps_exact_raw(tmp_path, parts_index, monkeypatch):
    from rag import chat
    ranked_queries = []
    rank = chat.rank_candidates
    def capture_rank(chunks, vector, query, config):
        ranked_queries.append(query)
        return rank(chunks, vector, query, config)
    monkeypatch.setattr(chat, 'rank_candidates', capture_rank)
    fake = V3Transport()
    app = create_app(data_dir=tmp_path, transport=fake)
    try:
        result = app.test_client().post('/api/ask', json={'prompt': PROMPT}).json
        assert result['status'] == 'ok', result
        preparation = fake.calls[0]
        validate(fake.proposal, preparation['text']['format']['schema'])
        assert set(preparation['text']['format']['schema']['properties']) == {
            'revision', 'operations', 'question_parts'}
        assert preparation['max_output_tokens'] == 4000
        assert preparation['model'] == 'gpt-6-luna' and preparation['reasoning']['effort'] == 'high'
        rows = result['state']['requests']
        prep = next(row for row in rows if row['metadata']['kind'] == 'conversation_preparation')
        assert prep['metadata']['requested_max_output_tokens'] == preparation['max_output_tokens']
        assert prep['output_policy']['name'] == PREPARATION_POLICY == 'conversation_preparation_v3_2'
        assert prep['metadata']['raw_proposal'] == fake.proposal
        assert json.loads(prep['metadata']['raw_preparation_text']) == fake.proposal
        parent = next(row for row in rows if row['metadata']['kind'] == 'answer')
        query = parent['metadata']['rag']['search_query']
        assert parts_index.calls == [[query]] and query.startswith(PROMPT) and 'an image' in query
        assert ranked_queries == [query]
        assert 'memory-validator-compatibility' not in json.dumps(result, ensure_ascii=False)
        assert 'memory-validator-compatibility' not in json.dumps(fake.calls, ensure_ascii=False)
        for meta in (prep['metadata'], parent['metadata']['rag'], parent['metadata']['conversation']):
            assert 'proposed_search_query' not in meta
            assert all('proposed_question' not in part for part in meta.get('question_parts', []))
        filters = [call for call in fake.calls if call['text']['format']['name'] == 'filter']
        assert [json.loads(call['input'][0]['content'])['original_question'] for call in filters] == [
            'lighting', 'display']
        assert all(json.loads(call['input'][0]['content'])['original_message'] == PROMPT for call in filters)
        plan = json.loads(next(row['content'].split('\n', 1)[1] for row in fake.calls[-1]['input']
            if row['content'].startswith('План частей вопроса:\n')))
        assert [part['question'] for part in plan] == ['lighting', 'display']
        assert len(fake.calls) == 4 and result['state']['summary']['api_requests'] == 5
        assert result['state']['summary']['cost_complete']
        assert result['state']['conversation_state']['goal'] == 'an image'
        assert {row['chunk_id'] for row in parent['metadata']['rag']['used_sources']} == {'lighting', 'display'}
        rows = result['state']['requests']
        agent = app.extensions['workspace'].agent()
        assert agent._rag_question_parts is None and agent._rag_search_query is None
        assert agent._rag_conversation_context is None
    finally:
        app.extensions['workspace'].close()
    reopened = create_app(data_dir=tmp_path, transport=V3Transport())
    try:
        assert reopened.test_client().get('/api/state').json['requests'] == rows
    finally:
        reopened.extensions['workspace'].close()


@pytest.mark.parametrize('extra', ['search_query', 'question'])
def test_v3_old_generated_rewrite_rejected_by_wire_and_host_before_state_embedding(tmp_path, parts_index, extra):
    def mutate(proposal):
        if extra == 'search_query':
            proposal[extra] = 'Invented owner'
        else:
            proposal['question_parts'][0][extra] = 'Invented owner'
    fake = V3Transport(mutate)
    app = create_app(data_dir=tmp_path, transport=fake)
    try:
        before = app.extensions['workspace'].agent().state()['conversation_state']
        result = app.test_client().post('/api/ask', json={'prompt': PROMPT}).json
        with pytest.raises(ValidationError):
            validate(fake.proposal, PREPARATION_FORMAT['schema'])
        assert result['status'] == 'rejected' and result['code'] == 'invalid_preparation'
        assert result['state']['conversation_state'] == before
        assert parts_index.calls == [] and len(fake.calls) == 1
        prep = next(row for row in result['state']['requests'] if row['metadata']['kind'] == 'conversation_preparation')
        assert prep['usage']['total_tokens'] == 110 and prep['usage']['reasoning_tokens'] == 2
        assert prep['cost_usd'] == '0.000015' and result['state']['summary']['cost_complete']
    finally:
        app.extensions['workspace'].close()


def test_v3_nonliteral_evidence_rejects_entire_goal_patch_and_retains_usage(tmp_path, parts_index):
    fake = V3Transport(lambda proposal: proposal['question_parts'].__setitem__(0, {'evidence': 'Area lighting'}))
    app = create_app(data_dir=tmp_path, transport=fake)
    try:
        before = app.extensions['workspace'].agent().state()['conversation_state']
        result = app.test_client().post('/api/ask', json={'prompt': PROMPT}).json
        assert result['status'] == 'rejected' and result['code'] == 'invalid_preparation'
        assert result['state']['conversation_state'] == before
        assert parts_index.calls == [] and len(fake.calls) == 1
        prep = next(row for row in result['state']['requests'] if row['metadata']['kind'] == 'conversation_preparation')
        assert prep['usage'] == {'input_tokens': 100, 'output_tokens': 10, 'total_tokens': 110,
            'cached_input_tokens': 0, 'cache_write_input_tokens': 0, 'reasoning_tokens': 2}
        assert prep['cost_usd'] == '0.000015'
        assert result['state']['summary']['api_requests'] == 1
    finally:
        app.extensions['workspace'].close()


def test_v3_literal_evidence_uses_current_total_3000_and_preserves_whitespace():
    evidence = [' ' + 'a' * 998 + ' ', 'b' * 1000, 'c' * 1000]
    prompt = ''.join(evidence)
    proposal = {'revision': 0, 'operations': [], 'question_parts': [{'evidence': e} for e in evidence]}
    parts = validate_question_parts(proposal, prompt)
    assert parts == [{'id': f'q{i}', 'question': e, 'evidence': e} for i, e in enumerate(evidence, 1)]
    assert sum(len(part['question']) for part in parts) == 3000
    with pytest.raises(ValueError):
        validate_question_parts({**proposal, 'question_parts': [{'evidence': 'a' * 1001}]}, 'a' * 1001)


def test_v3_prompt_requires_all_explicit_conditions_separately_without_owner_rewrites(tmp_path, parts_index):
    prompt = ('Моя цель — настроить освещение без изменения геометрии. '
              'Что делает настройка?')
    def explicit_memory(proposal):
        proposal['operations'] = [
            {'action': 'set', 'field': 'goal', 'key': '',
             'desired_outcome_evidence': 'настроить освещение',
             'conditions': [{'action': 'set', 'key': 'geometry',
                'value': 'Без изменения геометрии', 'evidence': 'без изменения геометрии'}]}]
        proposal['question_parts'] = [{'evidence': 'Что делает настройка?'}]
    fake = V3Transport(explicit_memory)
    fake.empty_scores = True
    app = create_app(data_dir=tmp_path, transport=fake)
    try:
        result = app.test_client().post('/api/ask', json={'prompt': prompt}).json
        assert result['code'] == 'no_context'
        state = result['state']['conversation_state']
        assert state['goal'] == 'настроить освещение'
        assert [row['value'] for row in state['constraints']] == ['Без изменения геометрии']
        assert json.loads(fake.calls[0]['input'][0]['content'])['current_message'] == prompt
        # Fixed operations verify host persistence and the actual sent prompt,
        # not the model's ability to infer the separate goal and condition.
        instructions = fake.calls[0]['instructions'].casefold()
        assert 'все' in instructions and 'отдельн' in instructions and 'внутри' in instructions
        assert 'только желаемый результат' in instructions
        assert 'в conditions этой goal-set операции' in instructions
        goal_schema = fake.calls[0]['text']['format']['schema']['properties']['operations']['items']['anyOf'][1]
        assert 'conditions' in goal_schema['required']
        assert goal_schema['properties']['conditions']['maxItems'] == 11
        assert 'гипотетическ' in instructions and 'не' in instructions
        assert 'search_query' not in instructions and 'cedarrelay' not in instructions
        assert 'владел' not in instructions and 'переформулир' not in instructions
    finally:
        app.extensions['workspace'].close()


def test_ordinary_instruction_requires_source_supported_aspects_without_forced_mechanism():
    from rag.chat import _ORDINARY_RAG_INSTRUCTIONS
    instruction = _ORDINARY_RAG_INSTRUCTIONS.casefold()
    assert 'основное назначение' in instruction
    assert 'каждой' in instruction and 'все подтверждённые аспекты' in instruction
    assert 'не достраивай' in instruction
    assert 'описывай только' in instruction and 'не добавляй механизм' in instruction
    assert 'основное назначение и механизм каждой' not in instruction


@pytest.mark.parametrize('nonliteral', [False, True])
def test_actual_l10_correction_exact_evidence_and_atomic_retention(tmp_path, parts_index, nonliteral):
    from agent.conversation_state import apply_preparation, empty_state
    prompt = ('Исправляю условие: теперь разрешены три источника Area вместо двух. '
              'Напомни, какие параметры каждого источника отвечают за форму и размеры.')
    old = 'Использую два источника Area.'
    initial = apply_preparation(empty_state(), {'revision': 0, 'search_query': old,
        'operations': [{'action': 'set', 'field': 'constraints', 'key': 'lights',
            'value': 'Два источника Area', 'evidence': 'два источника Area'}]}, old, request_id=1)
    def correction(proposal):
        proposal['operations'] = [{'action': 'set', 'field': 'constraints', 'key': 'lights',
            'value': 'Три источника Area', 'evidence': 'три источника Area вместо двух'}]
        proposal['question_parts'] = [{'evidence': 'какие параметры каждого источника Area'
            if nonliteral else 'какие параметры каждого источника'}]
    fake = V3Transport(correction)
    fake.empty_scores = True
    app = create_app(data_dir=tmp_path, transport=fake)
    try:
        agent = app.extensions['workspace'].agent()
        agent._store.save_conversation_state(initial, expected_revision=0, request_id=1)
        result = app.test_client().post('/api/ask', json={'prompt': prompt}).json
        state = result['state']['conversation_state']
        prep = next(row for row in result['state']['requests'] if row['metadata']['kind'] == 'conversation_preparation')
        assert prep['usage']['total_tokens'] == 110 and prep['cost_usd'] == '0.000015'
        if nonliteral:
            assert result['status'] == 'rejected' and result['code'] == 'invalid_preparation'
            assert state == initial and parts_index.calls == [] and len(fake.calls) == 1
        else:
            assert result['code'] == 'no_context'
            assert state['revision'] == 2
            assert [row['value'] for row in state['constraints']] == ['Три источника Area']
            assert state['provenance'][-1]['evidence'] == 'три источника Area вместо двух'
            assert len(parts_index.calls) == 1 and len(fake.calls) == 2
            assert result['state']['summary']['cost_complete']
    finally:
        app.extensions['workspace'].close()

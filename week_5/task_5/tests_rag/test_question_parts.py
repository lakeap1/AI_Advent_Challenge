"""Ordinary Luna question coverage through real preparation, packing, and ledger."""
from dataclasses import replace
import json
import re

import pytest
from jsonschema import ValidationError, validate

from app import create_app
from agent import load_config
from rag import grounding, retrieval
from rag.chat import RagChatAgent
from rag.chat_store import RagSQLiteStore
from rag.config import load_config as rag_config


PROMPT = 'My goal is an image. Check lighting and display.'
PARTS = [{'id': 'q1', 'question': 'Check lighting.', 'evidence': 'lighting'},
         {'id': 'q2', 'question': 'Check display.', 'evidence': 'display'}]


def response(value):
    return {'status': 'completed', 'model': 'gpt-6-luna', 'service_tier': 'default',
        'output': [{'type': 'message', 'role': 'assistant', 'status': 'completed',
            'content': [{'type': 'output_text', 'text': value if isinstance(value, str) else json.dumps(value)}]}],
        'usage': {'input_tokens': 100, 'output_tokens': 10, 'total_tokens': 110,
            'input_tokens_details': {'cached_tokens': 0, 'cache_write_tokens': 0},
            'output_tokens_details': {'reasoning_tokens': 2}}}


def chunk(name, text=None, *, file=None, start=0):
    text = text or f'{name} has documented properties.'
    return {'chunk_id': name, 'file': file or f'{name}.md', 'source': 'local',
        'title': name, 'section': ['Properties'], 'line_start': 1, 'line_end': 2,
        'start': start, 'end': start + len(text), 'document_hash': 'fixture',
        'text': text, 'embedding': [1.0] + [0.0] * 1535}


class Embedder:
    def __init__(self): self.calls = []
    def embed(self, texts):
        self.calls.append(texts)
        return {'model': 'text-embedding-3-small', 'vectors': [[1.0] + [0.0] * 1535],
            'usage': {'prompt_tokens': 5, 'total_tokens': 5}}


class PartsTransport:
    def __init__(self, *, malformed=None, fail_filter=None, unknown=False,
                 empty_scores=False, tool=False, bad_parts=None):
        self.calls = []
        self.malformed, self.fail_filter, self.unknown = malformed, fail_filter, unknown
        self.empty_scores, self.tool, self.bad_parts = empty_scores, tool, bad_parts
        self.final_calls = 0

    def create(self, payload, timeout):
        self.calls.append(payload)
        name = payload.get('text', {}).get('format', {}).get('name')
        if name == 'conversation_preparation':
            data = json.loads(payload['input'][0]['content'])
            proposal = {'revision': data['state']['revision'],
                'operations': [{'action': 'set', 'field': 'goal', 'key': '',
                    'desired_outcome_evidence': 'an image', 'conditions': []}],
                'question_parts': [{key: part[key] for key in ('evidence',)} for part in PARTS]}
            if self.bad_parts == 'missing': proposal.pop('question_parts')
            elif self.bad_parts is not None: proposal['question_parts'] = self.bad_parts
            return response(proposal)
        if name == 'filter':
            data = json.loads(payload['input'][0]['content'])
            selected = 'lighting' if data['original_question'] == 'lighting' else 'display'
            if self.fail_filter == selected: return response('{}')
            return response({'scores': {item['chunk_id']: {'score': 3 if not self.empty_scores
                and item['chunk_id'] == selected else 1, 'reason': 'Documented fact'}
                for item in data['candidates']}})
        if name == 'ordinary_parts_answer':
            self.final_calls += 1
            if self.tool and self.final_calls == 1:
                value = response('')
                value['output'] = [{'type': 'function_call', 'status': 'completed',
                    'name': 'lookup_wikipedia', 'call_id': 'call_parts',
                    'arguments': json.dumps({'query': 'graphics', 'language': 'en', 'limit': 1})}]
                return value
            plan = json.loads(next(item['content'].split('\n', 1)[1] for item in payload['input']
                if isinstance(item, dict) and str(item.get('content', '')).startswith('План частей вопроса:\n')))
            result = {'parts': {part['id']: {'status': 'answered',
                'claims': [{'text': f'{part["question"]} has documented properties.',
                    'source_labels': part['source_labels'][:1]}], 'clarification': ''}
                if part['source_labels'] and not (self.unknown and part['id'] == 'q2') else
                {'status': 'unknown', 'claims': [], 'clarification': 'Please clarify.'} for part in plan}}
            if self.malformed == 'missing': result['parts'].pop('q2')
            elif self.malformed == 'extra': result['parts']['q3'] = result['parts']['q1']
            elif self.malformed == 'cross': result['parts']['q1']['claims'][0]['source_labels'] = result['parts']['q2']['claims'][0]['source_labels']
            elif self.malformed == 'empty': result['parts']['q1']['claims'] = []
            elif self.malformed == 'clarification': result['parts']['q2']['clarification'] = 'A missing detail.'
            elif self.malformed == 'duplicate':
                return response('{"parts":{"q1":' + json.dumps(result['parts']['q1'])
                    + ',"q1":' + json.dumps(result['parts']['q1']) + ',"q2":' + json.dumps(result['parts']['q2']) + '}}')
            return response(result)
        raise AssertionError(f'unexpected stage {name}')


@pytest.fixture
def parts_index(monkeypatch):
    from rag import chat
    chunks = [chunk('lighting'), chunk('display')]
    monkeypatch.setattr(chat, 'read_index', lambda *_: chunks)
    embedder = Embedder()
    monkeypatch.setattr(chat, 'OpenAIEmbedder', lambda _: embedder)
    return embedder


def ask(tmp_path, transport):
    app = create_app(data_dir=tmp_path, transport=transport)
    result = app.test_client().post('/api/ask', json={'prompt': PROMPT})
    return app, result.json


def test_two_independent_filters_cover_both_parts_once_and_restore_ledger(tmp_path, parts_index):
    fake = PartsTransport()
    app, result = ask(tmp_path, fake)
    assert result['status'] == 'ok', result
    query = parts_index.calls[0][0]
    assert query.startswith(PROMPT) and 'an image' in query
    filters = [call for call in fake.calls if call['text']['format']['name'] == 'filter']
    assert [json.loads(call['input'][0]['content'])['original_question'] for call in filters] == ['lighting', 'display']
    assert [json.loads(call['input'][0]['content'])['candidates'] for call in filters][0] == json.loads(filters[1]['input'][0]['content'])['candidates']
    assert len(fake.calls) == 4
    assert all(call['model'] == 'gpt-6-luna' for call in fake.calls)
    assert [call['max_output_tokens'] for call in fake.calls] == [4000, 3000, 3000, 1600]
    rows = result['state']['requests']
    children = [row for row in rows if row['metadata']['kind'] == 'relevance_filter']
    assert [row['metadata']['part_id'] for row in children] == ['q1', 'q2']
    assert [row['metadata']['query'] for row in children] == ['lighting', 'display']
    assert children[0]['metadata']['scores']['lighting']['score'] == 3
    assert all(row['cost_usd'] == '0.000015' and row['usage']['reasoning_tokens'] == 2 for row in children)
    parent = next(row for row in rows if row['metadata']['kind'] == 'answer')
    rag = parent['metadata']['rag']
    assert rag['filter_request_ids'] == [row['id'] for row in children]
    assert rag['filter_request_id'] == children[0]['id']
    assert {source['chunk_id'] for source in rag['used_sources']} == {'lighting', 'display'}
    assert {key: value['status'] for key, value in rag['part_coverage'].items()} == {'q1': 'answered', 'q2': 'answered'}
    assert result['state']['summary']['api_requests'] == 5
    assert result['state']['conversation_state']['goal'] == 'an image'
    app.extensions['workspace'].close()
    restored = create_app(data_dir=tmp_path, transport=PartsTransport())
    assert restored.test_client().get('/api/state').json['requests'] == rows
    restored.extensions['workspace'].close()


@pytest.mark.parametrize('malformed', ['missing', 'extra', 'cross', 'empty', 'duplicate'])
def test_invalid_terminal_coverage_is_rejected_and_billed(tmp_path, parts_index, malformed):
    app, result = ask(tmp_path, PartsTransport(malformed=malformed))
    assert result['status'] == 'rejected'
    parent = next(row for row in result['state']['requests'] if row['metadata']['kind'] == 'answer')
    assert parent['cost_usd'] == '0.000015'
    assert parent['output_policy']['status'] == 'rejected'
    assert result['state']['conversation_state']['goal'] == 'an image'
    assert len(result['state']['messages']) == 1
    app.extensions['workspace'].close()


def test_multipart_wire_schema_matches_host_rejection_without_retry_or_lost_cost(tmp_path, parts_index):
    fake = PartsTransport(malformed='clarification')
    app, result = ask(tmp_path, fake)
    try:
        final = fake.calls[-1]
        invalid = {'parts': {'q1': {'status': 'answered', 'claims': [
            {'text': 'A documented fact.', 'source_labels': ['S1']}], 'clarification': ''},
            'q2': {'status': 'answered', 'claims': [
                {'text': 'Another fact.', 'source_labels': ['S2']}], 'clarification': 'A missing detail.'}}}
        with pytest.raises(ValidationError):
            validate(invalid, final['text']['format']['schema'])
        assert 'status=answered' in final['instructions'] and 'clarification=""' in final['instructions']
        assert 'status=unknown' in final['instructions'] and 'claims=[]' in final['instructions']
        assert result['status'] == 'rejected'
        parent = next(row for row in result['state']['requests'] if row['metadata']['kind'] == 'answer')
        assert parent['output_policy']['status'] == 'rejected' and parent['cost_usd'] == '0.000015'
        assert parent['usage']['total_tokens'] == 110
        assert result['state']['summary']['api_requests'] == 5 and result['state']['summary']['cost_complete']
        assert result['state']['conversation_state']['goal'] == 'an image'
        assert len(result['state']['messages']) == 1
        assert len(fake.calls) == 4 and len(parts_index.calls) == 1
        assert [call['max_output_tokens'] for call in fake.calls] == [4000, 3000, 3000, 1600]
        assert [call['reasoning']['effort'] for call in fake.calls] == ['high', 'medium', 'medium', 'medium']
        assert all(call['model'] == 'gpt-6-luna' for call in fake.calls)
    finally:
        app.extensions['workspace'].close()


@pytest.mark.parametrize('parts', ['missing', [], [{'evidence': 'not current'}],
    [{'evidence': 'lighting', 'id': 'q9'}],
    [{'evidence': 'lighting' * 150}],
    [{'evidence': 'lighting'}] * 4])
def test_invalid_question_parts_stop_before_memory_write_and_embedding(tmp_path, parts_index, parts):
    fake = PartsTransport(bad_parts=parts)
    app, result = ask(tmp_path, fake)
    assert result['status'] == 'rejected'
    assert result['state']['conversation_state']['goal'] == ''
    assert parts_index.calls == [] and len(fake.calls) == 1
    prep = next(row for row in result['state']['requests'] if row['metadata']['kind'] == 'conversation_preparation')
    assert prep['cost_usd'] == '0.000015'
    app.extensions['workspace'].close()


def test_second_filter_failure_keeps_first_cost_and_accepted_memory(tmp_path, parts_index):
    fake = PartsTransport(fail_filter='display')
    app, result = ask(tmp_path, fake)
    assert result['status'] == 'rejected'
    assert len(fake.calls) == 3 and len(parts_index.calls) == 1
    filters = [row for row in result['state']['requests'] if row['metadata']['kind'] == 'relevance_filter']
    assert [row['status'] for row in filters] == ['ok', 'rejected']
    assert [row['cost_usd'] for row in filters] == ['0.000015', '0.000015']
    assert result['state']['conversation_state']['goal'] == 'an image'
    app.extensions['workspace'].close()


def test_mixed_unknown_renders_gap_and_all_empty_skips_final(tmp_path, parts_index):
    app, result = ask(tmp_path / 'partial', PartsTransport(unknown=True))
    assert result['status'] == 'ok'
    assert 'Не знаю.' in result['text'] and 'display' in result['text']
    parent = next(row for row in result['state']['requests'] if row['metadata']['kind'] == 'answer')
    assert [item['chunk_id'] for item in parent['metadata']['rag']['used_sources']] == ['lighting']
    app.extensions['workspace'].close()
    fake = PartsTransport(empty_scores=True)
    app, result = ask(tmp_path / 'empty', fake)
    assert result['status'] == 'no_context' and len(fake.calls) == 3
    assert result['state']['conversation_state']['goal'] == 'an image'
    app.extensions['workspace'].close()


def test_multipart_mcp_continuation_keeps_plan_without_refilter_and_double_charge(tmp_path, parts_index):
    from rag import chat
    fake = PartsTransport(tool=True)
    store = RagSQLiteStore(tmp_path / 'tools.sqlite3')
    store.set_dialogue_kind('ordinary')

    class References:
        def fetch(self, provider, query, language, community, *, limit):
            return {'provider': provider, 'query': query, 'sources': [], 'metadata': {}}

    agent = RagChatAgent(load_config(), fake, store, index_data_dir=tmp_path,
        ordinary_final_reasoning_effort='medium', retrieval_client=References())
    result = agent.run(PROMPT)
    assert result.status == 'ok', result
    assert len(fake.calls) == 5 and len(parts_index.calls) == 1
    rows = agent.state()['requests']
    assert len([row for row in rows if row['metadata']['kind'] == 'relevance_filter']) == 2
    assert len([row for row in rows if row['metadata']['kind'] == 'mcp_step']) == 1
    assert agent.state()['summary']['api_requests'] == 6
    assert agent.state()['summary']['cost_complete']
    agent.close()


def test_fair_packer_prefers_coverage_with_overlap_dedup_and_exact_bytes():
    config = replace(rag_config(), top_k_after=2, max_context_tokens=450)
    chunks = [chunk('big', 'a' * 310), chunk('small', 'a' * 30), chunk('other', 'б' * 30)]
    scored = [(1.0, chunks[0]), (.8, chunks[1]), (.5, chunks[2])]
    scores = {'q1': {'big': {'score': 3}, 'small': {'score': 2}, 'other': {'score': 0}},
              'q2': {'big': {'score': 1}, 'small': {'score': 0}, 'other': {'score': 3}}}
    sources, context, mapping, coverage = retrieval.select_part_context(scored, scores, config)
    assert {source['chunk_id'] for source in sources} == {'small', 'other'}
    assert len(context.encode('utf-8')) <= 450
    assert all(mapping.values())
    shared = chunk('shared', 'one fact', file='same.md')
    overlap = chunk('overlap', 'another fact', file='same.md', start=1)
    scores = {part: {'shared': {'score': 3}, 'overlap': {'score': 3}} for part in ('q1', 'q2')}
    sources, context, mapping, coverage = retrieval.select_part_context([(1., shared), (.9, overlap)], scores, config)
    assert len(sources) == 1 and mapping['q1'] == mapping['q2'] == ['S1']


def test_fair_packer_marks_budget_excluded_without_forcing_low_scores():
    config = replace(rag_config(), top_k_after=1, max_context_tokens=6000)
    scored = [(1., chunk('lighting')), (.9, chunk('display')), (.8, chunk('irrelevant'))]
    scores = {'q1': {'lighting': {'score': 3}, 'display': {'score': 0}, 'irrelevant': {'score': 1}},
              'q2': {'lighting': {'score': 0}, 'display': {'score': 3}, 'irrelevant': {'score': 1}}}
    sources, context, mapping, coverage = retrieval.select_part_context(scored, scores, config)
    assert len(sources) == 1
    assert sorted(item['source_status'] for item in coverage.values()) == ['budget_excluded', 'selected']
    assert 'irrelevant' not in {item['chunk_id'] for item in sources}


def test_unary_uses_parts_answer_wire_and_records_coverage(tmp_path, parts_index):
    from tests_rag.grounded_fake import GroundedTransport
    fake = GroundedTransport('Lighting is documented.')
    app, result = ask(tmp_path, fake)
    assert result['status'] == 'ok', result
    assert [call['text']['format']['name'] for call in fake.calls] == [
        'conversation_preparation', 'filter', 'ordinary_parts_answer']
    assert len(parts_index.calls) == 1
    parent = next(row for row in result['state']['requests'] if row['metadata']['kind'] == 'answer')
    rag = parent['metadata']['rag']
    assert list(rag['part_coverage']) == ['q1']
    assert rag['part_coverage']['q1']['status'] == 'answered'
    assert rag['grounding']['claims'][0]['text'] == 'Lighting is documented.'
    assert 'parts' not in result['text'] and 'q1' not in result['text']
    app.extensions['workspace'].close()


def test_part_without_packed_sources_cannot_claim_other_part_source():
    source = {**chunk('lighting'), 'label': 'S1'}
    raw = json.dumps({'parts': {part['id']: {'status': 'answered',
        'claims': [{'text': 'A fact', 'source_labels': ['S1']}], 'clarification': ''}
        for part in PARTS}})
    with pytest.raises(grounding.GroundingError, match='unknown_source'):
        grounding.parse_part_grounding(raw, PARTS, [source], {'q1': ['S1'], 'q2': []})


def test_escaped_surrogate_preparation_rejects_before_memory_commit(tmp_path, parts_index):
    class EscapedPreparation(PartsTransport):
        def create(self, payload, timeout):
            value = super().create(payload, timeout)
            if payload['text']['format']['name'] == 'conversation_preparation':
                raw = value['output'][0]['content'][0]['text']
                # The raw provider text is valid ASCII/UTF-8. Only decoded
                # evidence data contains the malformed surrogate.
                assert raw.isascii() and '\\ud800' in raw
                raw.encode('utf-8')
            return value

    fake = EscapedPreparation(bad_parts=[{'evidence': '\ud800'}])
    app = create_app(data_dir=tmp_path, transport=fake)
    agent = app.extensions['workspace'].agent()
    before = agent.state()['conversation_state']
    try:
        result = agent.run(PROMPT)
        assert result.status == 'rejected' and result.code == 'invalid_preparation'
        state = agent.state()
        assert state['conversation_state'] == before
        assert len(fake.calls) == 1 and fake.final_calls == 0 and parts_index.calls == []
        rows = state['requests']
        assert all(row['status'] != 'pending' for row in rows)
        parent = next(row for row in rows if row['metadata']['kind'] == 'answer')
        prep = next(row for row in rows if row['metadata']['kind'] == 'conversation_preparation')
        assert parent['status'] == prep['status'] == 'rejected'
        assert prep['cost_usd'] == '0.000015' and prep['usage']['total_tokens'] == 110
        assert prep['output_policy']['status'] == 'rejected'
        assert state['summary']['api_requests'] == 1
        assert all(message['role'] == 'user' for message in state['messages'])
    finally:
        app.extensions['workspace'].close()


def test_preparation_wire_preserves_literal_evidence_and_independent_topics(tmp_path, parts_index):
    from tests_rag.grounded_fake import GroundedTransport
    fake = GroundedTransport('A documented setting.')
    app, result = ask(tmp_path, fake)
    try:
        assert result['status'] == 'ok'
        preparation = fake.calls[0]
        instructions = preparation['instructions'].casefold()
        # Guard the provider boundary, not a full prompt snapshot. The fake
        # does not infer entities or topics and cannot prove semantic quality.
        assert 'точную непустую подстроку' in instructions
        assert 'search_query' not in instructions and 'cedarrelay' not in instructions
        assert re.search(r'общая цель.{0,120}не объединяет.{0,120}тем', instructions, re.S)
        assert 'перед выдачей' in instructions and 'самостоятельн' in instructions
        data = json.loads(preparation['input'][0]['content'])
        assert data['current_message'] == PROMPT and data['state']['revision'] == 0
        assert preparation['text']['format']['schema']['properties']['question_parts']['maxItems'] == 3
        assert len(fake.calls) == 3 and len(parts_index.calls) == 1
        assert preparation['model'] == 'gpt-6-luna'
        assert preparation['max_output_tokens'] == 4000
        assert preparation['reasoning']['effort'] == 'high'
    finally:
        app.extensions['workspace'].close()


@pytest.mark.parametrize('multipart', [False, True])
def test_ordinary_final_wire_explains_setting_purpose_before_goal_relation(tmp_path, parts_index, multipart):
    from tests_rag.grounded_fake import GroundedTransport
    fake = PartsTransport() if multipart else GroundedTransport('A documented setting.')
    app, result = ask(tmp_path, fake)
    try:
        assert result['status'] == 'ok'
        final = fake.calls[-1]
        instructions = final['instructions'].casefold()
        assert 'основное назначение' in instructions and 'не добавляй механизм' in instructions
        assert re.search(r'сначала.{0,220}назначение.{0,350}затем.{0,160}цел', instructions, re.S)
        assert re.search(r'не заменяй.{0,150}назначение.{0,120}частным свойством', instructions, re.S)
        assert 'выбранным фрагментам' in instructions and 'не достраивай' in instructions
        expected_filters = 2 if multipart else 1
        assert final['text']['format']['name'] == 'ordinary_parts_answer'
        assert [call['max_output_tokens'] for call in fake.calls] == [4000] + [3000] * expected_filters + [1600]
        assert [call['reasoning']['effort'] for call in fake.calls] == ['high'] + ['medium'] * expected_filters + ['medium']
        assert all(call['model'] == 'gpt-6-luna' for call in fake.calls)
        assert len(parts_index.calls) == 1
        state = result['state']
        assert state['summary']['api_requests'] == 3 + expected_filters
        assert state['summary']['cost_complete']
        assert len([row for row in state['requests'] if row['metadata']['kind'] == 'relevance_filter']) == expected_filters
        assert state['conversation_state']['revision'] == (1 if multipart else 0)
    finally:
        app.extensions['workspace'].close()


def test_formal_final_does_not_receive_ordinary_purpose_instruction(tmp_path, parts_index):
    from tests_rag.grounded_fake import GroundedTransport
    fake = GroundedTransport('A documented setting.')
    app = create_app(data_dir=tmp_path, transport=fake)
    client = app.test_client()
    try:
        client.post('/api/task', json={'name': 'Explicit formal task'})
        result = client.post('/api/ask', json={'prompt': 'Check a setting.', 'use_rag': True}).json
        assert result['status'] == 'ok'
        final = next(call for call in fake.calls
            if call.get('text', {}).get('format', {}).get('name') == 'task_response')
        assert 'основное назначение' not in final['instructions'].casefold()
        assert all(call.get('text', {}).get('format', {}).get('name') != 'conversation_preparation'
            for call in fake.calls)
        assert len(parts_index.calls) == 1
        filter_call = next(call for call in fake.calls if call['text']['format']['name'] == 'filter')
        assert set(json.loads(filter_call['input'][0]['content'])) == {'original_question', 'candidates'}
        assert 'conversation_context' not in json.dumps(final['input'])
    finally:
        app.extensions['workspace'].close()


def test_complete_ordinary_filter_context_still_obeys_input_byte_limit(tmp_path, parts_index):
    fake = PartsTransport()
    app = create_app(data_dir=tmp_path, transport=fake)
    try:
        agent = app.extensions['workspace'].agent()
        agent._rag_config = replace(agent._rag_config, max_filter_input_bytes=1)
        result = agent.run(PROMPT)
        assert result.status == 'rejected' and result.code == 'filter_input_too_large'
        assert len(fake.calls) == 1 and len(parts_index.calls) == 1
        state = agent.state()
        assert state['conversation_state']['goal'] == 'an image'
        assert state['summary']['api_requests'] == 2
        assert not [row for row in state['requests'] if row['metadata']['kind'] == 'relevance_filter']
        assert agent._rag_conversation_context is None
    finally:
        app.extensions['workspace'].close()



def test_oversized_current_tokens_keep_preparation_cost_and_original_state(tmp_path, parts_index):
    prompt = '\U0001f9ec' * 4000

    class Oversized(PartsTransport):
        def create(self, payload, timeout):
            self.calls.append(payload)
            assert payload['text']['format']['name'] == 'conversation_preparation'
            data = json.loads(payload['input'][0]['content'])
            assert data['current_message'] == prompt
            return response({'revision': data['state']['revision'],
                'operations': [{'action': 'set', 'field': 'goal', 'key': '',
                    'desired_outcome_evidence': prompt[:1], 'conditions': []}],
                'question_parts': [{'evidence': prompt[:1]}]})

    fake = Oversized()
    app = create_app(data_dir=tmp_path, transport=fake)
    try:
        before = app.extensions['workspace'].agent().state()['conversation_state']
        result = app.test_client().post('/api/ask', json={'prompt': prompt}).json
        assert result['status'] == 'rejected' and result['code'] == 'invalid_preparation'
        assert '8000 токенов cl100k_base' in result['text']
        assert result['state']['conversation_state'] == before
        assert len(fake.calls) == 1 and parts_index.calls == []
        rows = result['state']['requests']
        assert all(row['status'] != 'pending' for row in rows)
        prep = next(row for row in rows if row['metadata']['kind'] == 'conversation_preparation')
        assert prep['cost_usd'] == '0.000015' and prep['usage']['total_tokens'] == 110
        assert prep['output_policy']['status'] == 'rejected'
        assert result['state']['summary']['api_requests'] == 1
        assert result['state']['summary']['cost_complete']
        assert result['state']['messages'][0]['content'] == prompt
    finally:
        app.extensions['workspace'].close()

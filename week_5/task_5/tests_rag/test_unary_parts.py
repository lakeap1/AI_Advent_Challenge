"""One prepared ordinary part at the provider, parser and durable HTTP boundary."""
import json

import pytest
from jsonschema import ValidationError, validate

from app import create_app
from agent import load_config
from rag.chat import RagChatAgent
from rag.chat_store import RagSQLiteStore
from tests_rag.test_question_parts import PartsTransport, PROMPT, parts_index, response


# Minimal exact slice of a rejected provider response. Only the first
# claim and clarification are retained; no encrypted reasoning or semantic
# judgment is used. The shape, not the truth of this claim, is the oracle.
L1_SLICE = {
    'status': 'answered',
    'claims': [{'text': 'В Blender 4.4 Area Light имитирует светящуюся поверхность. Для теней Blender выборочно учитывает точки на площади источника, поэтому у такого света могут быть мягкие границы теней; источник с нулевым размером даёт более резкую границу.',
                'source_labels': ['S1']}],
    'clarification': 'Во фрагментах нет точных рекомендаций по числовым настройкам и расположению двух источников или описания того, как меняется мягкость при разных ненулевых размерах.'}


class UnaryTransport(PartsTransport):
    def __init__(self, final=None, *, tool=False):
        super().__init__(tool=tool)
        self.final = final or {'parts': {'q1': {'status': 'answered', 'claims': [
            {'text': 'Lighting has documented properties.', 'source_labels': ['S1']}],
            'clarification': ''}}}

    def create(self, payload, timeout):
        name = payload.get('text', {}).get('format', {}).get('name')
        if name == 'conversation_preparation':
            value = super().create(payload, timeout)
            data = json.loads(value['output'][0]['content'][0]['text'])
            data['question_parts'] = [{'evidence': 'lighting'}]
            self.proposal = data
            return response(data)
        if name in ('grounded_answer', 'ordinary_parts_answer'):
            self.calls.append(payload)
            self.final_calls += 1
            if self.tool and self.final_calls == 1:
                value = response('')
                value['output'] = [{'type': 'function_call', 'status': 'completed',
                    'name': 'lookup_wikipedia', 'call_id': 'call_unary',
                    'arguments': json.dumps({'query': 'graphics', 'language': 'en', 'limit': 1})}]
                return value
            return response(self.final)
        return super().create(payload, timeout)


def parent_row(state):
    return next(row for row in state['requests'] if row['metadata']['kind'] == 'answer')


def plan(payload):
    return json.loads(next(row['content'].split('\n', 1)[1] for row in payload['input']
        if isinstance(row, dict) and str(row.get('content', '')).startswith('План частей вопроса:\n')))


@pytest.mark.parametrize('unknown', [False, True])
def test_prepared_unary_returns_answer_or_explicit_part_gap_and_reopens(tmp_path, parts_index, unknown):
    final = {'parts': {'q1': {'status': 'unknown', 'claims': [],
                            'clarification': 'Please clarify the lighting.'}}} if unknown else None
    fake = UnaryTransport(final)
    app = create_app(data_dir=tmp_path, transport=fake)
    try:
        result = app.test_client().post('/api/ask', json={'prompt': PROMPT}).json
        assert result['status'] == 'ok', result
        if unknown:
            assert result['text'] == 'Не знаю. lighting Переданные локальные источники не подтверждают ответ. Уточните эту часть вопроса.'
        else:
            assert result['text'] == 'Lighting has documented properties. [S1]'
        state = result['state']
        rows = state['requests']
        rag = parent_row(state)['metadata']['rag']
        final_payload = fake.calls[-1]
        assert final_payload['text']['format']['name'] == 'ordinary_parts_answer'
        assert plan(final_payload) == [{'id': 'q1', 'question': 'lighting',
            'source_labels': ['S1'], 'source_status': 'selected'}]
        assert [source['chunk_id'] for source in rag['sources']] == ['lighting']
        assert rag['part_sources'] == {'q1': ['S1']}
        assert rag['part_coverage']['q1']['status'] == ('unknown' if unknown else 'answered')
        assert [source['label'] for source in rag['used_sources']] == ([] if unknown else ['S1'])
        assert rag['context_budget']['used_utf8_bytes'] == len(rag['context'].encode('utf-8'))
        assert rag['context_budget']['used_utf8_bytes'] <= rag['context_budget']['limit_tokens']
        assert state['conversation_state']['goal'] == 'an image'
        prep = next(row for row in rows if row['metadata']['kind'] == 'conversation_preparation')
        assert prep['metadata']['raw_proposal'] == fake.proposal
        assert json.loads(prep['metadata']['raw_preparation_text']) == fake.proposal
        assert state['summary']['api_requests'] == 4 and state['summary']['cost_complete']
        assert parent_row(state)['usage']['total_tokens'] == 110
        assert parent_row(state)['cost_usd'] == '0.000015'
        assert len(fake.calls) == 3 and len(parts_index.calls) == 1
        assert [call['max_output_tokens'] for call in fake.calls] == [4000, 3000, 1600]
        assert [call['reasoning']['effort'] for call in fake.calls] == ['high', 'medium', 'medium']
        assert all(call['model'] == 'gpt-6-luna' for call in fake.calls)
    finally:
        app.extensions['workspace'].close()
    reopened = create_app(data_dir=tmp_path, transport=UnaryTransport())
    try:
        assert reopened.test_client().get('/api/state').json['requests'] == rows
    finally:
        reopened.extensions['workspace'].close()


def test_recorded_unary_incompatible_draft_is_excluded_by_requested_wire_and_billed(tmp_path, parts_index):
    fake = UnaryTransport({'parts': {'q1': L1_SLICE}})
    app = create_app(data_dir=tmp_path, transport=fake)
    try:
        result = app.test_client().post('/api/ask', json={'prompt': PROMPT}).json
        schema = fake.calls[-1]['text']['format']['schema']
        # The former real flat schema accepted this same invalid host shape.
        from rag.grounding import response_format
        validate(L1_SLICE, response_format()['schema'])
        assert schema['properties']['parts']['required'] == ['q1']
        with pytest.raises(ValidationError):
            validate(fake.final, schema)
        assert result['status'] == 'rejected' and result['code'] == 'invalid_grounding'
        state = result['state']
        parent = parent_row(state)
        assert parent['usage']['total_tokens'] == 110 and parent['usage']['reasoning_tokens'] == 2
        assert parent['cost_usd'] == '0.000015' and parent['output_policy']['status'] == 'rejected'
        assert state['conversation_state']['goal'] == 'an image'
        assert len(state['messages']) == 1 and state['messages'][0]['role'] == 'user'
        assert state['summary']['api_requests'] == 4 and state['summary']['cost_complete']
        assert len(fake.calls) == 3 and fake.final_calls == 1 and len(parts_index.calls) == 1
    finally:
        app.extensions['workspace'].close()


@pytest.mark.parametrize('final,code', [
    ({'parts': {}}, 'invalid_part_coverage'),
    ({'parts': {'q1': L1_SLICE, 'q2': L1_SLICE}}, 'invalid_part_coverage'),
    ({'parts': {'q1': {'status': 'answered', 'claims': [], 'clarification': ''}}}, 'invalid_grounding'),
    ({'parts': {'q1': {'status': 'unknown', 'claims': L1_SLICE['claims'], 'clarification': 'Clarify'}}}, 'invalid_grounding'),
    ({'parts': {'q1': {'status': 'answered', 'claims': [{'text': 'A fact', 'source_labels': ['S9']}], 'clarification': ''}}}, 'unknown_source'),
])
def test_unary_invalid_coverage_and_claims_stop_without_retry(tmp_path, parts_index, final, code):
    fake = UnaryTransport(final)
    app = create_app(data_dir=tmp_path, transport=fake)
    try:
        result = app.test_client().post('/api/ask', json={'prompt': PROMPT}).json
        assert result['status'] == 'rejected' and result['code'] == code
        assert parent_row(result['state'])['cost_usd'] == '0.000015'
        assert len(result['state']['messages']) == 1
        assert fake.final_calls == 1 and len(fake.calls) == 3 and len(parts_index.calls) == 1
    finally:
        app.extensions['workspace'].close()


def test_unary_tool_continuation_preserves_plan_without_retrieval_repeat(tmp_path, parts_index):
    class References:
        def fetch(self, provider, query, language, community, *, limit):
            return {'provider': provider, 'query': query, 'sources': [], 'metadata': {}}
    fake = UnaryTransport(tool=True)
    store = RagSQLiteStore(tmp_path / 'tools.sqlite3')
    store.set_dialogue_kind('ordinary')
    agent = RagChatAgent(load_config(), fake, store, index_data_dir=tmp_path,
        ordinary_final_reasoning_effort='medium', retrieval_client=References())
    try:
        result = agent.run(PROMPT)
        assert result.status == 'ok', result
        finals = fake.calls[-2:]
        assert [call['text']['format']['name'] for call in finals] == ['ordinary_parts_answer'] * 2
        assert plan(finals[0]) == plan(finals[1]) == [{'id': 'q1', 'question': 'lighting',
            'source_labels': ['S1'], 'source_status': 'selected'}]
        assert len(parts_index.calls) == 1 and len(fake.calls) == 4
        state = agent.state()
        assert len([row for row in state['requests'] if row['metadata']['kind'] == 'relevance_filter']) == 1
        assert len([row for row in state['requests'] if row['metadata']['kind'] == 'mcp_step']) == 1
        assert state['summary']['api_requests'] == 5 and state['summary']['cost_complete']
    finally:
        agent.close()

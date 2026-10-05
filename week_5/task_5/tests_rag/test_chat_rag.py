"""Main-chat RAG contract at the HTTP and durable dialogue boundary."""
from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path

import httpx
import pytest

from app import create_app
from agent import load_config
from indexing.client import EmbeddingError, OpenAIEmbedder
from indexing.config import load_config as load_indexing_config
from rag.chat import RagChatAgent
from rag.chat import RagProfileWorkspace
from rag.chat_store import RagSQLiteStore
from rag.config import load_config as load_rag_config
from test_rag import indexed, Embedder
from test_grounding import fixture_grounding


def response(text):
    return {'status': 'completed', 'model': 'gpt-6-luna', 'service_tier': 'default',
            'output': [{'type': 'message', 'role': 'assistant', 'status': 'completed',
                        'content': [{'type': 'output_text', 'text': text}]}],
            'usage': {'input_tokens': 100, 'output_tokens': 10, 'total_tokens': 110,
                      'input_tokens_details': {'cached_tokens': 0, 'cache_write_tokens': 0},
                      'output_tokens_details': {'reasoning_tokens': 0}}}


class Fake:
    def __init__(self, replies=()):
        self.replies = list(replies)
        self.calls = []

    def create(self, payload, timeout):
        self.calls.append(payload)
        if payload.get('text', {}).get('format', {}).get('name') == 'conversation_preparation':
            data = json.loads(payload['input'][0]['content'])
            return response(json.dumps({'revision': data['state']['revision'],
                'search_query': data['current_message'], 'operations': []}))
        if payload.get('text', {}).get('format', {}).get('name') == 'filter':
            candidate_ids = json.loads(payload['input'][0]['content'])['candidates']
            explicit = False
            if self.replies:
                try:
                    explicit = 'scores' in json.loads(self.replies[0]['output'][0]['content'][0]['text'])
                except (KeyError, IndexError, TypeError, ValueError):
                    pass
            reply = self.replies.pop(0) if explicit else response(json.dumps({'scores': {
                item['chunk_id']: {'score': 3, 'reason': 'Fixture evidence'} for item in candidate_ids}}))
        else:
            reply = self.replies.pop(0)
        if 'TASK_RESPONSE_JSON' in payload['instructions']:
            state_text = payload['instructions'].split('TASK_STATE —', 1)[1].split('TASK_RESPONSE_JSON', 1)[0]
            state = json.loads(state_text[state_text.index('{'):].strip())
            original = reply['output'][0]['content'][0]['text']
            grounding = payload['text']['format']['schema']['properties']['answer']['type'] == 'object'
            reply = response(json.dumps(dict(answer=fixture_grounding(original) if grounding else original,
                event='stay', evidence='', **{key: state[key] for key in
                ('goal', 'current_step', 'expected_action', 'notes', 'plan')}), ensure_ascii=False))
        elif payload.get('text', {}).get('format', {}).get('name') == 'grounded_answer' and reply['output'][0]['type'] == 'message':
            reply = response(json.dumps(fixture_grounding(reply['output'][0]['content'][0]['text']), ensure_ascii=False))
        return reply


@pytest.fixture
def local_index(indexed, monkeypatch):
    root, data = indexed
    from rag import chat
    original = chat.read_index
    monkeypatch.setattr(chat, "read_index", lambda _root, _data, config: original(root, data, config))
    embedder = Embedder()
    monkeypatch.setattr(chat, "OpenAIEmbedder", lambda _config: embedder)
    return embedder


def test_main_chat_rag_persists_sources_and_independent_costs(tmp_path, local_index):
    fake = Fake([response("Alpha is documented [S1]."), response('{"operations": []}')])
    app = create_app(data_dir=tmp_path, transport=fake)
    client = app.test_client()
    assert client.post('/api/task', json={'name': 'Formal RAG accounting'}).json['state']['dialogue_kind'] == 'formal'
    result = client.post('/api/ask', json={'prompt': 'Where is alpha?', 'use_rag': True})
    assert result.status_code == 200, result.json
    assert result.json['text'] == 'Alpha is documented. [S1]'
    assert local_index.calls == [['Where is alpha?']]
    assert 'Unique alpha evidence' in json.dumps(fake.calls[0]['input'], ensure_ascii=False)
    assert fake.calls[0]['input'][0]['role'] == 'user'
    assert 'Unique alpha evidence' not in fake.calls[0]['instructions']
    state = result.json['state']
    parent = next(r for r in state['requests'] if r['metadata']['kind'] == 'answer')
    child = next(r for r in state['requests'] if r['metadata']['kind'] == 'query_embedding')
    assert child['metadata']['parent_request_id'] == parent['id']
    assert parent['metadata']['rag']['sources'][0]['file'] == 'alpha.md'
    assert parent['metadata']['rag']['embedding_request_id'] == child['id']
    assert state['retrievals'][0]['provider'] == 'local_index'
    assert state['retrievals'][0]['request_id'] == parent['id']
    assert child['usage']['total_tokens'] == 5
    assert Decimal(parent['cost_usd']) == Decimal('0.000015')
    assert Decimal(child['cost_usd']) == Decimal('0.0000001')
    assert state['summary']['api_requests'] == 4
    assert state['summary']['cost_complete']
    assert len([m for m in state['messages'] if m['role'] == 'assistant']) == 1
    app.extensions['workspace'].close()
    restored = create_app(data_dir=tmp_path, transport=Fake()).test_client().get('/api/state').json
    assert restored['requests'] == state['requests']
    assert restored['messages'] == state['messages']


def test_main_route_requests_low_reasoning_only_for_grounded_generation(tmp_path, local_index):
    fake = Fake([response('Alpha is documented [S1].'), response('{"operations": []}')])
    app = create_app(data_dir=tmp_path, transport=fake)
    client = app.test_client()
    assert client.post('/api/task', json={'name': 'Formal RAG tools'}).json['state']['dialogue_kind'] == 'formal'
    result = client.post('/api/ask', json={'prompt': 'Where is alpha?'})

    assert result.status_code == 200, result.json
    filter_payload = next(call for call in fake.calls
        if call.get('text', {}).get('format', {}).get('name') == 'filter')
    answer_payload = next(call for call in fake.calls
        if call.get('text', {}).get('format', {}).get('name') == 'task_response')
    assert filter_payload['reasoning']['effort'] == 'none'
    assert answer_payload['reasoning']['effort'] == 'low'
    assert answer_payload['tool_choice'] == 'auto'
    assert {'lookup_wikipedia', 'search_stackexchange'} <= {
        tool['name'] for tool in answer_payload['tools']}
    assert all(call['reasoning']['effort'] == 'none' for call in fake.calls
        if call is not answer_payload and call is not filter_payload)
    parent = next(item for item in result.json['state']['requests']
        if item['metadata']['kind'] == 'answer')
    assert parent['metadata']['requested_reasoning_effort'] == 'low'
    assert parent['metadata']['rag']['settings']['main_final_reasoning_effort'] == 'low'
    assert parent['metadata']['rag']['requested_reasoning_effort'] == 'low'
    app.extensions['workspace'].close()


def test_main_reasoning_setting_validates_and_old_toml_defaults_to_none(tmp_path):
    configured = load_rag_config()
    assert configured.main_final_reasoning_effort == 'low'
    legacy = tmp_path / 'legacy.toml'
    source = Path(__file__).resolve().parents[1] / 'rag' / 'config.toml'
    legacy.write_text(''.join(line for line in source.read_text(encoding='utf-8').splitlines(True)
        if not line.startswith('main_final_reasoning_effort')), encoding='utf-8')
    assert load_rag_config(legacy).main_final_reasoning_effort == 'none'
    with pytest.raises(ValueError):
        replace(configured, main_final_reasoning_effort='high').validate()


def test_opted_in_reasoning_leaves_plain_and_rewrite_machine_at_none(tmp_path, local_index):
    plain_fake = Fake([response('A plain answer.')])
    plain = RagChatAgent(load_config(), plain_fake,
        RagSQLiteStore(tmp_path / 'plain.sqlite3'), index_data_dir=tmp_path,
        final_reasoning_effort='low')
    assert plain.run('Hello', rag_mode='plain').status == 'ok'
    assert plain_fake.calls[0]['reasoning']['effort'] == 'none'
    plain.close()

    rag_fake = Fake([response('{"query":"Where is alpha?"}'),
        response('Alpha is documented [S1].')])
    rag = RagChatAgent(load_config(), rag_fake,
        RagSQLiteStore(tmp_path / 'rag.sqlite3'), index_data_dir=tmp_path,
        final_reasoning_effort='low')
    assert rag.run('Where is alpha?', rag_mode='rewrite_filter').status == 'ok'
    efforts = {call['text']['format']['name']: call['reasoning']['effort']
        for call in rag_fake.calls}
    assert efforts == {'rewrite': 'none', 'filter': 'none', 'grounded_answer': 'low'}
    rag.close()


@pytest.mark.parametrize('prompt', [
    'Какие возможности реализованы в этом локальном проекте?',
    'Подготовь обзор внешних источников по компьютерной графике.',
])
def test_rag_generation_prioritizes_local_project_evidence_without_disabling_external_tools(
        tmp_path, local_index, prompt):
    fake = Fake([response('Alpha is documented [S1].')])
    agent = RagChatAgent(load_config(), fake, RagSQLiteStore(tmp_path / 'chat.sqlite3'),
        index_data_dir=tmp_path)

    result = agent.run(prompt, use_rag=True)

    assert result.status == 'ok', result
    payload = fake.calls[-1]
    instructions = payload['instructions']
    assert instructions.index('MCP tools are optional') < instructions.index('Вопросы о локальном проекте')
    assert 'только по уже выбранным локальным фрагментам' in instructions
    assert 'не вызывай Wikipedia или Stack Exchange' in instructions
    assert 'вопрос о настроенных инструментах проекта' in instructions
    assert 'внешнее исследование, обзор или отчёт' in instructions
    assert payload['tool_choice'] == 'auto'
    assert {'lookup_wikipedia', 'search_stackexchange'} <= {
        tool['name'] for tool in payload['tools']}
    agent.close()


def test_filter_instruction_scores_direct_passage_in_mixed_topic_chunk(tmp_path, local_index):
    fake = Fake([response('Alpha is documented [S1].')])
    agent = RagChatAgent(load_config(), fake, RagSQLiteStore(tmp_path / 'chat.sqlite3'),
        index_data_dir=tmp_path)

    result = agent.run('Where is alpha?', rag_mode='filter')

    assert result.status == 'ok', result
    filter_payload = next(call for call in fake.calls
        if call.get('text', {}).get('format', {}).get('name') == 'filter')
    instructions = filter_payload['instructions']
    assert '0=не относится' in instructions
    assert '1=только тема' in instructions
    assert '2=частично полезное доказательство' in instructions
    assert '3=прямой ответ на вопрос или любой его подпункт' in instructions
    assert 'весь текст каждого фрагмента' in instructions
    assert 'хотя бы в одном участке' in instructions
    assert 'не обязан целиком' in instructions
    assert 'код или фактический поток выполнения' in instructions
    assert 'Не выводи отсутствующие факты' in instructions
    candidates = json.loads(filter_payload['input'][0]['content'])['candidates']
    assert any('Unique alpha evidence' in candidate['text'] for candidate in candidates)
    agent.close()


def test_invalid_index_stops_before_embedding_and_generation(tmp_path):
    fake = Fake()
    app = create_app(data_dir=tmp_path, transport=fake)
    result = app.test_client().post('/api/ask', json={'prompt': 'Find alpha', 'use_rag': True})
    assert result.status_code == 502
    assert result.json['code'] == 'invalid_index'
    assert [call['text']['format']['name'] for call in fake.calls] == ['conversation_preparation']
    parent = next(r for r in result.json['state']['requests'] if r['metadata']['kind'] == 'answer')
    assert parent['usage_status'] == 'not_requested'
    assert result.json['state']['summary']['api_requests'] == 1
    assert not any(r['metadata']['kind'] == 'query_embedding' for r in result.json['state']['requests'])


def test_unknown_source_rejects_after_paid_calls_without_assistant(tmp_path, local_index):
    fake = Fake([response('Unsupported [S99].')])
    app = create_app(data_dir=tmp_path, transport=fake)
    result = app.test_client().post('/api/ask', json={'prompt': 'Find alpha', 'use_rag': True})
    assert result.status_code == 400
    assert result.json['code'] == 'unknown_source'
    state = result.json['state']
    assert state['summary']['api_requests'] == 4
    assert state['summary']['cost_complete']
    assert not any(m['role'] == 'assistant' for m in state['messages'])
    assert [call['text']['format']['name'] for call in fake.calls] == [
        'conversation_preparation', 'filter', 'grounded_answer']


def test_embedding_failure_keeps_parent_unrequested_and_unknown_child_cost(tmp_path, local_index):
    local_index.error = EmbeddingError('interrupted', usage=None)
    fake = Fake()
    result = create_app(data_dir=tmp_path, transport=fake).test_client().post(
        '/api/ask', json={'prompt': 'Find alpha', 'use_rag': True})
    assert result.status_code == 502
    assert result.json['code'] == 'embedding_failed'
    state = result.json['state']
    parent = next(r for r in state['requests'] if r['metadata']['kind'] == 'answer')
    child = next(r for r in state['requests'] if r['metadata']['kind'] == 'query_embedding')
    assert parent['usage_status'] == 'not_requested'
    assert child['usage_status'] == 'unavailable' and child['cost_usd'] is None
    assert state['summary']['api_requests'] == 2
    assert state['summary']['unknown_cost_requests'] == 1
    assert not state['summary']['cost_complete']
    assert [call['text']['format']['name'] for call in fake.calls] == ['conversation_preparation']


def test_unexpected_embedding_model_keeps_usage_but_unknown_price(tmp_path, local_index):
    original = local_index.embed

    def unexpected(texts):
        value = original(texts)
        value['model'] = 'another-embedding-model'
        return value

    local_index.embed = unexpected
    result = create_app(data_dir=tmp_path, transport=Fake()).test_client().post(
        '/api/ask', json={'prompt': 'Find alpha', 'use_rag': True})
    assert result.status_code == 502
    child = next(r for r in result.json['state']['requests'] if r['metadata']['kind'] == 'query_embedding')
    assert child['usage']['total_tokens'] == 5
    assert child['metadata']['actual_model'] == 'another-embedding-model'
    assert child['cost_usd'] is None
    assert not result.json['state']['summary']['cost_complete']


def test_invalid_query_vector_rejects_embedding_policy_before_retrieval(tmp_path, local_index):
    local_index.vector = [0.0] * 1536
    fake = Fake()
    result = create_app(data_dir=tmp_path, transport=fake).test_client().post(
        '/api/ask', json={'prompt': 'Find alpha', 'use_rag': True})
    assert result.status_code == 502
    assert result.json['code'] == 'invalid_embedding'
    child = next(r for r in result.json['state']['requests'] if r['metadata']['kind'] == 'query_embedding')
    assert child['code'] == 'invalid_embedding'
    assert child['output_policy']['status'] == 'rejected'
    assert child['usage']['total_tokens'] == 5 and child['cost_usd'] is not None
    assert [call['text']['format']['name'] for call in fake.calls] == ['conversation_preparation']


def test_valid_embedding_remains_accepted_when_local_context_selection_fails(tmp_path, local_index, monkeypatch):
    from rag import chat
    monkeypatch.setattr(chat, 'select_context', lambda _ranked, _config: ([], ''))
    fake = Fake()
    result = create_app(data_dir=tmp_path, transport=fake).test_client().post(
        '/api/ask', json={'prompt': 'Find alpha', 'use_rag': True})
    assert result.status_code == 502
    assert result.json['code'] == 'retrieval_failed'
    child = next(r for r in result.json['state']['requests'] if r['metadata']['kind'] == 'query_embedding')
    assert child['status'] == 'ok' and child['output_policy']['status'] == 'accepted'
    assert child['usage']['total_tokens'] == 5 and child['cost_usd'] is not None
    assert [call['text']['format']['name'] for call in fake.calls] == [
        'conversation_preparation', 'filter']


def test_provider_embedding_model_mismatch_preserves_usage_without_using_configured_tariff(indexed, tmp_path, monkeypatch):
    from rag import chat
    root, index_data = indexed
    original = chat.read_index
    monkeypatch.setattr(chat, 'read_index', lambda _root, _data, config: original(root, index_data, config))
    def reply(_request):
        return httpx.Response(200, json={'model': 'different-embedding-model', 'data': [],
            'usage': {'prompt_tokens': 5, 'total_tokens': 5}})
    client = httpx.Client(transport=httpx.MockTransport(reply))
    embedder = OpenAIEmbedder(load_indexing_config(), api_key='fixture-key', client=client)
    monkeypatch.setattr(chat, 'OpenAIEmbedder', lambda _config: embedder)
    result = create_app(data_dir=tmp_path / 'chat', transport=Fake()).test_client().post(
        '/api/ask', json={'prompt': 'Find alpha', 'use_rag': True})
    assert result.status_code == 502
    child = next(r for r in result.json['state']['requests'] if r['metadata']['kind'] == 'query_embedding')
    assert child['metadata']['actual_model'] == 'different-embedding-model'
    assert child['metadata']['provider_usage'] == {'prompt_tokens': 5, 'total_tokens': 5}
    assert child['usage']['total_tokens'] == 5
    assert child['cost_usd'] is None
    assert not result.json['state']['summary']['cost_complete']
    client.close()


def test_provider_invalid_vector_with_confirmed_model_retains_known_cost(indexed, tmp_path, monkeypatch):
    from rag import chat
    root, index_data = indexed
    original = chat.read_index
    monkeypatch.setattr(chat, 'read_index', lambda _root, _data, config: original(root, index_data, config))
    def reply(_request):
        return httpx.Response(200, json={'model': 'text-embedding-3-small',
            'data': [{'index': 0, 'embedding': [0.0] * 1536}],
            'usage': {'prompt_tokens': 5, 'total_tokens': 5}})
    client = httpx.Client(transport=httpx.MockTransport(reply))
    embedder = OpenAIEmbedder(load_indexing_config(), api_key='fixture-key', client=client)
    monkeypatch.setattr(chat, 'OpenAIEmbedder', lambda _config: embedder)
    result = create_app(data_dir=tmp_path / 'chat', transport=Fake()).test_client().post(
        '/api/ask', json={'prompt': 'Find alpha', 'use_rag': True})
    assert result.status_code == 502 and result.json['code'] == 'invalid_embedding'
    child = next(r for r in result.json['state']['requests'] if r['metadata']['kind'] == 'query_embedding')
    assert child['metadata']['actual_model'] == 'text-embedding-3-small'
    assert child['output_policy']['status'] == 'rejected'
    assert Decimal(child['cost_usd']) == Decimal('0.0000001')
    assert result.json['state']['summary']['cost_complete']
    client.close()


def test_rag_context_survives_mcp_continuation_without_second_embedding(tmp_path, local_index):
    selection = response('')
    selection['output'] = [{'type': 'function_call', 'status': 'completed',
        'name': 'lookup_wikipedia', 'call_id': 'call_1',
        'arguments': json.dumps({'query': 'alpha', 'language': 'en', 'limit': 1})}]
    fake = Fake([selection, response('The answer is in [S1].')])

    class References:
        def fetch(self, provider, query, language, community, *, limit):
            return {'provider': provider, 'query': query, 'sources': [
                {'title': 'Alpha', 'excerpt': 'External reference',
                 'url': 'https://en.wikipedia.org/wiki/Alpha'}], 'metadata': {}}

    agent = RagChatAgent(load_config(), fake, RagSQLiteStore(tmp_path / 'chat.sqlite3'),
        index_data_dir=tmp_path, retrieval_client=References())
    result = agent.run('Where is alpha?', use_rag=True)
    assert result.status == 'ok', result
    assert local_index.calls == [['Where is alpha?']]
    assert len(fake.calls) == 2
    assert all('Unique alpha evidence' in json.dumps(call['input'], ensure_ascii=False)
               for call in fake.calls)
    state = agent.state()
    assert len([r for r in state['requests'] if r['metadata']['kind'] == 'query_embedding']) == 1
    assert state['summary']['api_requests'] == 3
    assert state['summary']['cost_complete']
    agent.close()


def test_opted_in_mcp_continuation_keeps_tools_and_low_reasoning(tmp_path, local_index):
    selection = response('')
    selection['output'] = [{'type': 'function_call', 'status': 'completed',
        'name': 'lookup_wikipedia', 'call_id': 'call_1',
        'arguments': json.dumps({'query': 'alpha', 'language': 'en', 'limit': 1})}]
    fake = Fake([selection, response('The answer is in [S1].')])

    class References:
        def fetch(self, provider, query, language, community, *, limit):
            return {'provider': provider, 'query': query, 'sources': [
                {'title': 'Alpha', 'excerpt': 'External reference',
                 'url': 'https://en.wikipedia.org/wiki/Alpha'}], 'metadata': {}}

    agent = RagChatAgent(load_config(), fake, RagSQLiteStore(tmp_path / 'chat.sqlite3'),
        index_data_dir=tmp_path, retrieval_client=References(), final_reasoning_effort='low')

    result = agent.run('Where is alpha?', rag_mode='filter')

    assert result.status == 'ok', result
    filter_payload = next(call for call in fake.calls
        if call.get('text', {}).get('format', {}).get('name') == 'filter')
    generation = [call for call in fake.calls
        if call.get('text', {}).get('format', {}).get('name') == 'grounded_answer']
    assert filter_payload['reasoning']['effort'] == 'none'
    assert len(generation) == 2
    assert all(call['reasoning']['effort'] == 'low' and call['tool_choice'] == 'auto'
        for call in generation)
    assert all(any(tool['name'] == 'lookup_wikipedia' for tool in call['tools'])
        for call in generation)
    assert all('Unique alpha evidence' in json.dumps(call['input'], ensure_ascii=False)
        for call in generation)
    requests = agent.state()['requests']
    assert any(item['metadata']['kind'] == 'mcp_step' for item in requests)
    parent = next(item for item in requests if item['metadata']['kind'] == 'answer')
    assert parent['metadata']['requested_reasoning_effort'] == 'low'
    assert parent['metadata']['rag']['requested_reasoning_effort'] == 'low'
    agent.close()


def test_evaluation_workspace_keeps_legacy_none_reasoning(tmp_path, local_index):
    from rag.chat_evaluation import _CapturedTransport, _EvaluationWorkspace
    fake = Fake([response('Alpha is documented [S1].'), response('{"operations": []}')])
    captured = []
    transport = _CapturedTransport(fake, lambda kind, payload: captured.append((kind, payload)))
    workspace = _EvaluationWorkspace(tmp_path, transport, tmp_path, profile_id=1)

    result = workspace.agent().run('Where is alpha?', rag_mode='filter')

    assert result.status == 'ok', result
    assert captured
    assert all(payload['reasoning']['effort'] == 'none' for _, payload in captured)
    assert all(payload['tool_choice'] == 'none' for kind, payload in captured
        if kind == 'answer')
    workspace.close()


def test_rejected_input_and_paused_task_skip_rag(tmp_path, local_index):
    fake = Fake()
    app = create_app(data_dir=tmp_path, transport=fake)
    client = app.test_client()
    assert client.post('/api/ask', json={'prompt': ' ', 'use_rag': True}).json['code'] == 'input_invalid'
    assert client.post('/api/task', json={'name': 'Formal pause guard'}).json['state']['dialogue_kind'] == 'formal'
    current = client.get('/api/state').json
    assert client.post('/api/task-state/action', json={
        'task_id': current['workspace']['active_dialogue']['task_id'],
        'profile_id': current['personalization']['selected_id'], 'action': 'pause'}).status_code == 200
    assert client.post('/api/ask', json={'prompt': 'Find alpha', 'use_rag': True}).json['code'] == 'task_paused'
    assert local_index.calls == [] and fake.calls == []
    assert all(r['metadata']['rag']['status'] == 'not_requested'
               for r in client.get('/api/state').json['requests']
               if r['metadata']['kind'] == 'answer')


def test_input_invariant_conflict_skips_embedding_and_generation(tmp_path, local_index):
    fake = Fake([response(json.dumps({'checks': [{'id': 'R1', 'violated': True}]}))])
    workspace = RagProfileWorkspace(tmp_path, fake)
    workspace.memory.set_invariants(1, 0, ['Only grayscale.'])
    result = workspace.agent().run('Use red paint', use_rag=True)
    assert result.code == 'invariant_input_conflict'
    assert local_index.calls == [] and len(fake.calls) == 1
    parent = next(r for r in workspace.state()['requests'] if r['metadata']['kind'] == 'answer')
    assert parent['usage_status'] == 'not_requested'
    workspace.close()


def test_interrupted_embedding_recovers_unknown_child_without_fictitious_generation(tmp_path, local_index):
    workspace = RagProfileWorkspace(tmp_path, Fake())

    def interrupt(_texts):
        raise KeyboardInterrupt()

    local_index.embed = interrupt
    agent = workspace.agent()
    agent._rag_embedder = local_index
    with pytest.raises(KeyboardInterrupt):
        agent.run('Find alpha', use_rag=True)
    workspace.close()
    reopened = RagProfileWorkspace(tmp_path, Fake())
    state = reopened.state()
    parent = next(r for r in state['requests'] if r['metadata']['kind'] == 'answer')
    child = next(r for r in state['requests'] if r['metadata']['kind'] == 'query_embedding')
    assert parent['status'] == child['status'] == 'interrupted'
    assert parent['usage_status'] == 'not_requested'
    assert child['usage_status'] == 'unavailable'
    assert state['summary']['api_requests'] == state['summary']['unknown_cost_requests'] == 1
    reopened.close()


def test_actual_rag_context_is_counted_before_generation(tmp_path, local_index):
    fake = Fake()
    workspace = RagProfileWorkspace(tmp_path, fake)
    agent = workspace.agent()
    agent._config = replace(agent._config, max_input_tokens=1)
    result = agent.run('Find alpha', use_rag=True)
    assert result.code == 'context_budget_exceeded'
    assert local_index.calls == [['Find alpha']] and fake.calls == []
    state = workspace.state()
    parent = next(r for r in state['requests'] if r['metadata']['kind'] == 'answer')
    assert parent['usage_status'] == 'not_requested'
    assert parent['metadata']['context']['full_input_tokens_estimate'] > 1
    assert state['summary']['api_requests'] == 1
    workspace.close()


def test_preview_rag_is_offline_and_discloses_missing_context(tmp_path, local_index):
    fake = Fake()
    client = create_app(data_dir=tmp_path, transport=fake).test_client()
    result = client.post('/api/preview', json={'prompt': 'Find alpha', 'use_rag': True})
    assert result.status_code == 200
    assert result.json['token_metrics']['rag_context_included'] is False
    assert 'RAG' in result.json['token_metrics']['estimate_boundary']
    assert local_index.calls == [] and fake.calls == []


@pytest.mark.parametrize('flag', ['true', 1, None, [], {}])
def test_non_boolean_rag_flag_rejected_at_route(tmp_path, flag):
    fake = Fake()
    app = create_app(data_dir=tmp_path, transport=fake)
    result = app.test_client().post('/api/ask', json={'prompt': 'Hello', 'use_rag': flag})
    assert result.status_code == 400
    assert fake.calls == []

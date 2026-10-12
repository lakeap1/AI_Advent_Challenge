"""Day 25 conversation behavior at the state, SQLite, and HTTP boundaries."""
import json

import pytest

from app import create_app
from agent.conversation_state import apply_preparation, empty_state
from rag.chat import RagChatAgent
from rag.chat_store import RagSQLiteStore
from agent import load_config


def preparation(message, state, *, query=None, operations=None):
    return {'revision': state['revision'], 'search_query': query or message,
            'operations': operations or []}


def op(field, key, value, evidence, action='set'):
    return dict(action=action, field=field, key=key, value=value, evidence=evidence)


def test_explicit_goal_and_correction_replace_not_accumulate():
    state = empty_state()
    assert state['goal'] == ''
    message = 'Хочу сделать normal map без изменения геометрии.'
    state = apply_preparation(state, preparation(message, state, operations=[
        op('goal', '', 'Настроить normal map', 'Хочу сделать normal map'),
        op('constraints', 'geometry', 'Не менять геометрию', 'без изменения геометрии')]),
        message, request_id=10)
    assert state['goal'] == 'Настроить normal map'
    assert state['constraints'][0]['value'] == 'Не менять геометрию'
    assert state['constraints'][0]['source_request_id'] == 10
    corrected = 'Теперь можно менять геометрию, но без внешних плагинов.'
    state = apply_preparation(state, preparation(corrected, state, operations=[
        op('constraints', 'geometry', 'Можно менять геометрию', 'можно менять геометрию'),
        op('constraints', 'plugins', 'Не использовать внешние плагины', 'без внешних плагинов')]),
        corrected, request_id=11)
    assert {x['key']: x['value'] for x in state['constraints']} == {
        'geometry': 'Можно менять геометрию', 'plugins': 'Не использовать внешние плагины'}
    removed = 'Запрет на внешние плагины снимаю.'
    state = apply_preparation(state, preparation(removed, state, operations=[
        op('constraints', 'plugins', '', 'Запрет на внешние плагины снимаю', 'remove')]),
        removed, request_id=12)
    assert [x['key'] for x in state['constraints']] == ['geometry']


@pytest.mark.parametrize('bad', [
    {'revision': 0, 'search_query': 'x', 'operations': [], 'extra': True},
    {'revision': 1, 'search_query': 'x', 'operations': []},
    {'revision': 0, 'search_query': 'x', 'operations': [op('goal', '', 'fake', 'not user')]},
    {'revision': 0, 'search_query': 'x', 'operations': [op('constraints', 'x', 'y', 'hello', 'unknown')]},
])
def test_invalid_preparation_keeps_state(bad):
    state = empty_state()
    with pytest.raises(ValueError):
        apply_preparation(state, bad, 'hello', request_id=1)
    assert state == empty_state()


def test_checkpoint_fork_inherits_state_before_correction(tmp_path):
    store = RagSQLiteStore(tmp_path / 'chat.sqlite3')
    store.configure_context('branching')
    store.set_dialogue_kind('ordinary')
    first = apply_preparation(empty_state(), preparation('Два источника света', empty_state(), operations=[
        op('constraints', 'lights', 'Два источника света', 'Два источника света')]),
        'Два источника света', request_id=1)
    store.save_conversation_state(first, expected_revision=0, request_id=1)
    checkpoint = store.checkpoint('До коррекции')
    second = apply_preparation(first, preparation('Теперь три источника света', first, operations=[
        op('constraints', 'lights', 'Три источника света', 'три источника света')]),
        'Теперь три источника света', request_id=2)
    store.save_conversation_state(second, expected_revision=1, request_id=2)
    fork = store.branch(checkpoint['id'], 'Другая версия')
    assert store.conversation_state()['constraints'][0]['value'] == 'Два источника света'
    store.switch(1)
    assert store.conversation_state()['constraints'][0]['value'] == 'Три источника света'
    store.switch(fork['id'])
    store.close()
    restored = RagSQLiteStore(tmp_path / 'chat.sqlite3')
    assert restored.conversation_state()['constraints'][0]['value'] == 'Два источника света'
    restored.close()


class Embedder:
    def __init__(self): self.calls = []
    def embed(self, texts):
        self.calls.append(texts)
        return {'model': 'text-embedding-3-small', 'vectors': [[1.0] + [0.0] * 1535],
                'usage': {'prompt_tokens': 5, 'total_tokens': 5}}


def response(text):
    return {'status': 'completed', 'model': 'gpt-6-luna', 'service_tier': 'default',
            'output': [{'type': 'message', 'role': 'assistant', 'status': 'completed',
                        'content': [{'type': 'output_text', 'text': text}]}],
            'usage': {'input_tokens': 100, 'output_tokens': 10, 'total_tokens': 110,
                      'input_tokens_details': {'cached_tokens': 0, 'cache_write_tokens': 0},
                      'output_tokens_details': {'reasoning_tokens': 0}}}


class Transport:
    def __init__(self, *, invalid=False, rejected_answer=False, scores=3):
        self.calls = []
        self.invalid = invalid
        self.rejected_answer = rejected_answer
        self.scores = scores

    def create(self, payload, timeout):
        self.calls.append(payload)
        name = payload.get('text', {}).get('format', {}).get('name')
        if name == 'conversation_preparation':
            data = json.loads(payload['input'][0]['content'])
            message = data['current_message']
            if self.invalid:
                return response(json.dumps({'revision': data['state']['revision'],
                    'operations': [{'action': 'set', 'field': 'goal', 'key': '',
                        'desired_outcome_evidence': 'not user', 'conditions': []}],
                    'question_parts': [{'evidence': message[:1000]}]}))
            operations = [{'action': 'set', 'field': 'goal', 'key': '',
                'desired_outcome_evidence': 'normal map',
                'conditions': []}] if 'цель: normal map' in message else []
            if 'без изменения геометрии' in message:
                operations.append(op('constraints', 'geometry', 'Не менять геометрию', 'без изменения геометрии'))
            if 'Теперь можно менять геометрию' in message:
                operations.append(op('constraints', 'geometry', 'Можно менять геометрию',
                                     'Теперь можно менять геометрию'))
            condition = (' теперь можно менять геометрию' if 'Теперь можно менять геометрию' in message
                         else ' без изменения геометрии')
            proposal = preparation(message, data['state'],
                query=(data['state']['goal'] or 'Normal map') + condition + ': ' + message,
                operations=operations)
            proposal.pop('search_query')  # Legacy state helper is not the V3 provider wire.
            proposal['question_parts'] = [{'evidence': message[:1000]}]
            return response(json.dumps(proposal, ensure_ascii=False))
        if name == 'filter':
            candidates = json.loads(payload['input'][0]['content'])['candidates']
            return response(json.dumps({'scores': {item['chunk_id']: {
                'score': self.scores, 'reason': 'Evidence'} for item in candidates}}))
        if name in ('grounded_answer', 'ordinary_parts_answer'):
            label = 'S9' if self.rejected_answer else 'S1'
            body = {'status': 'answered', 'claims': [
                {'text': 'Normal map uses Non-Color.', 'source_labels': [label]}],
                'clarification': ''}
            if name == 'ordinary_parts_answer':
                body = {'parts': {'q1': body}}
            return response(json.dumps(body))
        raise AssertionError(f'unexpected stage: {name}')


@pytest.fixture
def local_index(monkeypatch):
    from rag import chat
    chunk = {'chunk_id': 'fixture-1', 'file': 'fixture.md', 'source': 'local',
             'title': 'Normal Map', 'section': ['Color Space'], 'line_start': 1,
             'line_end': 1, 'start': 0, 'end': 50, 'document_hash': 'fixture',
             'text': 'Set a normal map texture to Non-Color.',
             'embedding': [1.0] + [0.0] * 1535}
    monkeypatch.setattr(chat, 'read_index', lambda *_: [chunk])
    embedder = Embedder()
    monkeypatch.setattr(chat, 'OpenAIEmbedder', lambda _config: embedder)
    return embedder


def test_preparation_requests_high_reasoning_once_with_recorded_usage(tmp_path, local_index, monkeypatch):
    class LocalEncoding:
        def encode(self, text, *, disallowed_special):
            return list(text.encode('utf-8'))

    monkeypatch.setattr('agent.tokens.tiktoken.get_encoding', lambda _: LocalEncoding())
    class ReasoningTransport(Transport):
        def create(self, payload, timeout):
            result = super().create(payload, timeout)
            if payload.get('text', {}).get('format', {}).get('name') == 'conversation_preparation':
                result['usage']['output_tokens_details']['reasoning_tokens'] = 4
            return result

    transport = ReasoningTransport()
    app = create_app(data_dir=tmp_path, transport=transport)
    result = app.test_client().post('/api/ask', json={
        'prompt': 'Моя цель: normal map без изменения геометрии.'})
    assert result.status_code == 200, result.json

    preparations = [payload for payload in transport.calls
                    if payload.get('text', {}).get('format', {}).get('name')
                    == 'conversation_preparation']
    assert len(preparations) == 1
    assert preparations[0]['model'] == 'gpt-6-luna'
    assert preparations[0]['reasoning']['effort'] == 'high'
    assert preparations[0]['max_output_tokens'] == 4000
    assert len(transport.calls) == 3
    recorded = [row for row in result.json['state']['requests']
                if row['metadata']['kind'] == 'conversation_preparation']
    assert len(recorded) == 1
    assert recorded[0]['metadata']['requested_reasoning_effort'] == 'high'
    assert recorded[0]['metadata']['requested_max_output_tokens'] == 4000
    assert recorded[0]['usage']['total_tokens'] == 110
    assert recorded[0]['usage']['reasoning_tokens'] == 4
    assert recorded[0]['cost_usd'] == '0.000015'
    assert result.json['state']['summary']['api_requests'] == 4
    app.extensions['workspace'].close()


def test_main_chat_preparation_query_and_restart(tmp_path, local_index):
    transport = Transport()
    app = create_app(data_dir=tmp_path, transport=transport)
    client = app.test_client()
    result = client.post('/api/ask', json={'prompt': 'Моя цель: normal map без изменения геометрии.'})
    assert result.status_code == 200, result.json
    state = result.json['state']
    assert state['conversation_state']['goal'] == 'normal map'
    parent = next(r for r in state['requests'] if r['metadata']['kind'] == 'answer')
    prep = next(r for r in state['requests'] if r['metadata']['kind'] == 'conversation_preparation')
    assert prep['cost_usd'] is not None
    assert parent['metadata']['rag']['original_query'] == 'Моя цель: normal map без изменения геометрии.'
    query = parent['metadata']['rag']['search_query']
    assert query.startswith('Моя цель: normal map без изменения геометрии.')
    assert 'normal map' in query and 'Не менять геометрию' in query
    assert 'proposed_search_query' not in parent['metadata']['rag']
    assert set(prep['metadata']['raw_proposal']) == {'revision', 'operations', 'question_parts'}
    assert local_index.calls == [[query]]
    filter_input = json.loads(next(p for p in transport.calls if p.get('text', {}).get('format', {}).get('name') == 'filter')['input'][0]['content'])
    assert filter_input['original_question'] == 'Моя цель: normal map без изменения геометрии.'
    assert filter_input['original_message'] == filter_input['original_question']
    assert filter_input['conversation_context']['state']['goal'] == 'normal map'
    assert state['retrievals'][0]['query'] == query
    assert parent['metadata']['conversation']['state_after']['revision'] == 1
    assert parent['metadata']['rag']['used_sources'][0]['chunk_id'] == 'fixture-1'
    app.extensions['workspace'].close()
    reopened = create_app(data_dir=tmp_path, transport=Transport()).test_client().get('/api/state').json
    assert reopened['conversation_state'] == state['conversation_state']
    assert reopened['messages'] == state['messages']
    assert reopened['requests'] == state['requests']


@pytest.mark.parametrize('use_working,use_long_term,expected', [
    (True, True, {'Рабочий маркер', 'Долгий маркер'}),
    (False, True, {'Долгий маркер'}),
    (True, False, {'Рабочий маркер'}),
    (False, False, set()),
])
def test_ordinary_trace_resolves_only_memory_sent_to_model(
        tmp_path, local_index, use_working, use_long_term, expected):
    transport = Transport()
    app = create_app(data_dir=tmp_path, transport=transport)
    client = app.test_client()
    for layer, key in [('working', 'Рабочий маркер'), ('long_term', 'Долгий маркер')]:
        saved = client.post('/api/memory', json={
            'layer': layer, 'category': 'context' if layer == 'working' else 'knowledge',
            'scope': 'task' if layer == 'working' else 'user',
            'key': key, 'value': 'Значение ' + key, 'reason': 'Тест provenance'})
        assert saved.status_code == 200, saved.json
    asked = client.post('/api/ask', json={
        'prompt': 'Как настроить normal map?',
        'use_working': use_working, 'use_long_term': use_long_term})
    assert asked.status_code == 200, asked.json
    assert asked.json['status'] == 'ok'
    calls_before_trace = len(transport.calls)
    trace = client.get('/api/trace/' + str(asked.json['request_id']))
    assert trace.status_code == 200, trace.json
    assert len(transport.calls) == calls_before_trace
    after_trace = client.get('/api/state').json
    assert after_trace['requests'] == asked.json['state']['requests']
    assert after_trace['messages'] == asked.json['state']['messages']
    assert after_trace['summary'] == asked.json['state']['summary']
    assert {row['key'] for row in trace.json['memory']} == expected
    assert len(trace.json['context']['memory_refs']) == len(expected)
    assert trace.json['selection'] == {
        'working': use_working, 'long_term': use_long_term}
    generation = next(call for call in transport.calls
                      if call.get('text', {}).get('format', {}).get('name') == 'ordinary_parts_answer')
    sent = json.dumps(generation['input'], ensure_ascii=False)
    for key in ('Рабочий маркер', 'Долгий маркер'):
        assert (key in sent) == (key in expected)
    app.extensions['workspace'].close()


def test_invalid_preparation_stops_before_embedding(tmp_path, local_index):
    app = create_app(data_dir=tmp_path, transport=Transport(invalid=True))
    result = app.test_client().post('/api/ask', json={'prompt': 'Моя цель: normal map.'})
    assert result.status_code == 400
    assert result.json['state']['conversation_state']['goal'] == ''
    assert local_index.calls == []
    assert [r['metadata']['kind'] for r in result.json['state']['requests']].count('conversation_preparation') == 1
    app.extensions['workspace'].close()


@pytest.mark.parametrize('scores,rejected_answer,expected', [(0, False, 'no_context'), (3, True, 'rejected')])
def test_accepted_state_survives_later_failure(tmp_path, local_index, scores, rejected_answer, expected):
    app = create_app(data_dir=tmp_path, transport=Transport(scores=scores, rejected_answer=rejected_answer))
    result = app.test_client().post('/api/ask', json={'prompt': 'Моя цель: normal map без изменения геометрии.'})
    assert result.json['status'] == expected
    state = result.json['state']
    assert state['conversation_state']['goal'] == 'normal map'
    assert state['summary']['api_requests'] >= 2
    assert state['summary']['known_cost_usd'] != '0'
    if expected == 'rejected':
        answer = next(r for r in state['requests'] if r['metadata']['kind'] == 'answer')
        assert answer['usage']['total_tokens'] == 110
        assert not [m for m in state['messages'] if m['role'] == 'assistant']
    app.extensions['workspace'].close()


def test_ordinary_dialogue_continues_with_formal_task_done_or_paused(tmp_path, local_index):
    transport = Transport()
    app = create_app(data_dir=tmp_path, transport=transport)
    client = app.test_client()
    formal = client.post('/api/task', json={'name': 'Формальная проверка'}).json['state']
    assert formal['dialogue_kind'] == 'formal'
    task_id = formal['workspace']['active_dialogue']['task_id']
    # Setup only: formal state cannot legitimately reach done without its full workflow.
    with app.extensions['workspace'].memory.transaction() as db:
        db.execute("UPDATE task_states SET stage='done' WHERE task_id=?", (task_id,))
    blocked = client.post('/api/ask', json={'prompt': 'Как настроить normal map?'})
    assert blocked.json['code'] == 'task_done'
    assert transport.calls == []
    ordinary = client.post('/api/dialogue', json={
        'name': 'Обычный разговор', 'task_id': task_id, 'dialogue_kind': 'ordinary'}).json['state']
    assert ordinary['dialogue_kind'] == 'ordinary'
    answered = client.post('/api/ask', json={'prompt': 'Моя цель: normal map без изменения геометрии.'})
    assert answered.json['status'] == 'ok'
    assert answered.json['state']['conversation_state']['goal'] == 'normal map'
    app.extensions['workspace'].close()


def test_paused_formal_task_allows_ordinary_question_but_forbids_composition(tmp_path, local_index, monkeypatch):
    transport = Transport()
    app = create_app(data_dir=tmp_path, transport=transport)
    client = app.test_client()
    state = client.get('/api/state').json
    profile_id = state['personalization']['selected_id']
    task_id = state['task_state']['task_id']
    paused = client.post('/api/task-state/action', json={
        'profile_id': profile_id, 'task_id': task_id, 'action': 'pause'})
    assert paused.status_code == 200
    asked = client.post('/api/ask', json={'prompt': 'Как настроить normal map?'})
    assert asked.status_code == 200, asked.json
    assert asked.json['status'] == 'ok'
    assert any(p.get('text', {}).get('format', {}).get('name') == 'conversation_preparation'
               for p in transport.calls)
    state = asked.json['state']
    class NoopThread:
        def __init__(self, *args, **kwargs): pass
        def start(self): pass
    monkeypatch.setattr('composition_routes.Thread', NoopThread)
    composed = client.post('/api/composition', json={
        'question': 'Проверь normal map', 'query': 'normal map',
        'profile_id': profile_id,
        'dialogue_id': state['workspace']['active_dialogue']['id'],
        'task_id': task_id})
    assert composed.status_code == 400
    assert client.get('/api/composition').json['run'] is None
    app.extensions['workspace'].close()


def test_ordinary_preview_discloses_forced_retrieval_even_with_plain_flag(tmp_path, local_index):
    transport = Transport()
    app = create_app(data_dir=tmp_path, transport=transport)
    client = app.test_client()
    preview = client.post('/api/preview', json={'prompt': 'Как настроить normal map?', 'rag_mode': 'plain'})
    assert preview.status_code == 200, preview.json
    assert 'RAG' in preview.json['token_metrics']['estimate_boundary']
    assert preview.json['token_metrics']['rag_context_included'] is False
    assert transport.calls == [] and local_index.calls == []
    app.extensions['workspace'].close()


def test_explicit_task_keeps_formal_generation_contract(tmp_path, local_index):
    from tests_rag.grounded_fake import GroundedTransport
    transport = GroundedTransport('Подтверждено.')
    app = create_app(data_dir=tmp_path, transport=transport)
    client = app.test_client()
    created = client.post('/api/task', json={'name': 'Формальная задача'}).json['state']
    assert created['dialogue_kind'] == 'formal'
    answered = client.post('/api/ask', json={'prompt': 'Что подтверждает источник?'})
    assert answered.json['status'] == 'ok', answered.json
    names = [payload.get('text', {}).get('format', {}).get('name') for payload in transport.calls]
    assert 'task_response' in names
    assert 'conversation_preparation' not in names
    app.extensions['workspace'].close()


def test_late_followup_uses_goal_after_six_message_window(tmp_path, local_index):
    transport = Transport(scores=0)
    app = create_app(data_dir=tmp_path, transport=transport)
    client = app.test_client()
    first = client.post('/api/ask', json={'prompt': 'Моя цель: normal map без изменения геометрии.'})
    assert first.json['status'] == 'no_context'
    for n in range(6):
        result = client.post('/api/ask', json={'prompt': f'Что означает параметр {n}?'})
        assert result.json['status'] == 'no_context'
    last = client.post('/api/ask', json={'prompt': 'А какой размер ему задать?'})
    assert last.json['status'] == 'no_context'
    preps = [payload for payload in transport.calls
             if payload.get('text', {}).get('format', {}).get('name') == 'conversation_preparation']
    latest = json.loads(preps[-1]['input'][0]['content'])
    assert len(latest['recent_history']) == 6
    assert all('Моя цель:' not in row['content'] for row in latest['recent_history'])
    assert latest['state']['goal'] == 'normal map'
    assert 'normal map' in local_index.calls[-1][0]
    app.extensions['workspace'].close()


def test_separate_dialogue_has_separate_state(tmp_path, local_index):
    app = create_app(data_dir=tmp_path, transport=Transport())
    client = app.test_client()
    first = client.post('/api/ask', json={'prompt': 'Моя цель: normal map без изменения геометрии.'}).json
    first_dialogue = first['state']['workspace']['active_dialogue']['id']
    task_id = first['state']['workspace']['active_dialogue']['task_id']
    invalid = client.post('/api/dialogue', json={'name': 'Не создавать', 'task_id': task_id,
                                                  'dialogue_kind': 'unknown'})
    assert invalid.status_code == 400
    assert client.get('/api/state').json['workspace']['active_dialogue']['id'] == first_dialogue
    other = client.post('/api/dialogue', json={'name': 'Другой разговор', 'task_id': task_id}).json['state']
    assert other['dialogue_kind'] == 'ordinary'
    assert other['conversation_state']['goal'] == ''
    assert client.post('/api/open', json={'dialogue_id': first_dialogue}).json['state']['conversation_state']['goal'] == 'normal map'
    app.extensions['workspace'].close()


def test_http_fork_at_historical_message_ignores_later_correction(tmp_path, local_index):
    app = create_app(data_dir=tmp_path, transport=Transport())
    client = app.test_client()
    initial = client.get('/api/state').json
    task_id = initial['workspace']['active_dialogue']['task_id']
    created = client.post('/api/dialogue', json={
        'name': 'Ветки', 'task_id': task_id, 'mode': 'branching'}).json['state']
    assert created['dialogue_kind'] == 'ordinary'
    first = client.post('/api/ask', json={'prompt': 'Моя цель: normal map без изменения геометрии.'}).json
    first_message = first['state']['messages'][-1]['id']
    corrected = client.post('/api/ask', json={'prompt': 'Теперь можно менять геометрию.'}).json
    assert corrected['state']['conversation_state']['constraints'][0]['value'] == 'Можно менять геометрию'
    fork = client.post('/api/checkpoint', json={'name': 'До коррекции', 'message_id': first_message})
    assert fork.status_code == 200
    checkpoint_id = fork.json['state']['checkpoints'][-1]['id']
    branched = client.post('/api/branch', json={'checkpoint_id': checkpoint_id, 'name': 'Ранняя'}).json['state']
    assert branched['conversation_state']['constraints'][0]['value'] == 'Не менять геометрию'
    assert len(branched['messages']) == 2
    back = client.post('/api/switch', json={'branch_id': 1}).json['state']
    assert back['conversation_state']['constraints'][0]['value'] == 'Можно менять геометрию'
    app.extensions['workspace'].close()


def test_corrected_active_context_excludes_obsolete_provenance_and_assistant_proposals(tmp_path, local_index):
    transport = Transport()
    app = create_app(data_dir=tmp_path, transport=transport)
    client = app.test_client()
    try:
        first_prompt = 'Моя цель: normal map без изменения геометрии.'
        first = client.post('/api/ask', json={'prompt': first_prompt}).json
        old_state = first['state']['conversation_state']
        corrected_prompt = 'Теперь можно менять геометрию.'
        result = client.post('/api/ask', json={'prompt': corrected_prompt}).json
        assert result['status'] == 'ok'
        parent = next(row for row in result['state']['requests'] if row['id'] == result['request_id'])
        projection = parent['metadata']['rag']['conversation_context']
        assert projection['state']['constraints'] == [{'key': 'geometry', 'value': 'Можно менять геометрию'}]
        assert 'Не менять геометрию' not in str(projection['state'])
        assert 'provenance' not in projection['state']
        assert projection['recent_user_messages'] == [{'id': first['state']['messages'][0]['id'],
                                                      'content': first_prompt}]
        query = parent['metadata']['rag']['search_query']
        assert query.startswith(corrected_prompt) and 'Не менять геометрию' not in query
        assert 'Normal map uses Non-Color.' not in query
        assert local_index.calls[-1] == [query]
        filter_data = json.loads(next(call for call in reversed(transport.calls)
            if call['text']['format']['name'] == 'filter')['input'][0]['content'])
        assert filter_data['original_question'] == corrected_prompt
        assert filter_data['conversation_context'] == projection
        final = transport.calls[-1]
        trusted = json.loads(next(item['content'].split('\n', 1)[1] for item in final['input']
            if item['content'].startswith('Исходный пользовательский контекст')))
        assert trusted == {'original_message': corrected_prompt, 'conversation_context': projection}
        assert 'история assistant' in final['instructions'].casefold()
        assert 'попроси уточнить' in final['instructions'].casefold()
        assert result['state']['conversation_state']['provenance'][:len(old_state['provenance'])] == old_state['provenance']
    finally:
        app.extensions['workspace'].close()


@pytest.mark.parametrize('full_budget', [False, True], ids=['whitespace', '4000-chars'])
def test_literal_whitespace_prompt_and_part_evidence_are_preserved(tmp_path, local_index, full_budget):
    from tests_rag.grounded_fake import GroundedTransport

    class LiteralEvidence(GroundedTransport):
        def create(self, payload, timeout):
            result = super().create(payload, timeout)
            if payload['text']['format']['name'] == 'conversation_preparation':
                proposal = json.loads(result['output'][0]['content'][0]['text'])
                proposal['question_parts'] = [{'evidence': ' normal map '}]
                return response(json.dumps(proposal))
            return result

    prompt = '  Explain normal map please.\n '
    if full_budget:
        prompt += 'a' * (4000 - len(prompt))
    fake = LiteralEvidence()
    app = create_app(data_dir=tmp_path, transport=fake)
    try:
        result = app.test_client().post('/api/ask', json={'prompt': prompt}).json
        assert result['status'] == 'ok'
        assert local_index.calls == [[prompt]]
        preparation_data = json.loads(fake.calls[0]['input'][0]['content'])
        assert preparation_data['current_message'] == prompt
        data = json.loads(fake.calls[1]['input'][0]['content'])
        assert data['original_question'] == ' normal map ' and data['original_message'] == prompt
        parent = next(row for row in result['state']['requests'] if row['id'] == result['request_id'])
        assert parent['metadata']['rag']['part_coverage']['q1']['question'] == ' normal map '
        assert parent['metadata']['rag']['question_parts'] == [
            {'id': 'q1', 'question': ' normal map ', 'evidence': ' normal map '}]
        assert 'proposed_search_query' not in parent['metadata']['rag']
        assert 'Invented owner' not in json.dumps(fake.calls[-1]['input'])
        assert result['state']['messages'][0]['content'] == prompt
    finally:
        app.extensions['workspace'].close()


def test_embedding_omissions_are_durable_and_do_not_omit_filter_or_final_state(tmp_path, local_index):
    from tests_rag.grounded_fake import GroundedTransport

    class BoundedProposal(GroundedTransport):
        def create(self, payload, timeout):
            result = super().create(payload, timeout)
            if payload['text']['format']['name'] == 'conversation_preparation':
                proposal = json.loads(result['output'][0]['content'][0]['text'])
                return response(json.dumps(proposal))
            return result

    app = create_app(data_dir=tmp_path, transport=BoundedProposal())
    try:
        agent = app.extensions['workspace'].agent()
        state = empty_state()
        state['constraints'] = [{'key': f'condition-{i}', 'value': str(i) * 900,
            'evidence': 'User supplied a condition.', 'source_request_id': 1} for i in range(8)]
        agent._store.save_conversation_state(state, expected_revision=0, request_id=1)
        result = agent.run('Explain normal map. ' + 'a' * 3980)
        assert result.status == 'ok', result
        parent = next(row for row in agent.state()['requests'] if row['id'] == result.request_id)
        rag = parent['metadata']['rag']
        policy = rag['effective_query_policy']
        assert policy['omitted_entry_count'] > 0
        assert len(policy['omitted_entry_refs']) == policy['omitted_entry_count']
        assert policy == parent['metadata']['conversation']['effective_query_policy']
        assert local_index.calls == [[rag['search_query']]]
        for ref in policy['omitted_entry_refs']:
            key = ref.split('.')[-1]
            assert '\n' + key + '\n' not in rag['search_query']
        filter_call = next(call for call in agent._transport.calls if call['text']['format']['name'] == 'filter')
        filtered = json.loads(filter_call['input'][0]['content'])['conversation_context']
        assert len(filtered['state']['constraints']) == 8
        final = agent._transport.calls[-1]
        trusted = json.loads(next(item['content'].split('\n', 1)[1] for item in final['input']
            if item['content'].startswith('Исходный пользовательский контекст')))
        assert trusted['conversation_context'] == filtered == rag['conversation_context']
        rows = agent.state()['requests']
    finally:
        app.extensions['workspace'].close()
    reopened = create_app(data_dir=tmp_path, transport=BoundedProposal())
    try:
        assert reopened.test_client().get('/api/state').json['requests'] == rows
    finally:
        reopened.extensions['workspace'].close()

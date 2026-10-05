"""Unified chat contracts; model/search controlled, real HTTP adapter and SQLite."""
import asyncio
import json
import pytest

from app import create_app
from composition.service import CompositionService, CompositionError
from test_composition import Sources, Model
from test_strategies import Fake, response
from tests_rag.grounded_fake import GroundedTransport, install_local_index


class ImmediateThread:
    def __init__(self, target, args=(), **kwargs):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


def setup_chat(tmp_path, monkeypatch, refusal=False, pending=False, grounded=False):
    if grounded:
        install_local_index(monkeypatch)
    fake = (GroundedTransport('Деталь памятки.') if grounded else
            Fake([response('Деталь памятки.'), response('{"operations": []}')]))
    app = create_app(data_dir=tmp_path, transport=fake)
    store = app.extensions['composition_store']
    model = Model(refusal)

    def execute(rid):
        if pending:
            return
        run = store.get(rid)
        service = CompositionService(store, providers=Sources(), transport=model)
        materials = asyncio.run(service.search(rid, run['question'], run['query']))
        try:
            summary = service.summarize(materials)
            service.save(summary)
            store.event(rid, 'save', 'success')
        except CompositionError:
            store.event(rid, 'summarize', 'error', 'Output policy отказала.')

    monkeypatch.setattr(app.extensions['composition_agent'], 'execute', execute)
    monkeypatch.setattr('composition_routes.Thread', ImmediateThread)
    client = app.test_client()
    return app, client, fake, model


def body(client, **changes):
    state = client.get('/api/state').json
    return dict(question='Как проверить normal map?', query='normal map seams',
                profile_id=state['personalization']['selected_id'],
                dialogue_id=state['workspace']['active_dialogue']['id'],
                task_id=state['task_state']['task_id'], **changes)


@pytest.mark.parametrize('refusal', [False, True])
def test_completed_exchange_in_chat_once_and_no_double_cost(tmp_path, monkeypatch, refusal):
    app, client, fake, model = setup_chat(tmp_path, monkeypatch, refusal)
    result = client.post('/api/composition', json=body(client))
    assert result.status_code == 202
    rid = result.json['id']
    state = client.get('/api/state').json
    assert len(state['messages']) == (1 if refusal else 2)
    assert state['summary']['api_requests'] == 1
    assert state['summary']['known_cost_usd'] == '0.00002'
    assert state['token_accounting']['known_total_tokens'] == 120
    assert len(state['requests']) == 1
    assert state['requests'][0]['metadata']['composition_run_id'] == rid
    assert len(state['retrievals']) == 1
    assert state['retrievals'][0]['sources'][0]['url'] == 'https://blender.stackexchange.com/a/42'
    if not refusal:
        content = client.get('/api/composition/'+rid).json['saved']['content']
        assert state['messages'][-1]['content'] == content
        assert client.get('/api/composition/'+rid+'/file').data.decode('utf-8') == content
        # The next normal request actually sends the accepted note to the model.
        app.extensions['workspace'].agent().run('Поясни памятку', use_working=False, use_long_term=False)
        assert any(m.get('content') == content for m in fake.calls[0]['input'])
    for _ in range(3):
        again = client.get('/api/state').json
        assert len([r for r in again['requests'] if r['metadata'].get('composition_run_id') == rid]) == 1
    app.extensions['workspace'].close()
    restarted = create_app(data_dir=tmp_path, transport=fake)
    again = restarted.test_client().get('/api/state').json
    assert len([r for r in again['requests'] if r['metadata'].get('composition_run_id') == rid]) == 1
    assert len(model.payloads) == 1
    restarted.extensions['workspace'].close()


def test_stale_identity_and_paused_task_do_not_start_model(tmp_path, monkeypatch):
    app, client, fake, model = setup_chat(tmp_path, monkeypatch)
    data = body(client)
    client.post('/api/dialogue', json=dict(name='Другой', task_id=data['task_id'], mode='sliding'))
    assert client.post('/api/composition', json=data).status_code == 400
    data = body(client)
    client.post('/api/task-state/action', json=dict(profile_id=data['profile_id'],task_id=data['task_id'], action='pause'))
    assert client.post('/api/composition', json=data).status_code == 400
    assert not model.payloads and not fake.calls
    app.extensions['workspace'].close()


def test_pending_chain_blocks_same_dialogue_ordinary_send_but_not_switch(tmp_path, monkeypatch):
    app, client, fake, model = setup_chat(tmp_path, monkeypatch, pending=True)
    data = body(client)
    assert client.post('/api/composition', json=data).status_code == 202
    assert client.post('/api/ask', json=dict(prompt='Второй вопрос')).status_code == 400
    assert not fake.calls
    client.post('/api/dialogue', json=dict(name='Другой', task_id=data['task_id'], mode='sliding'))
    assert not client.get('/api/state').json['composition_runs']
    app.extensions['workspace'].close()


def test_old_page_redirects_to_only_chat(tmp_path):
    app = create_app(data_dir=tmp_path, transport=Fake([]))
    result = app.test_client().get('/composition?run='+'a'*32)
    assert result.status_code == 302
    assert result.headers['Location'].startswith('/?')
    app.extensions['workspace'].close()


def test_completion_keeps_original_profile_dialogue_and_branch(tmp_path, monkeypatch):
    app, client, fake, model = setup_chat(tmp_path, monkeypatch, pending=True)
    client.post('/api/task',json=dict(name='Ветки',project='',mode='branching'))
    state = client.post('/api/checkpoint',json=dict(name='До памятки')).json['state']
    state = client.post('/api/branch',json=dict(name='Памятка',checkpoint_id=state['checkpoints'][0]['id'])).json['state']
    branch = state['active_branch']
    data = body(client)
    rid = client.post('/api/composition',json=data).json['id']
    assert client.post('/api/switch',json=dict(branch_id=1)).status_code == 400
    client.post('/api/profiles',json=dict(name='Другой профиль'))
    store = app.extensions['composition_store']
    run = store.get(rid)
    assert run['branch_id'] == branch
    service = CompositionService(store,providers=Sources(),transport=model)
    materials = asyncio.run(service.search(rid,run['question'],run['query']))
    service.save(service.summarize(materials))
    store.event(rid,'save','success')
    assert client.get('/api/state').json['messages'] == []
    assert client.get('/api/state').json['summary']['api_requests'] == 0
    client.post('/api/profiles/select',json=dict(profile_id=data['profile_id']))
    original = client.get('/api/state').json
    assert original['workspace']['active_dialogue']['id'] == data['dialogue_id']
    assert original['active_branch'] == branch
    assert len(original['messages']) == 2
    assert client.post('/api/switch',json=dict(branch_id=1)).json['state']['messages'] == []
    restored = client.post('/api/switch',json=dict(branch_id=branch)).json['state']
    assert len(restored['messages']) == 2
    assert restored['summary']['api_requests'] == 1
    app.extensions['workspace'].close()


def test_followup_imports_completed_run_even_before_worker_callback(tmp_path, monkeypatch):
    app, client, fake, model = setup_chat(tmp_path, monkeypatch, pending=True, grounded=True)
    rid = client.post('/api/composition',json=body(client)).json['id']
    store = app.extensions['composition_store']
    run = store.get(rid)
    service = CompositionService(store,providers=Sources(),transport=model)
    materials = asyncio.run(service.search(rid,run['question'],run['query']))
    summary = service.summarize(materials)
    service.save(summary)
    store.event(rid,'save','success')
    # No /api/state or worker import between terminal MCP status and the next ask.
    asked = client.post('/api/ask',json=dict(prompt='Поясни первый пункт',use_working=False,use_long_term=False))
    assert asked.json['status'] == 'ok' and asked.json['text'].endswith('[S1]')
    generation = next(call for call in fake.calls
                      if call.get('text', {}).get('format', {}).get('name') == 'task_response')
    assert any(m.get('content') == summary.content for m in generation['input'])
    app.extensions['workspace'].close()

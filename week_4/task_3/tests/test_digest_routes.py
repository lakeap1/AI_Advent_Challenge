from app import create_app
from test_digest_mirror import digest
from test_digest_chat import FakeTransport
from agent.digest_chat import DigestChatAgent


def test_daily_chat_starts_clean_preserves_legacy_and_restores_new_discussion(tmp_path):
    app = create_app(data_dir=tmp_path, transport=FakeTransport())
    pid = app.extensions['workspace']._selected()['id']
    legacy = DigestChatAgent(tmp_path / 'profiles' / str(pid), transport=FakeTransport())
    assert legacy.ask('Старый вопрос о свете', digest()).status == 'ok'
    legacy.close()
    app.extensions['digest_agent'].cache_digests([digest()])
    http = app.test_client()
    initial = http.get('/api/digest/chat?sync=0').json['data']
    assert initial['conversation']['messages'] == []
    assert initial['conversation']['summary']['api_requests'] == 0
    result = http.post('/api/digest/chat/ask', json=dict(profile_id=pid, run_id=1, prompt='Новый вопрос о свете'))
    assert result.status_code == 200
    for chat in app.extensions['digest_chats'].values():
        chat.close()
    app.extensions['workspace'].close()
    reopened = create_app(data_dir=tmp_path)
    restored = reopened.test_client().get('/api/digest/chat?sync=0').json['data']['conversation']
    assert restored['summary']['api_requests'] == 1
    texts = [m['content'] for m in restored['messages']]
    assert 'Новый вопрос о свете' in texts and 'Старый вопрос о свете' not in texts
    archived = DigestChatAgent(tmp_path / 'profiles' / str(pid), transport=FakeTransport())
    assert archived.state()['messages'][0]['content'] == 'Старый вопрос о свете'
    archived.close()
    for chat in reopened.extensions['digest_chats'].values():
        chat.close()
    reopened.extensions['workspace'].close()


def test_profile_and_run_binding_prevents_stale_submission(tmp_path):
    app = create_app(data_dir=tmp_path)
    client = app.test_client()
    mirror = app.extensions['digest_agent']
    mirror.cache_digests([digest(1), digest(2)])
    data = client.get('/api/digest/chat?sync=0').json['data']
    pid = data['profile_id']
    assert data['selected_run_id'] == 2
    assert client.post('/api/digest/chat/select', json=dict(profile_id=pid,run_id=1)).status_code == 200
    assert client.post('/api/digest/chat/ask',json=dict(profile_id=pid,run_id=2,prompt='Question')).status_code == 400
    assert client.post('/api/digest/chat/ask',json=dict(profile_id=pid+1,run_id=1,prompt='Question')).status_code == 400
    assert client.get('/api/digest/chat?sync=0').json['data']['conversation']['summary']['api_requests'] == 0
    app.extensions['workspace'].create_profile(dict(name='Second'))
    second = client.get('/api/digest/chat?sync=0').json['data']
    assert second['profile_id'] != pid and second['selected_run_id'] == 2
    assert mirror.selected(pid) == 1
    for chat in app.extensions['digest_chats'].values():
        chat.close()
    app.extensions['workspace'].close()


def test_unavailable_mcp_keeps_saved_history_visible(tmp_path, monkeypatch):
    app = create_app(data_dir=tmp_path)
    mirror = app.extensions['digest_agent']
    mirror.cache_digests([digest()])
    async def fail(*args):
        raise OSError('offline')
    monkeypatch.setattr(mirror, '_request', fail)
    response = app.test_client().get('/api/digest/chat')
    assert response.status_code == 200
    assert response.json['data']['warning']
    assert response.json['data']['digests'][0]['run_id'] == 1
    for chat in app.extensions['digest_chats'].values():
        chat.close()
    app.extensions['workspace'].close()


def test_failed_manual_collection_does_not_report_previous_digest_as_new_success(tmp_path, monkeypatch):
    app = create_app(data_dir=tmp_path)
    mirror = app.extensions['digest_agent']
    mirror.cache_digests([digest()])
    monkeypatch.setattr(mirror, 'call', lambda *args: dict(executed=True, runs=[dict(status='error')], latest=digest()))
    monkeypatch.setattr(mirror, 'sync', lambda: None)
    response = app.test_client().post('/api/digest/collect', json={})
    assert response.status_code == 502 and response.json['status'] == 'error'
    assert response.json['data']['digests'][0]['run_id'] == 1
    for chat in app.extensions['digest_chats'].values():
        chat.close()
    app.extensions['workspace'].close()

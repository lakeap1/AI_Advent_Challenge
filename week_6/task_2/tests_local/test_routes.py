from app import create_app
from tests_local.test_local_agent import FakeTransport, answer


def test_local_state_starts_without_cloud_tokenizer(tmp_path, monkeypatch):
    import tiktoken
    from rag.chat import RagProfileWorkspace

    def forbidden(*args, **kwargs):
        raise AssertionError('cloud tokenizer was initialized')

    monkeypatch.setattr(tiktoken, 'get_encoding', forbidden)
    monkeypatch.setattr(RagProfileWorkspace, '__init__', forbidden)
    app = create_app(data_dir=tmp_path, local_transport=FakeTransport())
    assert app.test_client().get('/api/local/state').status_code == 200
    app.extensions['workspace'].close()


def test_local_routes_work_without_cloud_key_or_index(tmp_path, monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    fake = FakeTransport({'models': [{'name': 'qwen3.5:9b'}]}, answer())
    app = create_app(data_dir=tmp_path, local_transport=fake)
    client = app.test_client()
    assert client.get('/api/local/state').status_code == 200
    assert client.get('/api/local/runtime').status_code == 200
    result = client.post('/api/local/ask', json={'prompt': 'Как настроить свет?'})
    assert result.status_code == 200
    assert result.json['status'] == 'ok'
    assert len(result.json['state']['messages']) == 2
    assert fake.calls[1][:2] == ('POST', '/api/chat')
    app.extensions['workspace'].close()


def test_local_route_rejects_bad_json_before_model(tmp_path):
    fake = FakeTransport()
    app = create_app(data_dir=tmp_path, local_transport=fake)
    client = app.test_client()
    for value in ([], {}, {'prompt': 4}, {'prompt': 'Вопрос', 'model': 'remote'}):
        assert client.post('/api/local/ask', json=value).status_code == 400
    assert fake.calls == []
    app.extensions['workspace'].close()

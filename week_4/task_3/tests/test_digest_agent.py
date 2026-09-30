"""Public HTTP boundary: admission, output rejection and persisted audit."""
import sqlite3
import pytest
from app import create_app


@pytest.fixture
def client(tmp_path):
    app = create_app(data_dir=tmp_path)
    yield app.test_client(), app.extensions['digest_agent']
    app.extensions['workspace'].close()


@pytest.mark.parametrize('body', [dict(interval_seconds=True), dict(interval_seconds=20), dict(tag='x;evil'), dict(window_hours=12), dict(demo='true'), dict(unexpected=1)])
def test_invalid_configuration_never_reaches_mcp(client, monkeypatch, body):
    http, agent = client
    async def forbidden(*args):
        pytest.fail('Rejected input reached MCP')
    monkeypatch.setattr(agent, '_request', forbidden)
    assert http.post('/api/digest/configure', json=body).status_code == 400
    with sqlite3.connect(agent.path) as db:
        assert db.execute('select input_policy,output_policy,llm_status from calls').fetchone() == ('rejected', 'not_run', 'not_requested')


@pytest.mark.parametrize('result', [
    {'schedule': None, 'runs': [], 'latest': {'text': 'UNTRUSTED SUMMARY'}},
    {'schedule': {}, 'runs': [], 'latest': {}},
])
def test_malformed_mcp_result_is_not_shown_as_success(client, monkeypatch, result):
    http, agent = client
    async def malformed(*args):
        return result
    monkeypatch.setattr(agent, '_request', malformed)
    response = http.get('/api/digest')
    assert response.status_code == 502
    assert 'UNTRUSTED' not in response.text
    with sqlite3.connect(agent.path) as db:
        assert db.execute('select output_policy,status from calls').fetchone() == ('rejected', 'rejected')


def test_mcp_error_distinct_from_empty_success(client, monkeypatch):
    http, agent = client
    async def failed(*args):
        raise OSError('offline')
    monkeypatch.setattr(agent, '_request', failed)
    assert http.get('/api/digest').status_code == 502
    async def valid(*args):
        return dict(schedule=dict(enabled=False, mode='cron', timezone='Asia/Omsk', hours=[0,6,12,18],
                                  publication_hour=0, publication_timezone='Asia/Omsk', publication_window='previous_calendar_day',
                                  window_hours=24, next_due=None, sources=['blender'], backoff_until=None), runs=[], latest=None)
    monkeypatch.setattr(agent, '_request', valid)
    response = http.get('/api/digest')
    assert response.status_code == 200 and response.json['data']['latest'] is None
    with sqlite3.connect(agent.path) as db:
        assert db.execute('select status from calls order by id').fetchall() == [('transport_error',), ('ok',)]

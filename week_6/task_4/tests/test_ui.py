"""HTTP behavior of the Day 29 Flask wrapper."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import create_app


class ServiceStub:
    instances = []

    def __init__(self, data_dir, local_transport=None):
        self.data_dir = data_dir
        self.calls = []
        self.instances.append(self)

    def state(self):
        self.calls.append(('state',))
        return {'index': {'status': 'ready', 'chunks': 3}, 'presets': {
            'baseline': {'model': 'qwen3.5:9b', 'temperature': 0.2,
                         'context_window': 8192, 'max_output_tokens': 1024},
            'optimized': {'model': 'qwen3.5:9b', 'temperature': 0,
                          'context_window': 6144, 'max_output_tokens': 512}},
            'selected_preset': 'optimized', 'blocked': False,
            'history': [], 'receipts': [],
            'ledger': {'total_usd': 0, 'known_usd': 0, 'incomplete': False,
                       'receipt_count': 0}}

    def build_index(self):
        self.calls.append(('build',))
        return {'status': 'ok', 'index': {'status': 'ready'},
                'ledger': {'total_usd': 0, 'known_usd': 0, 'incomplete': False}}

    def ask(self, prompt, preset='optimized'):
        self.calls.append(('ask', prompt, preset))
        return {'status': 'ok', 'question': prompt, 'retrieval': None,
                'results': [], 'error': None,
                'ledger': {'total_usd': 0, 'known_usd': 0, 'incomplete': False}}


@pytest.fixture
def client(monkeypatch, tmp_path):
    import app as day29

    monkeypatch.setattr(day29, 'OptimizationService', ServiceStub)
    ServiceStub.instances.clear()
    flask_app = create_app(data_dir=tmp_path, local_transport=lambda *_: None)
    return flask_app.test_client(), tmp_path, ServiceStub.instances[-1]


def test_home_preserves_day28_and_chat_without_generation(client):
    http, _, service = client
    html = http.get('/').get_data(as_text=True)
    assert 'id="optimization-prompt"' in html
    assert 'href="/day28"' in html
    assert 'href="/chat"' in html
    assert '/day29-static/optimization.js' in html
    assert http.get('/day28').status_code == 200
    assert http.get('/chat').status_code == 200
    assert http.get('/day29-static/optimization.js').status_code == 200
    assert service.calls == []


def test_ask_and_build_reject_extra_or_wrong_payload_before_service(client):
    http, _, service = client
    for payload in ({'prompt': 'x', 'preset': 'compare', 'secret': 1},
                    {'prompt': 'x', 'preset': 7}, {'preset': 'baseline'},
                    ['x']):
        response = http.post('/api/optimization/ask', json=payload)
        assert response.status_code == 400
        assert response.json['error']['code'] == 'invalid_payload'
    assert http.post('/api/optimization/build', json={'force': True}).status_code == 400
    assert service.calls == []
    response = http.post('/api/optimization/ask', json={'prompt': 'Свет?', 'preset': 'compare'})
    assert response.status_code == 200
    assert service.calls == [('ask', 'Свет?', 'compare')]
    assert http.post('/api/optimization/build', json={}).status_code == 200
    assert service.calls[-1] == ('build',)


def test_saved_report_is_labelled_and_read_only(client):
    http, data_dir, service = client
    path = data_dir / 'selected-report.json'
    report = {'created_at': '2026-10-12T10:00:00Z',
              'quality': {'baseline': 0.6, 'optimized': 0.8},
              'speed': {'baseline': 8, 'optimized': 12},
              'resources': {'baseline': 6000, 'optimized': 5800}}
    path.write_text(json.dumps(report), encoding='utf-8')
    before = path.read_bytes()
    response = http.get('/api/optimization/state')
    assert response.status_code == 200
    assert response.json['benchmark']['kind'] == 'saved'
    assert response.json['benchmark']['report'] == report
    assert path.read_bytes() == before
    assert service.calls == [('state',)]

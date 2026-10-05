"""HTTP contracts for the primary Day 23 conversation controls."""

import pytest

from agent.core import AgentResult
from app import create_app


class Conversation:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or AgentResult('ok', 'Ответ.')

    def run(self, prompt, **options):
        self.calls.append(('run', prompt, options))
        return self.result

    def preview(self, prompt, **options):
        self.calls.append(('preview', prompt, options))
        return {'status': 'ok', 'token_metrics': {}}


@pytest.fixture
def conversation_client(tmp_path):
    app = create_app(data_dir=tmp_path)
    conversation = Conversation()
    app.extensions['workspace'].agent = lambda: conversation
    yield app.test_client(), conversation
    app.extensions['workspace'].close()


@pytest.mark.parametrize('path', ['/api/ask', '/api/preview'])
def test_explicit_mode_controls_primary_request_even_with_legacy_false(conversation_client, path):
    client, conversation = conversation_client
    result = client.post(path, json={'prompt': 'Вопрос', 'use_rag': False, 'rag_mode': 'rewrite_filter'})
    assert result.status_code == 200
    assert conversation.calls[-1][2]['rag_mode'] == 'rewrite_filter'


@pytest.mark.parametrize('path', ['/api/ask', '/api/preview'])
@pytest.mark.parametrize('field,value', [
    ('rag_mode', 'experimental'), ('rag_mode', True), ('rag_mode', None), ('use_rag', 'yes'),
    ('retrieval', {}), ('sources', []), ('context', 'injected'),
    ('scores', {'chunk': 3}), ('candidates', []),
])
def test_invalid_mode_or_client_retrieval_stops_before_agent(conversation_client, path, field, value):
    client, conversation = conversation_client
    result = client.post(path, json={'prompt': 'Вопрос', field: value})
    assert result.status_code == 400
    assert result.json['status'] == 'rejected'
    assert conversation.calls == []


def test_no_context_is_visible_terminal_http_result(conversation_client):
    client, conversation = conversation_client
    conversation.result = AgentResult('no_context', 'Релевантный контекст не найден.', 'no_context')
    result = client.post('/api/ask', json={'prompt': 'Вопрос', 'rag_mode': 'filter'})
    assert result.status_code == 200
    assert result.json['status'] == 'no_context'
    assert result.json['code'] == 'no_context'

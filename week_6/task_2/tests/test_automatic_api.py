"""The public endpoint rejects stale source settings before charging the model."""
from app import create_app
from test_strategies import Fake


def test_stale_manual_retrieval_requires_refresh_without_model_call(tmp_path):
    transport = Fake([])
    app = create_app(data_dir=tmp_path, transport=transport)
    try:
        response = app.test_client().post('/api/ask', json={
            'prompt': 'Explain normal mapping',
            'retrieval': {'sources': ['wikipedia'], 'query': 'normal mapping'},
        })
        assert response.status_code == 400
        assert response.json['status'] == 'rejected'
        assert 'автоматически' in response.json['text']
        assert transport.calls == []
        assert app.extensions['workspace'].state()['requests'] == []
    finally:
        app.extensions['workspace'].close()

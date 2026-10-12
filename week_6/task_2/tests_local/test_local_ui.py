"""The local page is served while cloud workspace construction is unavailable."""

from app import create_app
from rag.chat import RagProfileWorkspace


def test_local_page_and_state_need_no_cloud_workspace(tmp_path, monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)

    def forbidden(*args, **kwargs):
        raise AssertionError('cloud workspace initialized during local entry')

    monkeypatch.setattr(RagProfileWorkspace, '__init__', forbidden)
    app = create_app(data_dir=tmp_path)
    try:
        client = app.test_client()
        page = client.get('/')
        assert page.status_code == 200
        html = page.get_data(as_text=True)
        for control in ('local-chat-open', 'local-chat-view', 'local-ask-form',
                        'local-prompt', 'local-send', 'local-feed', 'local-notice',
                        'local-runtime', 'local-accounting'):
            assert f'id="{control}"' in html
        state = client.get('/api/local/state')
        assert state.status_code == 200
        assert state.json['messages'] == []
    finally:
        app.extensions['workspace'].close()

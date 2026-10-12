"""The Day 28 page is served by the real Flask wrapper without model calls."""

from app import create_app


def test_local_rag_home_and_preserved_chat(tmp_path):
    app = create_app(data_dir=tmp_path)
    client = app.test_client()
    home = client.get('/')
    assert home.status_code == 200
    html = home.get_data(as_text=True)
    assert 'id="rag-prompt"' in html
    assert 'id="rag-sources"' in html
    assert 'href="/chat"' in html
    assert '/day28-static/local-rag.js' in html
    assert client.get('/chat').status_code == 200
    assert client.get('/day28-static/local-rag.js').status_code == 200
    assert client.get('/day28-static/local-rag.css').status_code == 200

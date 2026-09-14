from app import create_app
from test_app import Stub

def test_mode_routes_are_isolated_and_invalid_mode_is_rejected():
    full, compressed = Stub(), Stub()
    client = create_app({'full': full, 'compressed': compressed}).test_client()
    assert client.post('/api/ask', json={'mode': 'full', 'prompt': 'Бриф'}).status_code == 200
    assert full.calls == ['Бриф'] and compressed.calls == []
    assert client.post('/api/ask', json={'mode': 'compressed', 'prompt': 'UV'}).status_code == 200
    assert compressed.calls == ['UV']
    assert client.get('/api/state?mode=bad').status_code == 400
    assert client.post('/api/ask', json={'mode': [], 'prompt': 'x'}).status_code == 400
    assert compressed.calls == ['UV']

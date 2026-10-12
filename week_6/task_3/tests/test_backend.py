"""Day 28 backend contract with local, deterministic provider responses."""

import hashlib
import io
import json
from pathlib import Path
import sys

import pytest

from local_rag.service import LocalRag


DIM = 1024


def vector(first=1.0):
    return [first] + [0.0] * (DIM - 1)


class LocalFake:
    def __init__(self, *, missing_usage=False, bad_label=False, truncated=False,
                 bad_vector=False, length_limit=False, bad_message=False):
        self.calls = []
        self.missing_usage = missing_usage
        self.bad_label = bad_label
        self.truncated = truncated
        self.bad_vector = bad_vector
        self.length_limit = length_limit
        self.bad_message = bad_message

    def __call__(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if path == '/api/tags':
            return {'models': [
                {'name': 'qwen3-embedding:0.6b', 'digest': 'embed-digest'},
                {'name': 'qwen3.5:9b', 'digest': 'chat-digest'},
            ]}
        if path == '/api/show':
            return {'details': {'parameter_size': '0.6B'}}
        if path == '/api/embed':
            items = payload['input'] if isinstance(payload['input'], list) else [payload['input']]
            embedding = [0.0] * DIM if self.bad_vector else vector()
            body = {'embeddings': [embedding for _ in items], 'total_duration': 1000}
            if not self.missing_usage:
                body['prompt_eval_count'] = len(items) * 8
            return body
        if path == '/api/chat':
            label = 'BAD' if self.bad_label else 'S1'
            return {
                'done': not self.truncated,
                'done_reason': 'length' if self.length_limit else 'stop',
                'message': None if self.bad_message else {'content': json.dumps({
                    'answer': 'Smoothness управляет гладкостью поверхности.', 'source_labels': [label]})},
                'prompt_eval_count': 80, 'eval_count': 18, 'total_duration': 1000,
            }
        raise AssertionError(path)


class CloudFake:
    def __init__(self):
        self.calls = []

    def __call__(self, payload):
        self.calls.append(payload)
        return {
            'status': 'completed',
            'output': [{'content': [{'type': 'output_text', 'text': json.dumps({
                'answer': 'Облачный ответ.', 'source_labels': ['S1'],
            })}]}],
            'usage': {'input_tokens': 80, 'output_tokens': 20, 'total_tokens': 100,
                      'input_tokens_details': {'cached_tokens': 0},
                      'output_tokens_details': {'reasoning_tokens': 0}},
        }


@pytest.fixture
def roots(tmp_path):
    base = tmp_path / 'base'
    task = tmp_path / 'task'
    for root in (base, task):
        (root / 'corpus').mkdir(parents=True)
    (base / 'corpus' / 'manifest.json').write_text(json.dumps({'version': 1, 'files': []}), encoding='utf-8')
    (task / 'corpus' / 'unity.md').write_text('# URP Material\nSmoothness controls highlight width.\n', encoding='utf-8')
    (task / 'corpus' / 'manifest.json').write_text(json.dumps({'version': 1, 'files': [{
        'file': 'corpus/unity.md', 'source': 'https://docs.unity3d.com/6000.0/Documentation/Manual/urp/lit-shader.html',
        'title': 'Unity URP Lit',
    }]}), encoding='utf-8')
    return base, task, tmp_path / 'data'


def make_service(roots, local=None, cloud=None):
    base, task, data = roots
    return LocalRag(data, task_root=task, base_root=base,
                    local_transport=local or LocalFake(), cloud_transport=cloud)


def test_local_build_and_ask_without_cloud_key(roots, monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    fake = LocalFake()
    service = make_service(roots, local=fake)
    built = service.build_index()
    assert built['status'] == 'ok'
    result = service.ask('Что делает smoothness?', mode='local')
    assert result['status'] == 'ok'
    assert result['retrieval']['sources'][0]['source'].startswith('https://docs.unity3d.com/')
    assert result['retrieval']['sources'][0]['line_start'] >= 1
    assert result['results'][0]['source_labels'] == ['S1']
    assert result['results'][0]['receipt']['usage']['total_tokens'] == 98
    assert result['ledger']['total_usd'] == 0
    assert any(path == '/api/embed' and payload['truncate'] is False for _, path, payload in fake.calls)


def test_q5_retrieval_keeps_cpu_variants_and_built_in_evidence_over_readme_noise():
    from local_rag.index import retrieve

    questions = json.loads((Path(__file__).resolve().parents[1] / 'evaluation' / 'questions.json').read_text(encoding='utf-8'))
    question = next(item['prompt'] for item in questions['questions'] if item['id'] == 'q5')
    noise = ('В проекте и в рабочем процессе используются записи и материалы для обучения. ' * 12)
    cpu = ('SRP Batcher уменьшает CPU-время подготовки и отправки draw calls для '
           'материалов, использующих один shader variant. Он уменьшает смены состояния '
           'рендеринга между draw calls; это отличается от уменьшения самого количества '
           'draw calls. Для эффективности желательно меньше shader variants.')
    compatibility = ('SRP Batcher поддерживается в URP и HDRP. Built-in Render Pipeline '
                     'его не поддерживает.')

    def row(identifier, file, text):
        return ({'chunk_id': identifier, 'file': file, 'title': file, 'source': file,
                 'section': ['Document'], 'line_start': 1, 'line_end': 2, 'text': text}, [1.0, 0.0])

    rows = [row('readme-1', 'corpus/project_readme.md', noise),
            row('readme-2', 'corpus/project_readme.md', noise + ' Дополнение.'),
            row('compatibility', 'corpus/unity6_srp_batcher.md', compatibility),
            row('cpu-variants', 'corpus/unity6_srp_batcher.md', cpu)]
    found = retrieve(rows, question, [1.0, 0.0], {'index_id': 'frozen'},
                     {'top_k': 3, 'context_bytes': 4200})
    selected = {source['chunk_id'] for source in found['sources']}
    assert {'cpu-variants', 'compatibility'} <= selected
    assert 'CPU-время' in found['context'] and 'Built-in' in found['context']


def test_stale_corpus_and_model_digest_stop_before_generation(roots):
    fake = LocalFake()
    service = make_service(roots, local=fake)
    service.build_index()
    (roots[1] / 'corpus' / 'unity.md').write_text('# Changed\n', encoding='utf-8')
    stale = service.ask('smoothness?')
    assert stale['status'] == 'error' and stale['error']['code'] == 'stale_index'
    assert not any(path == '/api/chat' for _, path, _ in fake.calls)
    (roots[1] / 'corpus' / 'unity.md').write_text('# URP Material\nSmoothness controls highlight width.\n', encoding='utf-8')
    fake.calls.clear()
    original = fake.__call__
    def changed(method, path, payload=None):
        body = original(method, path, payload)
        if path == '/api/tags':
            body['models'][0]['digest'] = 'new-digest'
        return body
    service.local_transport = changed
    stale = service.ask('smoothness?')
    assert stale['status'] == 'error' and stale['error']['code'] == 'stale_index'
    assert not any(path == '/api/chat' for _, path, _ in fake.calls)


def test_changed_generation_digest_stops_before_query_embedding(roots):
    fake = LocalFake()
    service = make_service(roots, local=fake)
    service.build_index()
    original = fake.__call__
    fake.calls.clear()
    def changed(method, path, payload=None):
        body = original(method, path, payload)
        if path == '/api/tags':
            body['models'][1]['digest'] = 'changed-generation'
        return body
    service.local_transport = changed
    result = service.ask('Smoothness?')
    assert result['status'] == 'error' and result['error']['code'] == 'stale_index'
    assert not any(path in ('/api/embed', '/api/chat') for _, path, _ in fake.calls)


def test_malformed_vector_or_missing_usage_never_publish_index(roots):
    for fake in (LocalFake(bad_vector=True), LocalFake(missing_usage=True)):
        service = make_service(roots, local=fake)
        result = service.build_index()
        assert result['status'] == 'error'
        assert service.state()['index']['status'] != 'ready'
        if fake.missing_usage:
            assert service.state()['blocked'] is True
            restarted = make_service(roots, local=LocalFake())
            assert restarted.state()['blocked'] is True
            assert restarted.build_index()['error']['code'] == 'unknown_receipt'


def test_bad_label_and_truncation_reject_with_known_usage(roots):
    for kwargs in ({'bad_label': True}, {'truncated': True},
                   {'length_limit': True}, {'bad_message': True}):
        fake = LocalFake(**kwargs)
        service = make_service(roots, local=fake)
        service.build_index()
        result = service.ask('Smoothness?')
        assert result['status'] == 'rejected'
        assert result['results'][0]['status'] == 'rejected'
        assert result['results'][0]['receipt']['usage']['total_tokens'] == 98
        assert service.state()['blocked'] is False


def test_compare_reuses_snapshot_and_stops_cloud_after_local_rejection(roots, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-test-key')
    local, cloud = LocalFake(), CloudFake()
    service = make_service(roots, local=local, cloud=cloud)
    service.build_index()
    result = service.ask('Smoothness?', mode='compare')
    assert result['status'] == 'ok' and len(result['results']) == 2
    assert len(cloud.calls) == 1
    local_calls = [payload for _, path, payload in local.calls if path == '/api/chat']
    assert len(local_calls) == 1
    local_messages = local_calls[0]['messages']
    assert cloud.calls[0]['instructions'] == local_messages[0]['content']
    assert cloud.calls[0]['input'] == local_messages[1]['content']
    expected_hash = hashlib.sha256(result['retrieval']['context'].encode('utf-8')).hexdigest()
    assert result['retrieval']['context_sha256'] == expected_hash
    assert local_messages[1]['content'] == (
        f'Вопрос:\n{result["question"]}\n\nКонтекст источников:\n{result["retrieval"]["context"]}')
    snapshot = {'question': result['question'], 'system': local_messages[0]['content'],
                'context': result['retrieval']['context']}
    expected_request_hash = hashlib.sha256(json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
    assert result['retrieval']['request_sha256'] == expected_request_hash
    assert [item['request_sha256'] for item in result['results']] == [expected_request_hash] * 2
    history = service.state()['history'][0]
    assert history['retrieval']['request_sha256'] == expected_request_hash
    assert [item['request_sha256'] for item in history['results']] == [expected_request_hash] * 2
    bad_local, cloud2 = LocalFake(bad_label=True), CloudFake()
    second = make_service((roots[0], roots[1], roots[2] / 'other'), local=bad_local, cloud=cloud2)
    second.build_index()
    assert second.ask('Smoothness?', mode='compare')['status'] == 'rejected'
    assert cloud2.calls == []


def test_cloud_partial_usage_blocks_next_request_after_restart(roots, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-test-key')
    def partial(_payload):
        return {'status': 'completed', 'output': [{'content': [{'type': 'output_text',
                'text': json.dumps({'answer': 'Облако', 'source_labels': ['S1']})}]}],
                'usage': {'input_tokens': 80, 'output_tokens': 20, 'total_tokens': 100}}
    service = make_service(roots, cloud=partial)
    service.build_index()
    result = service.ask('Smoothness?', mode='cloud')
    assert result['status'] == 'rejected'
    assert result['results'][0]['receipt']['usage']['input_tokens'] == 80
    assert result['results'][0]['receipt']['cost_usd'] is None
    assert result['ledger']['incomplete'] is True
    restarted = make_service(roots, cloud=CloudFake())
    assert restarted.ask('Smoothness?', mode='cloud')['error']['code'] == 'unknown_receipt'


def test_cloud_service_tier_is_explicit_and_unexpected_tier_blocks(roots, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-test-key')
    class OtherTier(CloudFake):
        def __call__(self, payload):
            body = super().__call__(payload)
            body['service_tier'] = 'priority'
            return body
    cloud = OtherTier()
    service = make_service(roots, cloud=cloud)
    service.build_index()
    result = service.ask('Smoothness?', mode='cloud')
    assert cloud.calls[0]['service_tier'] == 'default'
    assert result['status'] == 'rejected'
    assert result['results'][0]['receipt']['usage']['total_tokens'] == 100
    assert result['results'][0]['receipt']['cost_usd'] is None
    assert result['ledger']['incomplete'] is True


def test_malformed_cloud_output_keeps_reported_usage_and_cost(roots, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-test-key')
    class BrokenOutput(CloudFake):
        def __call__(self, payload):
            body = super().__call__(payload)
            body['output'] = None
            return body
    service = make_service(roots, cloud=BrokenOutput())
    service.build_index()
    result = service.ask('Smoothness?', mode='cloud')
    assert result['status'] == 'rejected'
    receipt = result['results'][0]['receipt']
    assert receipt['usage']['total_tokens'] == 100
    assert receipt['cost_usd'] == 0.000018
    assert result['ledger']['incomplete'] is False


def test_pending_receipt_survives_restart(roots):
    service = make_service(roots)
    service.build_index()
    service.store.start_receipt('generation', 'local', 'qwen3.5:9b', {'status': 'accepted'})
    restarted = make_service(roots)
    assert restarted.state()['blocked'] is True
    assert restarted.ask('Smoothness?')['error']['code'] == 'unknown_receipt'


def test_routes_strict_payload_and_previous_home(roots):
    from app import create_app
    app = create_app(data_dir=roots[2], local_transport=LocalFake())
    client = app.test_client()
    assert client.get('/chat').status_code == 200
    assert client.post('/api/local-rag/build', json={'unexpected': True}).status_code == 400
    assert client.post('/api/local-rag/ask', json={'prompt': 'x', 'context': 'injected'}).status_code == 400
    assert client.post('/api/local-rag/ask', json={'prompt': 'x', 'mode': 'fallback'}).status_code == 400


def test_local_http_api_needs_no_cloud_key(roots, monkeypatch):
    import local_rag.service as service_module
    monkeypatch.setattr(service_module, 'BASE_ROOT', roots[0])
    monkeypatch.setattr(service_module, 'TASK_ROOT', roots[1])
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    from app import create_app
    client = create_app(data_dir=roots[2], local_transport=LocalFake()).test_client()
    assert client.post('/api/local-rag/build', json={}).status_code == 200
    response = client.post('/api/local-rag/ask', json={'prompt': 'Smoothness?'})
    assert response.status_code == 200
    assert response.json['results'][0]['answer']
    assert response.json['results'][0]['receipt']['usage']['total_tokens'] == 98


def test_cli_writes_utf8_json_when_console_encoding_cannot_encode_source(monkeypatch):
    from local_rag import cli

    class SavedResult:
        def __init__(self, _data_dir):
            pass

        def ask(self, _prompt, mode='local'):
            return {'status': 'ok', 'retrieval': {'sources': [{'text': 'albedo → normal map'}]},
                    'results': [{'answer': 'Готово: →'}]}

    stream = io.BytesIO()
    console = io.TextIOWrapper(stream, encoding='cp1251', errors='strict')
    monkeypatch.setattr(cli, 'LocalRag', SavedResult)
    monkeypatch.setattr(sys, 'stdout', console)

    assert cli.main(['ask', '--prompt', 'smoothness?', '--mode', 'local']) == 0
    raw = stream.getvalue()
    assert json.loads(raw.decode('utf-8'))['results'][0]['answer'] == 'Готово: →'
    assert '→'.encode('utf-8') in raw
    assert b'\\u2192' not in raw

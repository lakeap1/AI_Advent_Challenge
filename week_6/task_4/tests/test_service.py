import hashlib
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from optimization.service import OptimizationService, OPTIMIZED_SYSTEM_TEXT
from local_rag.service import SYSTEM_TEXT


class FakeOllama:
    def __init__(self):
        self.calls = []
        self.missing_usage = False
        self.bad_label = False
        self.missing_q8 = False
        self.truncated = False
        self.wrong_model = False

    def __call__(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if path == '/api/tags':
            models = [{'name': 'qwen3-embedding:0.6b', 'digest': 'embed'},
                      {'name': 'qwen3.5:9b', 'digest': 'q4'}]
            if not self.missing_q8:
                models.append({'name': 'qwen3.5:9b-q8_0', 'digest': 'q8'})
            return {'models': models}
        if path == '/api/show':
            return {'details': {'quantization_level': 'Q8_0' if payload['model'].endswith('q8_0') else 'Q4_K_M'}}
        if path == '/api/embed':
            return {'embeddings': [[1.0] + [0.0] * 1023 for _ in payload['input']],
                    'prompt_eval_count': 8}
        if path == '/api/chat':
            body = {'model': 'wrong:model' if self.wrong_model else payload['model'],
                    'done': True, 'done_reason': 'length' if self.truncated else 'stop',
                    'message': {'content': json.dumps({'answer': 'Гладкость задаёт ширину блика.',
                                                        'source_labels': ['BAD' if self.bad_label else 'S1']})},
                    'prompt_eval_count': 80, 'eval_count': 12,
                    'total_duration': 2_000_000_000, 'load_duration': 200_000_000,
                    'prompt_eval_duration': 300_000_000, 'eval_duration': 1_000_000_000}
            if self.missing_usage:
                del body['eval_count']
            return body
        raise AssertionError(path)


def test_compare_reuses_retrieval_and_records_exact_requests(tmp_path):
    fake = FakeOllama()
    service = OptimizationService(tmp_path, local_transport=fake)
    assert service.build_index()['status'] == 'ok'
    result = service.ask('Как настроить Smoothness в Unity?', 'compare')
    assert result['status'] == 'ok'
    assert [item['preset'] for item in result['results']] == ['baseline', 'optimized']
    assert len([call for call in fake.calls if call[1] == '/api/embed']) > 0
    assert len([call for call in fake.calls if call[1] == '/api/embed' and call[2]['input'] == ['Как настроить Smoothness в Unity?']]) == 1
    first, second = result['results']
    assert first['request_sha256'] != second['request_sha256']
    assert first['receipt']['usage']['total_tokens'] == 92
    assert first['metrics']['eval_tokens_per_second'] == 12
    assert first['receipt']['tariff']['preset'] == 'baseline'
    assert first['receipt']['tariff']['prompt_version'] == 'day28-original'
    assert second['receipt']['tariff']['prompt_version'] == 'day29-graphics-v2'
    assert result['retrieval']['context_sha256'] == hashlib.sha256(result['retrieval']['context'].encode()).hexdigest()
    payloads = [call[2] for call in fake.calls if call[1] == '/api/chat']
    assert payloads[0]['options'] == {'num_ctx': 8192, 'num_predict': 1024, 'temperature': 0.2, 'seed': 42}
    assert payloads[1]['options'] == {'num_ctx': 6144, 'num_predict': 512, 'temperature': 0.0, 'seed': 42}
    assert all(payload['think'] is False for payload in payloads)
    assert payloads[0]['messages'][0]['content'] == SYSTEM_TEXT
    assert payloads[1]['messages'][0]['content'] == OPTIMIZED_SYSTEM_TEXT
    for item, payload in zip(result['results'], payloads):
        exact_hash = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                                separators=(',', ':')).encode()).hexdigest()
        assert item['request_sha256'] == exact_hash
    assert service.state()['history']


def test_rejection_and_unknown_stop_compare_and_survive_restart(tmp_path):
    fake = FakeOllama()
    service = OptimizationService(tmp_path, local_transport=fake)
    assert service.build_index()['status'] == 'ok'
    fake.bad_label = True
    rejected = service.ask('Как настроить Smoothness?', 'compare')
    assert rejected['status'] == 'rejected'
    assert len(rejected['results']) == 1
    assert rejected['results'][0]['receipt']['usage']['total_tokens'] == 92
    fake.bad_label = False
    fake.missing_usage = True
    unknown = service.ask('Как настроить Smoothness?', 'compare')
    assert len(unknown['results']) == 1
    assert unknown['ledger']['incomplete']
    restarted = OptimizationService(tmp_path, local_transport=fake)
    before = len(fake.calls)
    assert restarted.ask('Как настроить Smoothness?', 'optimized')['error']['code'] == 'unknown_receipt'
    assert len(fake.calls) == before


def test_input_and_missing_q8_are_rejected_before_generation(tmp_path):
    fake = FakeOllama()
    service = OptimizationService(tmp_path, local_transport=fake)
    rejected = service.ask('', 'compare')
    assert rejected['status'] == 'rejected'
    assert rejected['error']['code'] == 'input_policy'
    assert not fake.calls
    assert service.build_index()['status'] == 'ok'
    fake.missing_q8 = True
    before = len([call for call in fake.calls if call[1] == '/api/chat'])
    result = service.ask('Как настроить Smoothness?', 'quantized')
    assert result['status'] == 'error'
    assert result['error']['code'] == 'model_unavailable'
    assert len([call for call in fake.calls if call[1] == '/api/chat']) == before


def test_invalid_preset_and_context_overflow_never_call_generator(tmp_path):
    fake = FakeOllama()
    service = OptimizationService(tmp_path, local_transport=fake)
    invalid = service.ask('test', 'invented')
    assert invalid['error']['code'] == 'invalid_preset'
    assert service.state()['receipts'][0]['input_policy']['status'] == 'rejected'
    assert service.state()['receipts'][0]['error_code'] == 'invalid_preset'
    assert service.build_index()['status'] == 'ok'
    oversized = 'a' * 10000
    retrieval = {'sources': [{'label': 'S1'}], 'context': oversized,
                 'context_sha256': hashlib.sha256(oversized.encode()).hexdigest()}
    from local_rag.service import ServiceError
    import pytest
    with pytest.raises(ServiceError) as error:
        service.generate('Тест?', retrieval, 'optimized')
    assert error.value.code == 'context_overflow'
    assert not [call for call in fake.calls if call[1] == '/api/chat']


def test_optimized_prompt_requires_complete_causal_grounding_without_gold_facts():
    text = OPTIMIZED_SYSTEM_TEXT.casefold()
    for phrase in ('каждую часть вопроса', 'точные названия', 'причин', 'направлен',
                   'подготов', 'услов', 'ограничен', 'только', 'без markdown'):
        assert phrase in text
    for answer_specific in ('workflowmode', 'colorspace', 'metallic', 'occlusion'):
        assert answer_specific not in text


def test_model_mismatch_and_length_rejection_keep_incurred_usage(tmp_path):
    fake = FakeOllama()
    service = OptimizationService(tmp_path, local_transport=fake)
    assert service.build_index()['status'] == 'ok'
    fake.wrong_model = True
    wrong = service.ask('Как настроить Smoothness?', 'compare')
    assert wrong['status'] == 'rejected'
    assert len(wrong['results']) == 1
    assert wrong['results'][0]['receipt']['cost_usd'] == 0
    assert wrong['results'][0]['receipt']['usage']['total_tokens'] == 92
    fake.wrong_model = False
    fake.truncated = True
    short = service.ask('Как настроить Smoothness?', 'compare')
    assert short['status'] == 'rejected'
    assert len(short['results']) == 1
    assert short['results'][0]['receipt']['output_policy']['code'] == 'incomplete_response'
    assert short['results'][0]['receipt']['usage']['total_tokens'] == 92


def test_import_is_read_only_and_does_not_copy_receipts(tmp_path):
    fake = FakeOllama()
    source = OptimizationService(tmp_path / 'source', local_transport=fake)
    assert source.build_index()['status'] == 'ok'
    source.ask('Как настроить Smoothness?')
    source_file = source.rag.store.path
    before = hashlib.sha256(source_file.read_bytes()).hexdigest()
    target = OptimizationService(tmp_path / 'target', local_transport=fake)
    assert target.import_index(source_file)['status'] == 'ok'
    assert hashlib.sha256(source_file.read_bytes()).hexdigest() == before
    assert target.state()['ledger']['receipt_count'] == 0
    assert target.state()['index']['status'] == 'ready'

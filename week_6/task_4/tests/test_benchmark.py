import copy
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.benchmark import analyze, execution_order, run_series


def item(preset, status='ok', cost=0):
    return {'preset': preset, 'status': status, 'elapsed_seconds': 2,
            'metrics': {'eval_seconds': 1, 'eval_tokens_per_second': 100},
            'receipt': {'cost_usd': cost, 'usage_status': 'complete' if cost is not None else 'unknown'},
            'resources': {'resident': {'size_vram': 8_000_000_000},
                          'sampled_gpu_peak_mib': None, 'python_rss_bytes': 1000}}


def test_analysis_keeps_failed_attempts_and_unknown_resources_separate():
    run = {'cases': [{'results': [item('baseline'), item('optimized', 'rejected')]},
                     {'results': [item('baseline', 'error', None)]}]}
    original = copy.deepcopy(run)
    report = analyze(run)
    assert report['presets']['baseline']['attempts'] == 2
    assert report['presets']['baseline']['successes'] == 1
    assert report['presets']['baseline']['unknown_usage'] is True
    assert report['presets']['optimized']['successes'] == 0
    assert report['presets']['optimized']['wall_seconds']['median'] is None
    assert report['presets']['baseline']['sampled_gpu_peak_mib']['median'] is None
    assert report['presets']['baseline']['tokens_per_second']['median'] == 100
    assert run == original


def test_orders_counterbalance_pair_position():
    orders = [execution_order(i, ('baseline', 'optimized')) for i in range(6)]
    assert orders.count(['baseline', 'optimized']) == 3
    assert orders.count(['optimized', 'baseline']) == 3


class FakeService:
    def __init__(self):
        self.calls = []
    def generate(self, prompt, snapshot, preset):
        self.calls.append(preset)
        return item(preset, status='rejected')


def test_failure_preserves_partial_and_does_not_call_next_preset(tmp_path):
    service = FakeService()
    target = tmp_path / 'run.json'
    run = run_series(service, [{'id': 'q', 'prompt': 'x'}], {'q': {'context_sha256': 'same'}},
                     target, repeats=3, presets=('baseline', 'optimized'), measure=False)
    assert service.calls == ['baseline']
    assert run['completed'] is False
    assert len(run['cases']) == 1
    assert json.loads(target.read_text('utf-8')) == run


def test_existing_result_is_rejected_before_generation(tmp_path):
    target = tmp_path / 'accepted.json'
    target.write_text('unchanged', 'utf-8')
    service = FakeService()
    try:
        run_series(service, [], {}, target, presets=('baseline',), measure=False)
    except FileExistsError:
        pass
    else:
        raise AssertionError('previous run must be preserved')
    assert service.calls == []
    assert target.read_text('utf-8') == 'unchanged'


def test_preflight_failure_is_explicit_partial_without_pending(tmp_path):
    class MissingModel(Exception):
        code = 'model_unavailable'
        message = 'Q8 unavailable'
    class Service:
        def generate(self, *_):
            raise MissingModel()
    target = tmp_path / 'missing-model.json'
    run = run_series(Service(), [{'id': 'q', 'prompt': 'x'}], {'q': {}}, target,
                     presets=('quantized',), measure=False)
    assert run['pending'] is None
    assert run['error']['detail']['code'] == 'model_unavailable'
    assert run['cases'][0]['results'][0]['status'] == 'not_run'


def test_report_counts_warmup_and_does_not_invent_selection():
    warm = item('baseline')
    warm['receipt']['cost_usd'] = 0.01
    run = {'cases': [{'results': [item('baseline')]}], 'warmups': [warm],
           'ledger': {'known_usd': 0.02, 'total_usd': 0.02, 'incomplete': False}}
    report = analyze(run)
    assert report['selected_preset'] is None
    assert report['presets']['baseline']['known_cost_usd'] == 0.01
    assert report['ledger']['known_usd'] == 0.02


def test_unavailable_optional_resource_metadata_stays_unknown(monkeypatch):
    from scripts import resources
    class BadClient:
        def __init__(self, **_): pass
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def get(self, path):
            if path == '/api/ps': raise resources.httpx.ConnectError('no ps')
            class Response:
                def raise_for_status(self): return self
                def json(self): return {'observed': path}
            return Response()
    monkeypatch.setattr(resources.httpx, 'Client', BadClient)
    value = resources.runtime_metadata()
    assert value['ps'] is None
    assert value['version'] == {'observed': '/api/version'}
    assert value['errors']

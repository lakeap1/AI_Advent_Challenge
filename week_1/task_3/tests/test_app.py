from types import SimpleNamespace
import json
import pytest
from bridge import optimum, check_route
from app import create_app, TASK, PLAIN_TEXT

GOOD = [[1,3],[1],[8,12],[3],[1,3],[1],[1,6]]

def test_optimum_and_reconstructed_route():
    result = optimum()
    assert result['minutes'] == 29
    assert check_route(result['moves'], 29)['status'] == 'optimal'

def test_wrong_total_is_not_correct():
    result = check_route(GOOD, 28)
    assert result['status'] == 'invalid'
    assert result['minutes'] == 29

@pytest.mark.parametrize('moves', [[], [[1,3,6]], [[1,1]], [[99]], [[True]], [[1,3],[6]], [[1,3],[1]]])
def test_rejects_illegal_or_incomplete_routes(moves):
    assert check_route(moves, 29)['status'] == 'invalid'

def test_valid_but_slow_route():
    result = check_route([[1,12],[1],[1,8],[1],[1,6],[1],[1,3]], 32)
    assert result['status'] == 'suboptimal'
    assert result['minutes'] == 32

class FakeClient:
    def __init__(self, answers):
        self.answers = iter(answers)
        self.calls = []
        self.responses = self
    def create(self, **kwargs):
        self.calls.append(kwargs)
        answer = next(self.answers)
        return SimpleNamespace(output_text=answer, status='completed', id='fake-response', model='test', usage=SimpleNamespace(model_dump=lambda: {'input_tokens': 10, 'output_tokens': 20, 'total_tokens': 30}))

def test_direct_only_has_shared_format_instruction_and_persists(tmp_path):
    fake = FakeClient(['raw answer'])
    api = create_app(fake, tmp_path).test_client()
    response = api.post('/api/run/direct')
    assert response.status_code == 200
    assert fake.calls[0]['input'] == TASK
    assert fake.calls[0]['instructions'] == PLAIN_TEXT
    result = response.json
    assert result['answer'] == 'raw answer'
    assert json.loads((tmp_path / (result['id'] + '.json')).read_text(encoding='utf-8'))['answer'] == 'raw answer'

def test_generated_prompt_is_used_without_previous_answer(tmp_path):
    fake = FakeClient(['GENERATED PROMPT', 'solution'])
    result = create_app(fake, tmp_path).test_client().post('/api/run/meta').json
    assert len(fake.calls) == 2
    assert 'GENERATED PROMPT' in fake.calls[1]['input']
    assert TASK in fake.calls[1]['input']
    assert all('previous_response_id' not in call for call in fake.calls)
    assert result['tokens'] == 60
    assert result['answer'] == 'solution'

def test_step_and_experts_are_distinct(tmp_path):
    fake = FakeClient(['step', 'experts'])
    api = create_app(fake, tmp_path).test_client()
    api.post('/api/run/step')
    api.post('/api/run/experts')
    assert fake.calls[0]['input'] == TASK + '\n\nРешай пошагово.'
    assert fake.calls[0]['instructions'] == PLAIN_TEXT
    assert fake.calls[1]['input'] == TASK
    assert all(role in fake.calls[1]['instructions'] for role in ['Математик', 'Планировщик', 'Скептик'])

def test_check_and_reload_preserve_raw_answer(tmp_path):
    api = create_app(FakeClient(['original']), tmp_path).test_client()
    result = api.post('/api/run/direct').json
    response = api.post('/api/check/' + result['id'], json={'reviews':[{'label':'Ответ', 'moves':GOOD, 'claimed':29, 'note':'перенос из ответа'}]})
    assert response.status_code == 200
    assert response.json['reviews'][0]['check']['status'] == 'optimal'
    assert api.get('/api/latest').json['direct']['answer'] == 'original'

@pytest.mark.parametrize('payload', [None, [], {}, {'reviews':[{'moves':'bad','claimed':29}]}, {'reviews':[{'moves':GOOD,'claimed':True}]}])
def test_bad_review_is_client_error(tmp_path, payload):
    api = create_app(FakeClient(['original']), tmp_path).test_client()
    result = api.post('/api/run/direct').json
    assert api.post('/api/check/' + result['id'], json=payload).status_code == 400

def test_empty_response_not_saved_as_success(tmp_path):
    api = create_app(FakeClient(['  ']), tmp_path).test_client()
    assert api.post('/api/run/direct').status_code == 502
    assert list(tmp_path.glob('*.json')) == []

def test_unknown_mode_does_not_call_api(tmp_path):
    fake = FakeClient([])
    assert create_app(fake, tmp_path).test_client().post('/api/run/unknown').status_code == 404
    assert not fake.calls


def test_meta_second_failure_preserves_first_paid_call(tmp_path):
    from openai import APIConnectionError
    import httpx
    class FailingSecond(FakeClient):
        def create(self, **kwargs):
            if self.calls:
                raise APIConnectionError(request=httpx.Request('POST', 'https://api.openai.com/v1/responses'))
            return super().create(**kwargs)
    api = create_app(FailingSecond(['generated prompt']), tmp_path).test_client()
    assert api.post('/api/run/meta').status_code == 502
    failures = list((tmp_path / 'failed').glob('*.json'))
    assert len(failures) == 1
    failure = json.loads(failures[0].read_text(encoding='utf-8'))
    assert failure['status'] == 'failed'
    assert failure['calls'][0]['answer'] == 'generated prompt'
    assert failure['tokens'] == 30
    assert api.get('/api/latest').json == {}


def test_all_five_requests_use_high_reasoning(tmp_path):
    fake = FakeClient(['direct', 'step', 'prompt', 'meta', 'experts'])
    api = create_app(fake, tmp_path).test_client()
    for mode in ['direct', 'step', 'meta', 'experts']:
        response = api.post('/api/run/' + mode)
        assert response.status_code == 200
        assert all(call['request']['reasoning'] == {'effort': 'high'} for call in response.json['calls'])
    assert len(fake.calls) == 5
    assert all(call['reasoning'] == {'effort': 'high'} for call in fake.calls)

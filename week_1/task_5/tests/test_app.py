import json
from types import SimpleNamespace
import httpx
import pytest
from openai import AuthenticationError, BadRequestError, RateLimitError
from app import EXAMPLE_PROMPT, INSTRUCTIONS, MODELS, cost_usd, create_app, summarize

class Data(dict):
    def model_dump(self):
        return dict(self)

def usage(incoming=1000, outgoing=1000, cached=0, reasoning=800):
    return Data(input_tokens=incoming, output_tokens=outgoing, total_tokens=incoming+outgoing,
                input_tokens_details={"cached_tokens":cached},
                output_tokens_details={"reasoning_tokens":reasoning})

class FakeClient:
    def __init__(self, failure=None, status="completed", answer="Проверяемый ответ", tokens=True):
        self.calls=[]
        self.responses=self
        self.failure,self.status,self.answer,self.tokens=failure,status,answer,tokens
    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.failure and kwargs["model"].endswith("terra"):
            raise self.failure
        return SimpleNamespace(output_text=self.answer,status=self.status,model=kwargs["model"],
            usage=usage() if self.tokens else None,id="test-only",service_tier="default",
            incomplete_details=Data(reason="max_output_tokens") if self.status!="completed" else None)

def post(client, repeats=3, prompt=EXAMPLE_PROMPT, buffered=True):
    return client.post('/api/compare',json={"prompt":prompt,"repeats":repeats},buffered=buffered)

def events(response):
    return [json.loads(line) for line in response.get_data(as_text=True).splitlines()]

def test_identical_requests_and_rotated_order(tmp_path):
    fake=FakeClient()
    response=post(create_app(fake,tmp_path).test_client())
    assert response.status_code==200
    assert [c["model"].split('-')[-1] for c in fake.calls]==[
        'luna','terra','sol','terra','sol','luna','sol','luna','terra']
    params=[{k:v for k,v in c.items() if k!="model"} for c in fake.calls]
    assert all(c==params[0] for c in params)
    assert params[0]["input"]==EXAMPLE_PROMPT
    assert params[0]["instructions"]==INSTRUCTIONS
    assert "без Markdown" in INSTRUCTIONS
    assert params[0]["reasoning"]=={"effort":"medium"}
    assert params[0]["extra_body"]=={"prompt_cache_options":{"mode":"explicit"}}
    assert params[0]["max_output_tokens"]==6000
    assert params[0]["service_tier"]=="default"
    assert params[0]["store"] is False
    run=events(response)[-1]["run"]
    assert run["complete"] and len(run["results"])==9
    assert all(row["completed"]==3 for row in run["summary"])
    assert json.loads(next(tmp_path.glob('*.json')).read_text('utf-8'))==run

def test_cost_counts_reasoning_once_and_cached_rate():
    assert cost_usd(usage(),MODELS[0])==pytest.approx(.0014)
    assert cost_usd(usage(cached=500),MODELS[0])==pytest.approx(.00131)
    assert cost_usd(usage(),MODELS[1])==pytest.approx(.014)
    assert cost_usd(usage(),MODELS[2])==pytest.approx(.024)
    assert cost_usd(usage(reasoning=0),MODELS[2])==cost_usd(usage(reasoning=800),MODELS[2])

@pytest.mark.parametrize('tokens',[None,{},{"input_tokens":1,"output_tokens":2},usage(cached=1001),usage(incoming=-1)])
def test_unknown_usage_is_not_zero(tokens):
    assert cost_usd(tokens,MODELS[0]) is None

@pytest.mark.parametrize('payload',[[],None,{},{"prompt":" "},{"prompt":"x"*4001},{"prompt":"x","repeats":True},{"prompt":"x","repeats":9}])
def test_invalid_input_never_calls_api(tmp_path,payload):
    fake=FakeClient()
    response=create_app(fake,tmp_path).test_client().post('/api/compare',json=payload)
    assert response.status_code==400
    assert fake.calls==[]

@pytest.mark.parametrize('error_type,status',[(AuthenticationError,401),(RateLimitError,429),(BadRequestError,400)])
def test_partial_failure_retains_answers_without_leaking_errors(tmp_path,error_type,status):
    error=error_type("secret-provider-body",response=httpx.Response(status,request=httpx.Request('POST','https://api.openai.com/v1/responses')),body=None)
    response=post(create_app(FakeClient(failure=error),tmp_path).test_client(),1)
    run=events(response)[-1]["run"]
    assert [r["status"] for r in run["results"]]==['completed','error','completed']
    assert "secret-provider-body" not in response.get_data(as_text=True)
    assert run["summary"][1]["median_seconds"] is None
    assert run["summary"][1]["total_cost_usd"] is None

@pytest.mark.parametrize('status,answer',[('incomplete','Часть ответа'),('completed','')])
def test_unfinished_and_empty_excluded_but_usage_charged(tmp_path,status,answer):
    run=events(post(create_app(FakeClient(status=status,answer=answer),tmp_path).test_client(),1))[-1]["run"]
    assert run["results"][0]["answer"]==answer
    assert run["summary"][0]["completed"]==0
    assert run["summary"][0]["total_cost_usd"]==pytest.approx(.0014)

def test_missing_usage_answer_preserved(tmp_path):
    run=events(post(create_app(FakeClient(tokens=False),tmp_path).test_client(),1))[-1]["run"]
    assert run["results"][0]["answer"]=="Проверяемый ответ"
    assert run["summary"][0]["median_tokens"] is None
    assert run["summary"][0]["total_cost_usd"] is None

def test_busy_and_disconnect_release_lock(tmp_path):
    client=create_app(FakeClient(),tmp_path).test_client()
    response=post(client,1,buffered=False)
    assert post(client,1).status_code==409
    response.close()
    assert post(client,1).status_code==200

def test_save_failure_visible(tmp_path):
    target=tmp_path/'file'
    target.write_text('not a directory')
    run=events(post(create_app(FakeClient(),target).test_client(),1))[-1]["run"]
    assert "save_warning" in run and len(run["results"])==3

def test_medians_exclude_failures_but_cost_includes_usage():
    rows=[dict(requested_model=MODELS[0]['id'],status=status,seconds=seconds,usage=usage(incoming=100,outgoing=out),cost_usd=.01)
        for status,seconds,out in [('completed',1,100),('completed',9,300),('incomplete',100,6000)]]
    result=summarize(rows)[0]
    assert result['median_seconds']==5 and result['median_tokens']==300
    assert result['total_cost_usd']==.03

def test_example_optimum_by_exhaustive_enumeration():
    weights=dict(zip('ABCDEFGH',[3,4,2,3,5,2,4,3]))
    values=dict(zip('ABCDEFGH',[5,8,4,7,10,4,9,8]))
    dependencies={'B':'A','C':'B','D':'A','E':'A','F':'A','H':'D'}
    feasible=[]
    for mask in range(256):
        chosen={c for i,c in enumerate(weights) if mask&(1<<i)}
        if any(c in chosen and dep not in chosen for c,dep in dependencies.items()): continue
        if {'G','H'} <= chosen: continue
        hours=sum(weights[c] for c in chosen)
        if hours<=14: feasible.append((sum(values[c] for c in chosen),hours,''.join(sorted(chosen))))
    best=max(row[0] for row in feasible)
    winners=[row for row in feasible if row[0]==best]
    assert winners==[(30,14,'ADEH')]



def test_latest_skips_partial_and_malformed_runs(tmp_path):
    client=create_app(FakeClient(),tmp_path).test_client()
    assert client.get('/api/latest').status_code==404
    run=events(post(client,1))[-1]['run']
    (tmp_path/'partial.json').write_text('{"complete":false}')
    (tmp_path/'broken.json').write_text('not json')
    response=client.get('/api/latest')
    assert response.status_code==200 and response.json==run

def test_missing_key_does_not_start_stream(tmp_path,monkeypatch):
    monkeypatch.setattr('app.load_dotenv',lambda *args:None)
    monkeypatch.delenv('OPENAI_API_KEY',raising=False)
    response=post(create_app(results_dir=tmp_path).test_client(),1)
    assert response.status_code==503

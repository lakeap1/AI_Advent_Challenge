"""HTTP proof of the isolated, durable main-chat evaluation boundary."""

import json
from copy import deepcopy
from decimal import Decimal
import os
import subprocess
import sys

import pytest

from app import create_app
from agent.transport import ResponsesTransport, TransportError
from test_chat_rag import Fake, response
from test_rag import Embedder, indexed


def identity(state):
    return {
        'profile_id': state['personalization']['selected_id'],
        'task_id': state['workspace']['active_dialogue']['task_id'],
        'dialogue_id': state['workspace']['active_dialogue']['id'],
        'branch_id': state['active_branch'],
    }


def test_http_pair_uses_fresh_same_base_agent_and_durable_receipts(tmp_path, indexed, monkeypatch):
    from rag import chat

    root, index_data = indexed
    original = chat.read_index
    monkeypatch.setattr(chat, 'read_index', lambda _root, _data, config: original(root, index_data, config))
    embedder = Embedder()
    monkeypatch.setattr(chat, 'OpenAIEmbedder', lambda _config: embedder)
    fake = Fake([response('Plain answer.'), response('{"operations": []}'),
                 response('RAG answer [S1].'), response('{"operations": []}')])
    app = create_app(data_dir=tmp_path / 'chat', transport=fake)
    client = app.test_client()
    owner = identity(client.get('/api/state').json)
    started = client.post('/api/rag/chat/start', json=owner)
    assert started.status_code == 200, started.json
    run_id = started.json['run']['id']
    from rag.chat_evaluation import ChatEvaluationService
    gold = ChatEvaluationService.questions()[0]
    pair = client.post('/api/rag/chat/question', json={**owner, 'run_id': run_id,
        'question_id': gold['id'], 'prompt': gold['question']})
    assert pair.status_code == 200, pair.json
    result = pair.json['run']['questions'][0]['pair']
    assert result['plain']['text'] == 'Plain answer.'
    assert result['rag']['text'] == 'RAG answer [S1].'
    assert result['plain']['initial_context_sha256'] == result['rag']['initial_context_sha256']
    generation = [call for call in fake.calls if 'TASK_RESPONSE_JSON' in call['instructions']]
    assert len(generation) == 2
    assert generation[0]['input'][-1] == generation[1]['input'][-1]
    assert all(call['tools'] == [] and call['tool_choice'] == 'none' for call in generation)
    assert 'expected_facts' not in json.dumps(generation, ensure_ascii=False)
    assert 'Plain answer.' not in json.dumps(generation[1], ensure_ascii=False)
    assert result['plain']['summary']['api_requests'] == 2
    assert result['rag']['summary']['api_requests'] == 3
    assert pair.json['state']['summary']['api_requests'] == 5
    assert len(pair.json['state']['chat_evaluation_requests']) == 5
    assert len(fake.calls) == 4
    repeated = client.post('/api/rag/chat/question', json={**owner, 'run_id': run_id,
        'question_id': gold['id'], 'prompt': gold['question']})
    assert repeated.status_code == 200 and len(fake.calls) == 4
    app.extensions['workspace'].close()


def test_default_transport_runs_complete_pair_through_responses_boundary(tmp_path,indexed,monkeypatch):
    from rag import chat
    from rag.chat_evaluation import ChatEvaluationService

    root,index_data=indexed
    original=chat.read_index
    monkeypatch.setattr(chat,'read_index',lambda _root,_data,cfg:original(root,index_data,cfg))
    monkeypatch.setattr(chat,'OpenAIEmbedder',lambda _config:Embedder())
    provider=Fake([response('Plain answer.'),response('{"operations": []}'),
        response('RAG answer [S1].'),response('{"operations": []}')])
    monkeypatch.setattr(ResponsesTransport,'create',
        lambda self,payload,timeout:provider.create(payload,timeout))

    app=create_app(data_dir=tmp_path/'chat')
    client=app.test_client()
    owner=identity(client.get('/api/state').json)
    run_id=client.post('/api/rag/chat/start',json=owner).json['run']['id']
    q=ChatEvaluationService.questions()[0]
    answer=client.post('/api/rag/chat/question',json={**owner,'run_id':run_id,
        'question_id':q['id'],'prompt':q['question']})
    assert answer.status_code==200
    pair=answer.json['run']['questions'][0]['pair']
    assert pair['plain']['status']==pair['rag']['status']=='ok'
    assert pair['plain']['text']=='Plain answer.'
    assert pair['rag']['text']=='RAG answer [S1].'
    assert len(provider.calls)==4
    assert pair['plain']['payloads'][0]['payload']==provider.calls[0]
    assert pair['rag']['payloads'][0]['payload']==provider.calls[2]
    assert answer.json['state']['summary']['api_requests']==5
    app.extensions['workspace'].close()


def test_default_transport_preflight_failure_is_not_billed_or_interrupted(tmp_path,monkeypatch):
    from rag.chat_evaluation import ChatEvaluationService

    def no_key(self,payload,timeout):
        raise TransportError('not_configured',request_started=False)

    monkeypatch.setattr(ResponsesTransport,'create',no_key)
    app=create_app(data_dir=tmp_path/'chat')
    client=app.test_client()
    owner=identity(client.get('/api/state').json)
    run_id=client.post('/api/rag/chat/start',json=owner).json['run']['id']
    q=ChatEvaluationService.questions()[0]
    answer=client.post('/api/rag/chat/question',json={**owner,'run_id':run_id,
        'question_id':q['id'],'prompt':q['question']})
    assert answer.status_code==200
    pair=answer.json['run']['questions'][0]['pair']
    assert pair['plain']['code']=='not_configured'
    assert pair['plain']['requests'][-1]['usage_status']=='not_requested'
    assert pair['rag'] is None
    assert answer.json['state']['summary']['api_requests']==0
    assert answer.json['state']['summary']['unknown_cost_requests']==0
    app.extensions['workspace'].close()


def test_owner_and_exact_prompt_rejected_without_paid_call(tmp_path):
    fake = Fake()
    app = create_app(data_dir=tmp_path, transport=fake)
    client = app.test_client()
    owner = identity(client.get('/api/state').json)
    run_id = client.post('/api/rag/chat/start', json=owner).json['run']['id']
    from rag.chat_evaluation import ChatEvaluationService
    q = ChatEvaluationService.questions()[0]
    wrong = client.post('/api/rag/chat/question', json={**owner, 'branch_id': owner['branch_id']+1,
        'run_id': run_id, 'question_id': q['id'], 'prompt': q['question']})
    assert wrong.status_code == 400
    changed = client.post('/api/rag/chat/question', json={**owner,
        'run_id': run_id, 'question_id': q['id'], 'prompt': q['question']+' extra'})
    assert changed.status_code == 400
    assert fake.calls == []
    assert client.get('/api/state').json['chat_evaluations'][0]['questions'][0]['pair']['plain'] is None
    app.extensions['workspace'].close()


@pytest.mark.parametrize('usage_present', [True, False])
def test_plain_output_failure_suppresses_rag_and_keeps_cost_completeness(tmp_path, usage_present):
    class Failed(Fake):
        def create(self, payload, timeout):
            self.calls.append(payload)
            result = {**response('ignored'), 'status': 'failed'}
            if not usage_present:
                result.pop('usage')
            return result

    fake = Failed()
    app = create_app(data_dir=tmp_path, transport=fake)
    client = app.test_client()
    owner = identity(client.get('/api/state').json)
    run_id = client.post('/api/rag/chat/start', json=owner).json['run']['id']
    from rag.chat_evaluation import ChatEvaluationService
    q = ChatEvaluationService.questions()[0]
    pair = client.post('/api/rag/chat/question', json={**owner,'run_id':run_id,
        'question_id':q['id'],'prompt':q['question']})
    assert pair.status_code == 200
    question = pair.json['run']['questions'][0]
    assert question['status'] == 'failed'
    assert question['pair']['rag'] is None
    assert len(fake.calls) == 1
    assert pair.json['state']['summary']['api_requests'] == 1
    assert pair.json['state']['summary']['cost_complete'] is usage_present
    assert pair.json['state']['summary']['unknown_cost_requests'] == (0 if usage_present else 1)
    app.extensions['workspace'].close()


def test_missing_index_stops_rag_before_embedding_and_keeps_plain_cost(tmp_path):
    fake = Fake([response('Plain answer.'), response('{"operations": []}')])
    app = create_app(data_dir=tmp_path, transport=fake)
    client = app.test_client()
    owner = identity(client.get('/api/state').json)
    run_id = client.post('/api/rag/chat/start', json=owner).json['run']['id']
    from rag.chat_evaluation import ChatEvaluationService
    q = ChatEvaluationService.questions()[0]
    pair = client.post('/api/rag/chat/question', json={**owner,'run_id':run_id,
        'question_id':q['id'],'prompt':q['question']})
    assert pair.status_code == 200
    result = pair.json['run']['questions'][0]['pair']
    assert result['plain']['status'] == 'ok'
    assert result['rag']['code'] == 'invalid_index'
    assert len(fake.calls) == 2
    assert pair.json['state']['summary']['api_requests'] == 2
    app.extensions['workspace'].close()


def test_interrupted_question_remains_idempotent_after_reopen(tmp_path):
    class Interrupt(Fake):
        def create(self,payload,timeout):
            self.calls.append(payload)
            raise KeyboardInterrupt()

    fake = Interrupt()
    app = create_app(data_dir=tmp_path,transport=fake)
    client = app.test_client()
    owner = identity(client.get('/api/state').json)
    run_id = client.post('/api/rag/chat/start',json=owner).json['run']['id']
    from rag.chat_evaluation import ChatEvaluationService
    q = ChatEvaluationService.questions()[0]
    message = {**owner,'run_id':run_id,'question_id':q['id'],'prompt':q['question']}
    with pytest.raises(KeyboardInterrupt):
        client.post('/api/rag/chat/question',json=message)
    app.extensions['workspace'].close()
    reopened = create_app(data_dir=tmp_path,transport=Fake())
    state = reopened.test_client().get('/api/state').json
    assert state['chat_evaluations'][0]['questions'][0]['status']=='interrupted'
    assert state['summary']['unknown_cost_requests']==1
    assert state['summary']['api_requests']==1
    duplicate = reopened.test_client().post('/api/rag/chat/question',json=message)
    assert duplicate.json['run']['questions'][0]['status']=='interrupted'
    assert duplicate.json['state']['summary']['api_requests']==1
    reopened.extensions['workspace'].close()


def test_interrupted_rag_restores_embedding_and_pending_generation_cost(tmp_path,indexed,monkeypatch):
    from rag import chat
    from rag.chat_evaluation import ChatEvaluationService

    root,index_data=indexed
    original=chat.read_index
    monkeypatch.setattr(chat,'read_index',lambda _root,_data,cfg:original(root,index_data,cfg))
    monkeypatch.setattr(chat,'OpenAIEmbedder',lambda _config:Embedder())

    class InterruptRag(Fake):
        def create(self,payload,timeout):
            self.calls.append(payload)
            if len(self.calls)==3:
                raise KeyboardInterrupt()
            reply=self.replies.pop(0)
            if 'TASK_RESPONSE_JSON' in payload['instructions']:
                state_text=payload['instructions'].split('TASK_STATE —',1)[1].split('TASK_RESPONSE_JSON',1)[0]
                state=json.loads(state_text[state_text.index('{'):].strip())
                reply=response(json.dumps(dict(answer=reply['output'][0]['content'][0]['text'],
                    event='stay',evidence='',**{key:state[key] for key in
                    ('goal','current_step','expected_action','notes','plan')}),ensure_ascii=False))
            return reply

    transport=InterruptRag([response('Plain answer.'),response('{"operations": []}')])
    app=create_app(data_dir=tmp_path/'chat',transport=transport)
    client=app.test_client()
    owner=identity(client.get('/api/state').json)
    run_id=client.post('/api/rag/chat/start',json=owner).json['run']['id']
    q=ChatEvaluationService.questions()[0]
    request={**owner,'run_id':run_id,'question_id':q['id'],'prompt':q['question']}
    with pytest.raises(KeyboardInterrupt):
        client.post('/api/rag/chat/question',json=request)
    app.extensions['workspace'].close()

    no_retry=Fake()
    reopened=create_app(data_dir=tmp_path/'chat',transport=no_retry)
    state=reopened.test_client().get('/api/state').json
    pair=state['chat_evaluations'][0]['questions'][0]['pair']
    assert state['chat_evaluations'][0]['questions'][0]['status']=='interrupted'
    assert pair['plain']['status']=='ok'
    assert pair['rag']['status']=='interrupted'
    assert state['summary']['api_requests']==4
    assert state['summary']['unknown_cost_requests']==1
    assert Decimal(state['summary']['known_cost_usd'])==Decimal('0.0000301')
    assert len(state['chat_evaluation_requests'])==4
    assert next(r for r in state['chat_evaluation_requests'] if
        r['metadata']['underlying_kind']=='query_embedding')['usage']['total_tokens']==5
    duplicate=reopened.test_client().post('/api/rag/chat/question',json=request)
    assert duplicate.status_code==200
    assert duplicate.json['state']['summary']['api_requests']==4
    assert no_retry.calls==[]
    reopened.extensions['workspace'].close()


def test_hidden_branch_receipts_keep_cost_but_redact_content_and_deny_trace(tmp_path):
    from rag.chat_evaluation import ChatEvaluationService

    class Failed(Fake):
        def create(self,payload,timeout):
            self.calls.append(payload)
            return {**response('HIDDEN_ANSWER'), 'status':'failed'}

    app=create_app(data_dir=tmp_path,transport=Failed())
    client=app.test_client()
    initial=client.get('/api/state').json
    changed=client.post('/api/dialogue',json={'name':'Benchmark','task_id':
        initial['workspace']['active_dialogue']['task_id'],'mode':'branching'})
    owner=identity(changed.json['state'])
    run_id=client.post('/api/rag/chat/start',json=owner).json['run']['id']
    q=ChatEvaluationService.questions()[0]
    reply=client.post('/api/rag/chat/question',json={**owner,'run_id':run_id,
        'question_id':q['id'],'prompt':q['question']})
    receipt_id=reply.json['state']['chat_evaluation_requests'][0]['id']
    assert client.get('/api/rag/chat/trace/'+receipt_id).status_code==200
    cp=client.post('/api/checkpoint',json={'name':'Before fork'})
    branch=client.post('/api/branch',json={'checkpoint_id':cp.json['state']['checkpoints'][-1]['id'],
        'name':'Other branch'})
    assert branch.status_code==200
    state=branch.json['state']
    assert state['chat_evaluations']==[]
    assert state['summary']['api_requests']==1
    hidden=state['chat_evaluation_requests']
    assert len(hidden)==1 and hidden[0]['id']==receipt_id
    assert hidden[0]['metadata']['redacted'] is True
    assert 'context' not in hidden[0]['metadata']
    assert q['question'] not in json.dumps(hidden,ensure_ascii=False)
    assert 'HIDDEN_ANSWER' not in json.dumps(hidden,ensure_ascii=False)
    assert 'TASK_RESPONSE_JSON' not in json.dumps(hidden,ensure_ascii=False)
    assert client.get('/api/rag/chat/trace/'+receipt_id).status_code==404
    app.extensions['workspace'].close()


def test_full_assessment_rejects_stale_or_bad_sources_and_derives_totals(tmp_path,indexed,monkeypatch):
    from rag import chat
    from rag.chat_evaluation import ChatEvaluationService

    root,index_data=indexed
    original=chat.read_index
    monkeypatch.setattr(chat,'read_index',lambda _root,_data,cfg:original(root,index_data,cfg))
    monkeypatch.setattr(chat,'OpenAIEmbedder',lambda _config:Embedder())
    fake=Fake([item for _ in range(20) for item in
        (response('An answer [S1].'),response('{"operations": []}'))])
    app=create_app(data_dir=tmp_path / 'chat',transport=fake)
    client=app.test_client()
    owner=identity(client.get('/api/state').json)
    run_id=client.post('/api/rag/chat/start',json=owner).json['run']['id']
    for q in ChatEvaluationService.questions():
        answer=client.post('/api/rag/chat/question',json={**owner,'run_id':run_id,
            'question_id':q['id'],'prompt':q['question']})
        assert answer.status_code==200 and answer.json['run']['questions'][int(q['id'][1:])-1]['status']=='complete'
    service=app.extensions['chat_evaluation_service']
    saved=service.export(run_id,profile_id=owner['profile_id'])
    report=dict(run_id=run_id,evidence_sha256=saved['evidence_sha256'],
        reviewer=dict(kind='independent_agent',profile='reviewer',model='fixture-review',reasoning_effort='high'),
        questions=[])
    for q in saved['questions']:
        last=q['id']=='q10'
        report['questions'].append(dict(id=q['id'],facts=[dict(fact_index=i,plain_correct=True,
            plain_reason='Verified against corpus.',rag_correct=True,rag_supported=True,
            rag_source_labels=['S1'],rag_reason='Exact chunk checked.')
            for i,_ in enumerate(q['expected_facts'])],
            plain=dict(full_answer=True,unsupported_claims=[],abstained=last,reason='All facts.'),
            rag=dict(full_answer=True,unsupported_claims=[],abstained=last,reason='All facts and support.'),
            context_sufficient=True,context_reason='Exact chunk checked.',conclusion='Both covered.'))
    stale=deepcopy(report)
    stale['evidence_sha256']='0'*64
    with pytest.raises(ValueError):service.apply_assessment(stale)
    missing=deepcopy(report)
    missing['questions'][0]['facts'].pop()
    with pytest.raises(ValueError):service.apply_assessment(missing)
    unknown=deepcopy(report)
    unknown['questions'][0]['facts'][0]['rag_source_labels']=['S99']
    with pytest.raises(ValueError):service.apply_assessment(unknown)
    unsupported=deepcopy(report)
    unsupported['questions'][0]['facts'][0]['rag_source_labels']=[]
    with pytest.raises(ValueError):service.apply_assessment(unsupported)
    assert service.export(run_id)['assessment_status']=='pending'
    cli=[sys.executable,'-B','-m','rag.chat_evaluation_cli','--data-dir',str(tmp_path/'chat'),
         '--profile-id',str(owner['profile_id'])]
    export_path=tmp_path/'export.json'
    exported=subprocess.run([*cli,'export',run_id,str(export_path)],
        capture_output=True,text=True,env={**os.environ,'PYTHONUTF8':'1'},check=True)
    assert exported.stdout.strip()==saved['evidence_sha256']
    assert json.loads(export_path.read_text(encoding='utf-8'))['id']==run_id
    report_path=tmp_path/'assessment.json'
    report_path.write_text(json.dumps(report,ensure_ascii=False),encoding='utf-8')
    imported=subprocess.run([*cli,'import',str(report_path)],
        capture_output=True,text=True,env={**os.environ,'PYTHONUTF8':'1'},check=True)
    assert imported.stdout.strip()==run_id+' reviewed'
    assessed=service.export(run_id)
    assert assessed['assessment_status']=='reviewed'
    assert assessed['totals']['plain_full_answers']==10
    assert assessed['totals']['rag_q10_abstained'] is True
    assert client.get('/api/state').json['summary']['api_requests']==50
    app.extensions['workspace'].close()

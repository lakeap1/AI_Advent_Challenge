"""Versioned evaluation at the durable production chat boundary."""
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import shutil

import pytest
from app import create_app
from test_chat_rag import Fake, response
from test_rag import Embedder, indexed


def identity(state):
    return {key: value for key, value in zip(
        ('profile_id', 'task_id', 'dialogue_id', 'branch_id'),
        (state['personalization']['selected_id'],
         state['workspace']['active_dialogue']['task_id'],
         state['workspace']['active_dialogue']['id'], state['active_branch']))}


@pytest.fixture
def local_index(indexed, monkeypatch):
    from rag import chat
    root, index_data = indexed
    for app_data in (root.parent, root.parent / 'app'):
        app_data.mkdir(parents=True, exist_ok=True)
        shutil.copy2(index_data / 'indexing.sqlite3', app_data / 'indexing.sqlite3')
    original = chat.read_index
    monkeypatch.setattr(chat, 'read_index',
        lambda _root, actual_data, config: original(root, actual_data, config))
    embedder = Embedder()
    monkeypatch.setattr(chat, 'OpenAIEmbedder', lambda _config: embedder)
    return root, index_data, embedder


class FourModeTransport(Fake):
    def __init__(self, *, low_filter=False, bad_rewrite=False, unknown_rewrite=False,
                 bad_filter=False, unknown_filter=False, unknown_extraction=False,
                 unknown_answer=False):
        super().__init__()
        self.low_filter = low_filter
        self.bad_rewrite = bad_rewrite
        self.unknown_rewrite = unknown_rewrite
        self.bad_filter = bad_filter
        self.unknown_filter = unknown_filter
        self.unknown_extraction = unknown_extraction
        self.unknown_answer = unknown_answer

    def create(self, payload, timeout):
        instructions = payload['instructions']
        if 'Перепиши поисковый запрос' in instructions:
            reply = response(json.dumps({'query': '' if self.bad_rewrite else
                payload['input'][-1]['content']}, ensure_ascii=False))
            if self.unknown_rewrite:
                reply.pop('usage')
        elif 'Оцени полезность' in instructions:
            data = json.loads(payload['input'][-1]['content'])
            reply = response(json.dumps({'scores': {} if self.bad_filter else {
                candidate['chunk_id']: {
                 'score': 0 if self.low_filter else 3,
                 'reason': 'Fixture relevance decision.'}
                for candidate in data['candidates']}}))
            if self.unknown_filter:
                reply.pop('usage')
        elif 'TASK_RESPONSE_JSON' in instructions:
            reply = response('The answer is in source [S1].')
        else:
            reply = response('{"operations": []}')
            if self.unknown_extraction:
                reply.pop('usage')
        self.replies.append(reply)
        result = super().create(payload, timeout)
        if self.unknown_answer and 'TASK_RESPONSE_JSON' in instructions:
            result.pop('usage')
        return result


def start(client):
    from rag.chat_evaluation import ChatEvaluationService
    owner = identity(client.get('/api/state').json)
    run = client.post('/api/rag/chat/start', json=owner).json['run']
    return owner, run, ChatEvaluationService.questions()


def ask(client, owner, run, q):
    reply = client.post('/api/rag/chat/question', json={**owner, 'run_id': run['id'],
        'question_id': q['id'], 'prompt': q['question']})
    assert reply.status_code == 200, reply.json
    return next(item for item in reply.json['run']['questions'] if item['id'] == q['id'])


def historical_run(client, service, *, complete):
    """Persist a full four-mode run produced through the real evaluation path."""
    owner, run, questions = start(client)
    run['modes'] = ['rag', 'rewrite', 'filter', 'rewrite_filter']
    for q in run['questions']:
        q['pair'] = {mode: None for mode in run['modes']}
    service._save(run)
    if complete:
        for q in questions:
            assert ask(client, owner, run, q)['status'] == 'complete'
    run = service._get(run['id'])
    run['version'] = 2
    run['totals'] = service._totals(run)
    service._save(run)
    return owner, run, questions


def assessment_report(raw):
    report = {'version': raw['version'], 'run_id': raw['id'],
              'evidence_sha256': raw['evidence_sha256'],
              'reviewer': {'kind': 'independent_agent', 'profile': 'test-reviewer',
                           'model': 'fixture', 'reasoning_effort': 'high'},
              'questions': []}
    for q in raw['questions']:
        modes = {}
        for mode in raw['modes']:
            no_context = q['pair'][mode]['status'] == 'no_context'
            label = q['pair'][mode]['sources'][0]['label'] if not no_context else None
            is_q10 = q['id'] == 'q10'
            modes[mode] = {'facts': [{'fact_index': i, 'correct': True,
                'supported': not is_q10 and not no_context,
                'source_labels': [] if is_q10 or no_context else [label],
                'reason': 'Corpus slice checked.'}
                for i, _ in enumerate(q['expected_facts'])],
                'full_answer': not no_context or is_q10,
                'unsupported_claims': [], 'abstained': is_q10,
                'reason': 'Fixture verdict.', 'candidate_sufficient': True,
                'candidate_reason': 'Candidate present.',
                'context_sufficient': not no_context,
                'context_reason': 'Selected source present.'}
        report['questions'].append({'id': q['id'], 'modes': modes,
                                    'conclusion': 'Fixture comparison.'})
    return report


def test_new_run_freezes_two_modes_before_any_paid_call(tmp_path, local_index):
    fake = Fake()
    app = create_app(data_dir=tmp_path, transport=fake)
    owner, run, _ = start(app.test_client())
    assert run['version'] == 3
    assert run['modes'] == ['rag', 'filter']
    assert run['settings'] == {'top_k_before': 10, 'top_k_after': 2,
                               'relevance_threshold': 2, 'max_context_utf8_bytes': 6000}
    assert set(run['questions'][0]['pair']) == set(run['modes'])
    assert all(value is None for value in run['questions'][0]['pair'].values())
    assert run['owner'] == owner and run['assessment_status'] == 'pending'
    assert fake.calls == []
    app.extensions['workspace'].close()


def test_start_rejects_unpublished_index_before_api(tmp_path):
    fake = Fake()
    app = create_app(data_dir=tmp_path, transport=fake)
    client = app.test_client()
    assert (tmp_path / 'indexing.sqlite3').is_file()
    owner = identity(client.get('/api/state').json)
    result = client.post('/api/rag/chat/start', json=owner)
    assert result.status_code == 400
    assert fake.calls == []
    assert client.get('/api/state').json['chat_evaluations'] == []
    app.extensions['workspace'].close()


def test_start_rejects_missing_index_before_api(tmp_path, local_index):
    from rag.chat_evaluation import ChatEvaluationService
    fake = Fake()
    app = create_app(data_dir=tmp_path / 'app', transport=fake)
    workspace = app.extensions['workspace']
    service = ChatEvaluationService(tmp_path / 'missing', workspace=workspace, transport=fake)
    assert not (tmp_path / 'missing' / 'indexing.sqlite3').exists()
    with pytest.raises(ValueError):
        service.start(identity(workspace.state()))
    assert fake.calls == []
    workspace.close()


def test_start_rejects_stale_source_index_before_api(tmp_path, local_index):
    root, _, _ = local_index
    (root / 'alpha.md').write_text('# Alpha\nChanged before start.\n', encoding='utf-8')
    fake = Fake()
    app = create_app(data_dir=tmp_path, transport=fake)
    client = app.test_client()
    result = client.post('/api/rag/chat/start', json=identity(client.get('/api/state').json))
    assert result.status_code == 400 and fake.calls == []
    assert client.get('/api/state').json['chat_evaluations'] == []
    app.extensions['workspace'].close()


def test_question_rechecks_source_index_before_invariant_model_call(tmp_path, local_index):
    root, _, _ = local_index
    fake = FourModeTransport()
    app = create_app(data_dir=tmp_path, transport=fake)
    client = app.test_client()
    owner, run, questions = start(client)
    app.extensions['workspace'].memory.set_invariants(owner['task_id'], 0, ['Only grayscale.'])
    (root / 'alpha.md').write_text('# Alpha\nChanged after indexing.\n', encoding='utf-8')
    result = client.post('/api/rag/chat/question', json={**owner, 'run_id': run['id'],
        'question_id': questions[0]['id'], 'prompt': questions[0]['question']})
    assert result.status_code == 400
    assert fake.calls == []
    assert client.get('/api/state').json['chat_evaluations'][0]['questions'][0]['status'] == 'not_started'
    app.extensions['workspace'].close()


def test_runtime_tool_loop_drift_rejected_before_api(tmp_path, local_index, monkeypatch):
    from rag import chat_evaluation
    source_root = Path(chat_evaluation.__file__).resolve().parents[1]
    copied_root = tmp_path / 'runtime'
    for package in ('agent', 'rag', 'indexing', 'composition', 'orchestration',
                    'knowledge_server', 'digest_server'):
        for source in (source_root / package).rglob('*.py'):
            target = copied_root / source.relative_to(source_root)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    for source in source_root.glob('*.py'):
        if source.name != 'check_mcp.py':
            shutil.copy2(source, copied_root / source.name)
    (copied_root / 'evaluation').mkdir()
    shutil.copy2(source_root / 'evaluation' / 'questions.json',
                 copied_root / 'evaluation' / 'questions.json')
    monkeypatch.setattr(chat_evaluation, 'TASK_ROOT', copied_root)
    fake = FourModeTransport()
    app = create_app(data_dir=tmp_path / 'app', transport=fake)
    client = app.test_client()
    owner, run, questions = start(client)
    tool_loop = copied_root / 'agent' / 'tool_loop.py'
    tool_loop.write_text(tool_loop.read_text(encoding='utf-8') + '\n# changed after start\n',
                         encoding='utf-8')
    result = client.post('/api/rag/chat/question', json={**owner, 'run_id': run['id'],
        'question_id': questions[0]['id'], 'prompt': questions[0]['question']})
    assert result.status_code == 400
    assert fake.calls == []
    assert client.get('/api/state').json['chat_evaluations'][0]['questions'][0]['status'] == 'not_started'
    app.extensions['workspace'].close()


def test_two_modes_share_preinjection_intent_and_owner_state(tmp_path, local_index):
    fake = FourModeTransport()
    app = create_app(data_dir=tmp_path, transport=fake)
    client = app.test_client()
    owner, run, questions = start(client)
    q = ask(client, owner, run, questions[0])
    pair = q['pair']
    assert q['status'] == 'complete'
    assert all(pair[mode]['status'] == 'ok' for mode in run['modes'])
    assert len({pair[mode]['initial_context_sha256'] for mode in run['modes']}) == 1
    assert all(pair[mode]['base_generation_payload'] for mode in run['modes'])
    assert all(pair[mode]['base_memory_sha256'] == q['base_memory_sha256'] for mode in run['modes'])
    assert all(pair[mode]['retrieval']['original_query'] == questions[0]['question']
               for mode in run['modes'])
    assert all(pair[mode]['sources'] and pair[mode]['candidates'] for mode in run['modes'])
    assert all(sum(p['kind'] == 'answer' for p in pair[mode]['payloads']) == 1
               for mode in run['modes'])
    assert all(call['tools'] == [] and call['tool_choice'] == 'none' for call in fake.calls)
    assert not any('expected_facts' in json.dumps(call, ensure_ascii=False) for call in fake.calls)
    ids = [r['id'] for mode in run['modes'] for r in pair[mode]['requests']]
    assert len(ids) == len(set(ids))
    assert any(r['metadata']['underlying_kind'] == 'relevance_filter'
               for r in pair['filter']['requests'])
    cost = sum((Decimal(pair[mode]['summary']['known_cost_usd']) for mode in run['modes']), Decimal(0))
    assert Decimal(client.get('/api/state').json['summary']['known_cost_usd']) == cost
    count = len(fake.calls)
    assert ask(client, owner, run, questions[0])['pair'] == pair
    wrong = client.post('/api/rag/chat/question', json={**owner, 'branch_id': owner['branch_id'] + 1,
        'run_id': run['id'], 'question_id': questions[1]['id'], 'prompt': questions[1]['question']})
    assert wrong.status_code == 400 and len(fake.calls) == count
    app.extensions['workspace'].close()


def test_branching_dialogue_uses_its_actual_frozen_agent_config(tmp_path, local_index):
    app = create_app(data_dir=tmp_path, transport=FourModeTransport())
    client = app.test_client()
    state = client.get('/api/state').json
    created = client.post('/api/dialogue', json={'name': 'Evaluation branch',
        'task_id': state['workspace']['active_dialogue']['task_id'], 'mode': 'branching'})
    assert created.status_code == 200
    owner, run, questions = start(client)
    assert run['config_snapshot']['agent']['context_mode'] == 'branching'
    assert ask(client, owner, run, questions[0])['status'] == 'complete'
    app.extensions['workspace'].close()


def test_other_branch_keeps_cost_but_hides_evaluation_content(tmp_path, local_index):
    app = create_app(data_dir=tmp_path, transport=FourModeTransport())
    client = app.test_client()
    state = client.get('/api/state').json
    created = client.post('/api/dialogue', json={'name': 'Private evaluation',
        'task_id': state['workspace']['active_dialogue']['task_id'], 'mode': 'branching'})
    assert created.status_code == 200
    owner, run, questions = start(client)
    assert ask(client, owner, run, questions[0])['status'] == 'complete'
    before = client.get('/api/state').json
    receipt_id = before['chat_evaluation_requests'][0]['id']
    assert client.get('/api/rag/chat/trace/' + receipt_id).status_code == 200
    checkpoint = client.post('/api/checkpoint', json={'name': 'Before other branch'})
    branched = client.post('/api/branch', json={
        'checkpoint_id': checkpoint.json['state']['checkpoints'][-1]['id'],
        'name': 'Other branch'})
    assert branched.status_code == 200
    after = branched.json['state']
    assert after['chat_evaluations'] == []
    assert after['summary']['known_cost_usd'] == before['summary']['known_cost_usd']
    hidden = after['chat_evaluation_requests']
    assert hidden and all(r['metadata']['redacted'] for r in hidden)
    assert questions[0]['question'] not in json.dumps(hidden, ensure_ascii=False)
    assert client.get('/api/rag/chat/trace/' + receipt_id).status_code == 404
    app.extensions['workspace'].close()


def test_no_context_completes_without_fabricated_generation(tmp_path, local_index):
    app = create_app(data_dir=tmp_path, transport=FourModeTransport(low_filter=True))
    client = app.test_client()
    owner, run, questions = start(client)
    q = ask(client, owner, run, questions[0])
    assert q['status'] == 'complete'
    assert len({q['pair'][mode]['initial_context_sha256'] for mode in run['modes']}) == 1
    for mode in ('filter',):
        result = q['pair'][mode]
        assert result['status'] == 'no_context'
        assert result['sources'] == [] and result['context'] == ''
        assert result['base_generation_payload'] and not result['generation_requested']
        assert not any(p['kind'] == 'answer' for p in result['payloads'])
        parent = next(r for r in result['requests'] if r['metadata']['underlying_kind'] == 'answer')
        assert parent['usage_status'] == 'not_requested'
    assert q['pair']['rag']['status'] == 'ok'
    app.extensions['workspace'].close()


def test_paid_filter_rejection_survives_reload_with_unknown_cost(tmp_path, local_index):
    fake = FourModeTransport(bad_filter=True, unknown_filter=True)
    app = create_app(data_dir=tmp_path, transport=fake)
    client = app.test_client()
    owner, run, questions = start(client)
    q = ask(client, owner, run, questions[0])
    assert q['status'] == 'failed' and q['pair']['rag']['status'] == 'ok'
    assert q['pair']['filter']['status'] == 'rejected'
    filter_call = next(r for r in q['pair']['filter']['requests']
                       if r['metadata']['underlying_kind'] == 'relevance_filter')
    assert filter_call['cost_usd'] is None and filter_call['usage_status'] != 'not_requested'
    assert client.get('/api/state').json['summary']['cost_complete'] is False
    count = len(fake.calls)
    app.extensions['workspace'].close()
    reopened_fake = FourModeTransport()
    reopened = create_app(data_dir=tmp_path, transport=reopened_fake)
    restored = reopened.test_client().get('/api/state').json['chat_evaluations'][0]
    assert restored['questions'][0]['pair'] == q['pair']
    assert ask(reopened.test_client(), owner, run, questions[0])['status'] == 'failed'
    assert len(fake.calls) == count and reopened_fake.calls == []
    reopened.extensions['workspace'].close()


def test_interrupted_paid_stage_recovers_without_retry(tmp_path, local_index):
    class Interrupt(Fake):
        def create(self, payload, timeout):
            self.calls.append(payload)
            raise KeyboardInterrupt()

    fake = Interrupt()
    app = create_app(data_dir=tmp_path, transport=fake)
    client = app.test_client()
    owner, run, questions = start(client)
    with pytest.raises(KeyboardInterrupt):
        ask(client, owner, run, questions[0])
    app.extensions['workspace'].close()
    reopened_fake = FourModeTransport()
    reopened = create_app(data_dir=tmp_path, transport=reopened_fake)
    state = reopened.test_client().get('/api/state').json
    q = state['chat_evaluations'][0]['questions'][0]
    assert q['status'] == 'failed'
    assert q['pair']['rag']['status'] == 'interrupted'
    assert q['pair']['filter'] is None
    assert state['summary']['api_requests'] == 2
    assert state['summary']['unknown_cost_requests'] == 1
    assert state['summary']['cost_complete'] is False
    assert ask(reopened.test_client(), owner, run, questions[0])['status'] == 'failed'
    assert len(fake.calls) == 1 and reopened_fake.calls == []
    reopened.extensions['workspace'].close()


def test_unfinished_historical_run_rejects_new_calls_before_transport(tmp_path, local_index):
    fake = FourModeTransport()
    app = create_app(data_dir=tmp_path, transport=fake)
    client = app.test_client()
    service = app.extensions['chat_evaluation_service']
    owner, run, questions = historical_run(client, service, complete=False)
    result = client.post('/api/rag/chat/question', json={**owner, 'run_id': run['id'],
        'question_id': questions[0]['id'], 'prompt': questions[0]['question']})
    assert result.status_code == 400
    assert fake.calls == []
    assert service.export(run['id'])['questions'][0]['pair'] == run['questions'][0]['pair']
    app.extensions['workspace'].close()


def test_restart_does_not_rewrite_pending_historical_evidence(tmp_path, local_index):
    class Interrupt(Fake):
        def create(self, payload, timeout):
            self.calls.append(payload)
            raise KeyboardInterrupt()

    fake = Interrupt()
    app = create_app(data_dir=tmp_path, transport=fake)
    client = app.test_client()
    service = app.extensions['chat_evaluation_service']
    owner, run, questions = historical_run(client, service, complete=False)
    run['version'] = 3  # Produce a genuine interrupted four-mode record.
    service._save(run)
    with pytest.raises(KeyboardInterrupt):
        ask(client, owner, run, questions[0])
    interrupted = service._get(run['id'])
    interrupted['version'] = 2
    service._save(interrupted)
    before = service._get(run['id'])
    service.db.close()
    app.extensions['workspace'].close()

    reopened = create_app(data_dir=tmp_path, transport=FourModeTransport())
    restored = reopened.extensions['chat_evaluation_service']._get(run['id'])
    assert restored == before
    assert restored['questions'][0]['status'] == 'pending'
    reopened.extensions['workspace'].close()


def test_pending_historical_paid_receipts_project_without_rewriting_evidence(tmp_path, local_index):
    class InterruptFilter(FourModeTransport):
        def create(self, payload, timeout):
            if 'Оцени полезность' in payload['instructions']:
                self.calls.append(payload)
                raise KeyboardInterrupt()
            return super().create(payload, timeout)

    fake = InterruptFilter()
    app = create_app(data_dir=tmp_path, transport=fake)
    client = app.test_client()
    service = app.extensions['chat_evaluation_service']
    owner, run, questions = historical_run(client, service, complete=False)
    run['version'] = 3
    service._save(run)
    with pytest.raises(KeyboardInterrupt):
        ask(client, owner, run, questions[0])
    pending = service._get(run['id'])
    pending['version'] = 2
    service._save(pending)
    raw_before = service.db.execute('SELECT data_json FROM runs WHERE id=?',
        (run['id'],)).fetchone()['data_json']
    assert pending['questions'][0]['pair']['rag']['status'] == 'ok'
    assert pending['questions'][0]['pair']['filter'] == {'status': 'pending'}
    service.db.close()
    app.extensions['workspace'].close()
    ledgers = {path: path.read_bytes() for path in
        (tmp_path / 'chat-evaluations' / run['id'] / questions[0]['id']).rglob('*.sqlite3')}

    reopened = create_app(data_dir=tmp_path, transport=FourModeTransport())
    service = reopened.extensions['chat_evaluation_service']
    view = service.export(run['id'], profile_id=owner['profile_id'])
    state = reopened.test_client().get('/api/state').json
    second = service.export(run['id'], profile_id=owner['profile_id'])
    assert view['questions'][0]['pair'] == pending['questions'][0]['pair']
    assert second == view
    assert view['totals']['api_requests'] == 9, [
        (r['metadata']['underlying_kind'], r['usage_status'])
        for r in state['chat_evaluation_requests']]
    assert view['totals']['unknown_cost_requests'] == 1
    assert view['totals']['cost_complete'] is False
    assert view['totals']['modes']['filter']['api_requests'] == 2
    assert state['summary']['unknown_cost_requests'] == 1
    assert len(state['chat_evaluation_requests']) == 10
    assert state['chat_evaluations'][0]['totals']['unknown_cost_requests'] == 1
    assert service.db.execute('SELECT data_json FROM runs WHERE id=?',
        (run['id'],)).fetchone()['data_json'] == raw_before
    assert all(path.read_bytes() == before for path, before in ledgers.items())
    assert reopened.extensions['workspace'].transport.calls == []
    reopened.extensions['workspace'].close()


@pytest.mark.parametrize(('mode', 'unknown_stage', 'expected_paid_kinds'), [
    ('rag', 'embedding', ('query_embedding',)),
    ('filter', 'filter', ('query_embedding', 'relevance_filter')),
    ('rewrite', 'rewrite', ('query_rewrite',)),
    ('rewrite_filter', 'rewrite', ('query_rewrite',)),
])
def test_evaluation_stops_after_unknown_stage_before_next_paid_call(
        tmp_path, local_index, monkeypatch, mode, unknown_stage, expected_paid_kinds):
    from rag import chat

    fake = FourModeTransport(unknown_filter=unknown_stage == 'filter',
                             unknown_rewrite=unknown_stage == 'rewrite')
    if unknown_stage == 'embedding':
        class MissingUsageEmbedder(Embedder):
            def embed(self, texts):
                output = super().embed(texts)
                output.pop('usage')
                return output
        monkeypatch.setattr(chat, 'OpenAIEmbedder', lambda _config: MissingUsageEmbedder())
    app = create_app(data_dir=tmp_path, transport=fake)
    client = app.test_client()
    service = app.extensions['chat_evaluation_service']
    owner, run, questions = start(client)
    run['modes'] = ['rag', mode] if mode != 'rag' else ['rag', 'filter']
    for q in run['questions']:
        q['pair'] = {selected: None for selected in run['modes']}
    service._save(run)
    first = ask(client, owner, run, questions[0])
    assert first['status'] == 'failed'
    assert first['pair'][mode]['code'] == 'unknown_cost'
    assert first['pair'][mode]['summary']['unknown_cost_requests'] == 1
    assert tuple(r['metadata']['underlying_kind'] for r in first['pair'][mode]['requests']
                 if r['usage_status'] != 'not_requested') == expected_paid_kinds
    if unknown_stage == 'embedding':
        assert fake.calls == []
    assert next(q for q in service.export(run['id'])['questions']
                if q['id'] == questions[1]['id'])['status'] == 'not_started'
    app.extensions['workspace'].close()


def test_evaluation_stops_after_unknown_extraction_before_next_mode(
        tmp_path, local_index):
    fake = FourModeTransport(unknown_extraction=True)
    app = create_app(data_dir=tmp_path, transport=fake)
    client = app.test_client()
    owner, run, questions = start(client)
    first = ask(client, owner, run, questions[0])
    assert first['status'] == 'failed'
    assert first['pair']['rag']['code'] == 'extraction_failed'
    receipts = first['pair']['rag']['requests']
    assert [r['metadata']['underlying_kind'] for r in receipts
            if r['usage_status'] != 'not_requested'] == [
                'answer', 'query_embedding', 'extraction']
    assert next(r for r in receipts if r['metadata']['underlying_kind'] == 'extraction')['cost_usd'] is None
    assert first['pair']['rag']['summary']['unknown_cost_requests'] == 1
    assert len(fake.calls) == 2
    assert first['pair']['filter'] is None
    app.extensions['workspace'].close()


def test_evaluation_stops_after_unknown_generation_before_extraction_and_next_mode(
        tmp_path, local_index):
    fake = FourModeTransport(unknown_answer=True)
    app = create_app(data_dir=tmp_path, transport=fake)
    client = app.test_client()
    owner, run, questions = start(client)
    first = ask(client, owner, run, questions[0])
    assert first['status'] == 'failed'
    assert first['pair']['rag']['code'] == 'unknown_cost'
    assert first['pair']['rag']['summary']['unknown_cost_requests'] == 1
    assert [r['metadata']['underlying_kind'] for r in first['pair']['rag']['requests']
            if r['usage_status'] != 'not_requested'] == ['answer', 'query_embedding']
    assert first['pair']['filter'] is None
    assert len(fake.calls) == 1
    app.extensions['workspace'].close()


def test_unknown_cost_blocks_direct_next_question_before_and_after_restart(
        tmp_path, local_index):
    fake = FourModeTransport(unknown_filter=True)
    app = create_app(data_dir=tmp_path, transport=fake)
    client = app.test_client()
    service = app.extensions['chat_evaluation_service']
    owner, run, questions = start(client)
    first = ask(client, owner, run, questions[0])
    assert first['status'] == 'failed'
    assert first['pair']['filter']['summary']['unknown_cost_requests'] == 1
    run_bytes = service.db.execute('SELECT data_json FROM runs WHERE id=?',
        (run['id'],)).fetchone()['data_json']
    area = tmp_path / 'chat-evaluations' / run['id']
    ledger_bytes = {path: path.read_bytes() for path in area.rglob('*.sqlite3')}
    call_count = len(fake.calls)
    next_body = {**owner, 'run_id': run['id'],
                 'question_id': questions[1]['id'], 'prompt': questions[1]['question']}

    rejected = client.post('/api/rag/chat/question', json=next_body)
    assert rejected.status_code == 400
    assert 'неизвест' in rejected.json['text'].lower()
    assert ask(client, owner, run, questions[0])['status'] == 'failed'
    assert len(fake.calls) == call_count
    assert service._get(run['id'])['questions'][1]['status'] == 'not_started'
    assert service.db.execute('SELECT data_json FROM runs WHERE id=?',
        (run['id'],)).fetchone()['data_json'] == run_bytes
    assert {path: path.read_bytes() for path in area.rglob('*.sqlite3')} == ledger_bytes
    service.db.close()
    app.extensions['workspace'].close()

    reopened_fake = FourModeTransport()
    reopened = create_app(data_dir=tmp_path, transport=reopened_fake)
    reopened_client = reopened.test_client()
    rejected = reopened_client.post('/api/rag/chat/question', json=next_body)
    assert rejected.status_code == 400
    assert 'неизвест' in rejected.json['text'].lower()
    assert reopened_fake.calls == []
    reopened_service = reopened.extensions['chat_evaluation_service']
    assert reopened_service._get(run['id'])['questions'][1]['status'] == 'not_started'
    assert reopened_service.db.execute('SELECT data_json FROM runs WHERE id=?',
        (run['id'],)).fetchone()['data_json'] == run_bytes
    assert {path: path.read_bytes() for path in area.rglob('*.sqlite3')} == ledger_bytes
    reopened_service.db.close()
    reopened.extensions['workspace'].close()


def test_assessment_v2_rejects_stale_incomplete_and_unselected_sources(tmp_path, local_index):
    from rag.chat_evaluation_cli import main as evaluation_cli
    fake = FourModeTransport()
    app = create_app(data_dir=tmp_path, transport=fake)
    client = app.test_client()
    service = app.extensions['chat_evaluation_service']
    owner, run, questions = historical_run(client, service, complete=True)
    raw = service.export(run['id'], profile_id=owner['profile_id'])
    assert set(raw['totals']['modes']) == set(run['modes'])
    assert all(set(q['pair']) == set(run['modes']) for q in raw['questions'])
    assert any(q['pair']['rewrite']['requests'] for q in raw['questions'])
    receipt_ids = {request['id'] for q in raw['questions'] for mode in run['modes']
                   for request in q['pair'][mode]['requests']}
    state_ids = {request['id'] for request in client.get('/api/state').json['chat_evaluation_requests']}
    assert receipt_ids <= state_ids
    report = assessment_report(raw)
    for mutation in (
        lambda r: r.update(version=1),
        lambda r: r.update(evidence_sha256='0' * 64),
        lambda r: r['questions'][0]['modes'].pop('rewrite'),
        lambda r: r['questions'][0]['modes']['filter']['facts'].pop(),
        lambda r: r['questions'][0]['modes']['rag']['facts'][0].update(source_labels=['S99']),
        lambda r: r['questions'][0]['modes']['rag'].update(full_answer=False),
    ):
        invalid = deepcopy(report)
        mutation(invalid)
        with pytest.raises(ValueError):
            service.apply_assessment(invalid)
        assert service.export(run['id'])['assessment_status'] == 'pending'
    export_path = tmp_path / 'export.json'
    cli_args = ['--data-dir', str(tmp_path), '--profile-id', str(owner['profile_id'])]
    evaluation_cli([*cli_args, 'export', run['id'], str(export_path)])
    assert json.loads(export_path.read_text(encoding='utf-8'))['version'] == 2
    report_path = tmp_path / 'review.json'
    report_path.write_text(json.dumps(report, ensure_ascii=False), encoding='utf-8')
    evaluation_cli([*cli_args, 'import', str(report_path)])
    assessed = service.export(run['id'])
    assert assessed['assessment_status'] == 'reviewed'
    assert assessed['evidence_sha256'] == raw['evidence_sha256']
    assert assessed['totals']['modes']['rag']['full_answers'] == 10
    assert assessed['totals']['modes']['rewrite_filter']['q10_abstained'] is True
    assert assessed['totals']['paired']['rewrite_filter'] == {'gains': [], 'losses': []}
    assert service.export(run['id'])['evidence_sha256'] == raw['evidence_sha256']
    assert {r['id'] for r in service._receipts(service._get(run['id']))} == receipt_ids
    calls = len(fake.calls)
    assert ask(client, owner, run, questions[0])['status'] == 'complete'
    assert len(fake.calls) == calls
    app.extensions['workspace'].close()


def test_assessment_v3_binds_two_mode_evidence(tmp_path, local_index):
    app = create_app(data_dir=tmp_path, transport=FourModeTransport())
    client = app.test_client()
    owner, run, questions = start(client)
    for q in questions:
        assert ask(client, owner, run, q)['status'] == 'complete'
    service = app.extensions['chat_evaluation_service']
    raw = service.export(run['id'])
    assert set(raw['totals']['modes']) == {'rag', 'filter'}
    report = assessment_report(raw)
    wrong = deepcopy(report)
    wrong['questions'][0]['modes']['rewrite'] = wrong['questions'][0]['modes']['filter']
    with pytest.raises(ValueError):
        service.apply_assessment(wrong)
    assert service.export(run['id'])['assessment_status'] == 'pending'
    assessed = service.apply_assessment(report, profile_id=owner['profile_id'])
    assert assessed['assessment_status'] == 'reviewed'
    assert assessed['totals']['modes']['filter']['full_answers'] == 10
    assert set(assessed['totals']['paired']) == {'filter'}
    assert service.export(run['id'])['evidence_sha256'] == raw['evidence_sha256']
    app.extensions['workspace'].close()


def test_v3_no_context_is_complete_but_not_a_full_answer(tmp_path, local_index):
    app = create_app(data_dir=tmp_path, transport=FourModeTransport(low_filter=True))
    client = app.test_client()
    owner, run, questions = start(client)
    for question in questions:
        assert ask(client, owner, run, question)['status'] == 'complete'
    service = app.extensions['chat_evaluation_service']
    report = assessment_report(service.export(run['id']))
    assert report['questions'][-1]['modes']['filter']['full_answer'] is True
    report['questions'][-1]['modes']['filter']['full_answer'] = False
    assessed = service.apply_assessment(report)
    assert assessed['totals']['modes']['filter']['no_context'] == 10
    assert assessed['totals']['modes']['filter']['full_answers'] == 0
    app.extensions['workspace'].close()


def test_benchmark_dry_run_isolates_control_and_rejects_existing_output(tmp_path, local_index):
    from scripts.run_two_chat_benchmark import main

    app = create_app(data_dir=tmp_path, transport=FourModeTransport())
    owner = identity(app.test_client().get('/api/state').json)
    source_service = app.extensions['chat_evaluation_service']
    assert source_service.db.execute('SELECT COUNT(*) FROM runs').fetchone()[0] == 0
    app.extensions['workspace'].close()
    prefix = tmp_path / 'bench' / 'control'
    args = ['--data-dir', str(tmp_path), '--profile-id', str(owner['profile_id']),
            '--before', '20', '--after', '6', '--threshold', '2',
            '--output-prefix', str(prefix), '--dry-run']
    assert main(args) == 0
    manifest = json.loads(prefix.with_suffix('.json').read_text(encoding='utf-8'))
    assert manifest['known_spent_before_usd'] is None
    assert manifest['combined_known_spent_usd'] is None
    assert manifest['remaining_known_usd'] is None
    assert manifest['label'] == 'control'
    assert manifest['settings'] == {'top_k_before': 20, 'top_k_after': 6,
                                    'relevance_threshold': 2}
    assert manifest['run']['version'] == 3
    assert manifest['run']['modes'] == ['rag', 'filter']
    assert manifest['run']['config_snapshot']['rag']['top_k_before'] == 20
    assert manifest['run']['config_snapshot']['rag']['top_k_after'] == 6
    assert manifest['run']['questions'][0]['status'] == 'not_started'
    assert manifest['run']['index_snapshot']['sha256'] == manifest['source_index_sha256']
    assert source_service.db.execute('SELECT COUNT(*) FROM runs').fetchone()[0] == 0
    with pytest.raises(FileExistsError):
        main(args)
    with pytest.raises(ValueError, match='known-spent-usd'):
        main([*args[:-1], '--output-prefix', str(tmp_path / 'bench' / 'paid'),
              '--execute'])
    source_service.db.close()


def test_benchmark_stops_after_unknown_filter_before_generation_and_next_question(
        tmp_path, local_index, monkeypatch):
    from scripts import run_two_chat_benchmark as benchmark
    from rag.chat_evaluation import ChatEvaluationService

    app = create_app(data_dir=tmp_path, transport=FourModeTransport())
    owner = identity(app.test_client().get('/api/state').json)
    app.extensions['workspace'].close()
    app.extensions['chat_evaluation_service'].db.close()
    fake = FourModeTransport(unknown_filter=True)
    monkeypatch.setattr(benchmark, 'ResponsesTransport', lambda _key: fake)
    questions = ChatEvaluationService.questions()
    monkeypatch.setattr(ChatEvaluationService, 'questions', staticmethod(lambda: questions[:2]))
    monkeypatch.setenv('OPENAI_API_KEY', 'fixture-key')
    prefix = tmp_path / 'bench' / 'paid'
    result = benchmark.main(['--data-dir', str(tmp_path), '--profile-id',
        str(owner['profile_id']), '--before', '10', '--after', '2', '--threshold',
        '2', '--output-prefix', str(prefix), '--known-spent-usd', '0.05',
        '--execute'])
    manifest = json.loads(prefix.with_suffix('.json').read_text(encoding='utf-8'))
    assert result == 2
    assert manifest['stop_reason'] == 'unknown_cost'
    assert manifest['cost_complete'] is False
    assert manifest['run']['questions'][1]['status'] == 'not_started'
    assert manifest['run']['questions'][0]['pair']['filter']['code'] == 'unknown_cost'
    assert len(fake.calls) == 3


def test_benchmark_rejects_understated_prior_spend_before_side_effects(tmp_path,
                                                                       monkeypatch):
    from scripts import run_two_chat_benchmark as benchmark

    monkeypatch.setenv('OPENAI_API_KEY', 'fixture-key')
    monkeypatch.setattr(benchmark, 'ResponsesTransport',
                        lambda _key: pytest.fail('Transport must not be constructed.'))
    prefix = tmp_path / 'new-parent' / 'paid'
    with pytest.raises(ValueError, match='known-spent-usd'):
        benchmark.main(['--data-dir', str(tmp_path), '--profile-id', '1',
            '--before', '10', '--after', '2', '--threshold', '2',
            '--output-prefix', str(prefix), '--known-spent-usd', '0',
            '--minimum-prior-spend-usd', '0.05', '--execute'])
    assert not prefix.parent.exists()

"""Durable, owner-scoped evaluation of the production chat agent."""

from dataclasses import asdict, replace
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
from threading import RLock
from time import monotonic
from uuid import uuid4

from agent import load_config as load_agent_config
from agent.memory import positive_id
from agent.storage import StorageError
from agent.tokens import accounting
from agent.transport import ResponsesTransport
from rag import chat as chat_module
from rag.chat import RagChatAgent, RagWorkspace
from rag.chat_store import RagSQLiteStore
from rag.config import load_config as load_rag_config


TASK_ROOT = Path(__file__).resolve().parents[1]
RAG_INSTRUCTION_MARKER = '\nЛокальные фрагменты в input'
MODES = ('rag', 'rewrite', 'filter', 'rewrite_filter')
CURRENT_MODES = ('rag', 'filter')


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _hash(value):
    return sha256(_json(value).encode('utf-8')).hexdigest()


def _utc():
    return datetime.now(timezone.utc).isoformat()


def _file_hash(path):
    path = Path(path)
    return sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _runtime_sources():
    """Frozen executable source and configuration surface for one comparison."""
    packages = ('agent', 'rag', 'indexing', 'composition', 'orchestration',
                'knowledge_server', 'digest_server')
    paths = [path for package in packages
             for path in (TASK_ROOT / package).rglob('*.py')]
    paths.extend(path for path in TASK_ROOT.glob('*.py')
                 if path.name != 'check_mcp.py')
    paths.extend(TASK_ROOT / package / 'config.toml'
                 for package in ('agent', 'rag', 'indexing'))
    return tuple(sorted(str(path.relative_to(TASK_ROOT)).replace('\\', '/')
                        for path in paths if path.is_file()))


def _owner(state):
    return (state['personalization']['selected_id'],
            state['workspace']['active_dialogue']['task_id'],
            state['workspace']['active_dialogue']['id'], state['active_branch'])


def _summary(requests):
    called = [item for item in requests if item['usage_status'] != 'not_requested']
    unknown = sum(item['cost_usd'] is None for item in called)
    total = sum((Decimal(item['cost_usd']) for item in called
                 if item['cost_usd'] is not None), Decimal(0))
    return dict(known_cost_usd=format(total, 'f'), cost_complete=unknown == 0,
                unknown_cost_requests=unknown, api_requests=len(called))


class _CapturedTransport:
    def __init__(self, underlying, capture):
        self.underlying = underlying
        self.capture = capture
        self.kind = None

    def create(self, payload, timeout):
        self.capture(self.kind, payload)
        return self.underlying.create(payload, timeout)


class _EvaluationAgent(RagChatAgent):
    def _invoke(self, payload, metadata, ip, op, *, facts=False, output_policy=None):
        # RagChatAgent budgets the modified payload after this override.
        payload = {**payload, 'tools': [], 'tool_choice': 'none',
                   'parallel_tool_calls': False}
        previous_kind = self._transport.kind
        self._transport.kind = metadata.get('kind')
        try:
            return super()._invoke(payload, metadata, ip, op, facts=facts,
                                   output_policy=output_policy)
        finally:
            self._transport.kind = previous_kind


class _EvaluationWorkspace(RagWorkspace):
    def __init__(self, data_dir, transport, index_data_dir, profile_id):
        super().__init__(data_dir, transport, index_data_dir=index_data_dir)
        self.profile_id = profile_id

    def agent(self):
        dialogue = self.memory.workspace()['active_dialogue']
        did = dialogue['id']
        if did not in self._agents:
            agent = _EvaluationAgent(
                replace(load_agent_config(), context_mode=dialogue['mode']),
                self.transport, RagSQLiteStore(self.data_dir / f'dialogue-{did}.sqlite3'),
                index_data_dir=self.index_data_dir, memory_store=self.memory,
                task_id=dialogue['task_id'], dialogue_id=did)
            agent.profile_id = self.profile_id
            self._agents[did] = agent
        return self._agents[did]


class ChatEvaluationService:
    def __init__(self, data_dir, workspace=None, transport=None):
        self.data_dir = Path(data_dir)
        self.workspace = workspace
        selected_transport = transport if transport is not None else (
            workspace.transport if workspace is not None else None)
        self.transport = selected_transport if selected_transport is not None else (
            ResponsesTransport(os.getenv('OPENAI_API_KEY')) if workspace is not None else None)
        self.lock = RLock()
        self.root = self.data_dir / 'chat-evaluations'
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.root / 'runs.sqlite3'),
                                  check_same_thread=False, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA synchronous=FULL')
        with self.db:
            self.db.execute('''CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, profile_id INTEGER NOT NULL, task_id INTEGER NOT NULL,
                dialogue_id INTEGER NOT NULL, branch_id INTEGER NOT NULL, data_json TEXT NOT NULL)''')
            self.db.execute('''CREATE TABLE IF NOT EXISTS payloads (
                run_id TEXT NOT NULL, question_id TEXT NOT NULL, mode TEXT NOT NULL,
                sequence INTEGER NOT NULL, kind TEXT, payload_json TEXT NOT NULL,
                PRIMARY KEY(run_id, question_id, mode, sequence))''')
        if self.workspace is not None:
            self._recover()

    @staticmethod
    def questions():
        return json.loads((TASK_ROOT / 'evaluation' / 'questions.json').read_text(encoding='utf-8'))['questions']

    @staticmethod
    def gold_sha256():
        return sha256((TASK_ROOT / 'evaluation' / 'questions.json').read_bytes()).hexdigest()

    def _verified_index(self, config):
        try:
            chunks = chat_module.read_index(TASK_ROOT, self.data_dir, config)
            if not chunks:
                raise ValueError('Проверенный индекс пуст.')
            return {'sha256': _file_hash(self.data_dir / 'indexing.sqlite3'),
                    'verified_chunks': len(chunks),
                    'chunk_ids_sha256': _hash([chunk['chunk_id'] for chunk in chunks])}
        except (ValueError, OSError, KeyError, TypeError, sqlite3.Error) as error:
            raise ValueError('Проверенный индекс отсутствует, устарел или повреждён.') from error

    def _save(self, run):
        owner = run['owner']
        with self.lock, self.db:
            self.db.execute('''INSERT OR REPLACE INTO runs
                (id,profile_id,task_id,dialogue_id,branch_id,data_json)
                VALUES (?,?,?,?,?,?)''', (run['id'], owner['profile_id'], owner['task_id'],
                owner['dialogue_id'], owner['branch_id'], _json(run)))

    def _get(self, run_id):
        if not isinstance(run_id, str):
            raise ValueError('Неверный идентификатор контрольного прогона.')
        with self.lock:
            row = self.db.execute('SELECT data_json FROM runs WHERE id=?', (run_id,)).fetchone()
        if row is None:
            raise ValueError('Контрольный прогон не найден.')
        return json.loads(row['data_json'])

    def _all(self, owner, *, branch=None):
        where = 'profile_id=? AND task_id=? AND dialogue_id=?'
        args = list(owner[:3])
        if branch is not None:
            where += ' AND branch_id=?'
            args.append(branch)
        with self.lock:
            rows = self.db.execute('SELECT data_json FROM runs WHERE ' + where +
                                   ' ORDER BY rowid', args).fetchall()
        return [json.loads(row['data_json']) for row in rows]

    def _recover(self):
        with self.lock:
            rows = self.db.execute('SELECT data_json FROM runs').fetchall()
        for row in rows:
            run = json.loads(row['data_json'])
            if run['version'] != 3:
                continue  # Historical evidence is read-only, including interrupted runs.
            changed = False
            for q in run['questions']:
                if q.get('status') == 'pending':
                    q['status'] = 'failed'
                    for mode in run['modes']:
                        if q['pair'].get(mode) is not None:
                            previous = q['pair'][mode]
                            result = self._mode_result(run, q['id'], mode,
                                previous.get('duration_ms'))
                            result['index_sha256'] = previous.get('index_sha256')
                            result['base_memory_sha256'] = q.get('base_memory_sha256')
                            q['pair'][mode] = result
                    changed = True
            if changed:
                run['status'] = 'partial'
                run['evidence_sha256'] = self._evidence_hash(run)
                run['totals'] = self._totals(run)
                self._save(run)

    def _identity(self, supplied):
        if self.workspace is None:
            raise ValueError('Контрольный прогон требует рабочее пространство.')
        if not isinstance(supplied, dict) or set(('profile_id','task_id','dialogue_id','branch_id')) - set(supplied):
            raise ValueError('Передайте профиль, задачу, диалог и ветку.')
        values = tuple(positive_id(supplied[key]) for key in
                       ('profile_id','task_id','dialogue_id','branch_id'))
        if values != _owner(self.workspace.state()):
            raise ValueError('Активный профиль, задача, диалог или ветка изменились.')
        return values

    def _owned(self, run_id, identity):
        run = self._get(run_id)
        if tuple(run['owner'][key] for key in
                 ('profile_id','task_id','dialogue_id','branch_id')) != identity:
            raise ValueError('Контрольный прогон не принадлежит текущему диалогу.')
        return run

    def start(self, supplied):
        identity = self._identity(supplied)
        owner = dict(zip(('profile_id','task_id','dialogue_id','branch_id'), identity))
        rag_config = load_rag_config()
        dialogue = self.workspace.state()['workspace']['active_dialogue']
        agent_config = replace(load_agent_config(), context_mode=dialogue['mode'])
        verified_index = self._verified_index(rag_config)
        sources = _runtime_sources()
        run = dict(version=3, modes=list(CURRENT_MODES), id=uuid4().hex, owner=owner, status='pending',
                   assessment_status='pending', created_at=_utc(),
                   gold_sha256=self.gold_sha256(), evidence_sha256=None,
                   settings=dict(top_k_before=rag_config.top_k_before,
                       top_k_after=rag_config.top_k_after,
                       relevance_threshold=rag_config.relevance_threshold,
                       max_context_utf8_bytes=rag_config.max_context_tokens),
                   config_snapshot=dict(agent=asdict(agent_config),
                       rag=asdict(rag_config)),
                   code_snapshot={path: _file_hash(TASK_ROOT / path) for path in sources},
                   index_snapshot=dict(path='indexing.sqlite3', **verified_index,
                       embedding_model=rag_config.embedding_model,
                       embedding_dimensions=rag_config.embedding_dimensions),
                   reviewer=None, questions=[{
                       **q, 'status': 'not_started', 'pair': {mode: None for mode in CURRENT_MODES},
                       'assessment': 'pending'} for q in self.questions()],
                   totals={'assessment': 'pending', 'questions_complete': 0,
                           'known_cost_usd': '0', 'cost_complete': True,
                           'api_requests': 0, 'unknown_cost_requests': 0})
        run['totals'] = self._totals(run)
        self._save(run)
        return run

    def _area(self, run, qid, mode):
        return self.root / run['id'] / qid / mode

    def _clone_memory(self, area):
        area.mkdir(parents=True, exist_ok=True)
        with self.workspace.memory.transaction() as db:
            target = sqlite3.connect(str(area / 'memory.sqlite3'))
            try:
                db.backup(target)
            finally:
                target.close()

    def _capture(self, run_id, qid, mode, kind, payload):
        with self.lock, self.db:
            row = self.db.execute('''SELECT COALESCE(MAX(sequence),0)+1 FROM payloads
                WHERE run_id=? AND question_id=? AND mode=?''', (run_id,qid,mode)).fetchone()
            self.db.execute('INSERT INTO payloads VALUES(?,?,?,?,?,?)',
                            (run_id,qid,mode,row[0],kind,_json(payload)))

    def _payloads(self, run_id, qid, mode):
        with self.lock:
            rows = self.db.execute('''SELECT kind,payload_json FROM payloads
                WHERE run_id=? AND question_id=? AND mode=? ORDER BY sequence''',
                (run_id,qid,mode)).fetchall()
        return [dict(kind=row['kind'], payload=json.loads(row['payload_json'])) for row in rows]

    def _source_state(self, run, qid, mode):
        area = self._area(run,qid,mode)
        if not (area / 'memory.sqlite3').is_file():
            return None
        holder = _EvaluationWorkspace(area, _CapturedTransport(self.transport,
            lambda _kind,_payload: None), self.data_dir, run['owner']['profile_id'])
        try:
            return holder.state()
        finally:
            holder.close()

    @staticmethod
    def _normalized_generation(payload, mode):
        value = json.loads(_json(payload))
        if mode in MODES:
            if not value['input'] or not value['input'][0].get('content','').startswith(
                    'Локальные справочные фрагменты для текущего вопроса'):
                return None
            value['input'] = value['input'][1:]
            at = value['instructions'].find(RAG_INSTRUCTION_MARKER)
            if at < 0:
                return None
            value['instructions'] = value['instructions'][:at]
            structured = value.get('text', {}).get('format')
            if structured == chat_module._task_response_format(grounded=True):
                value['text'] = {'format': chat_module._task_response_format()}
            elif structured == chat_module.response_format():
                value.pop('text')
            else:
                return None
        return value

    def _mode_result(self, run, qid, mode, duration_ms=None):
        state = self._source_state(run,qid,mode)
        if state is None:
            return dict(status='interrupted',text='',code='interrupted',requests=[],
                        summary=_summary([]),token_accounting=accounting([]),
                        duration_ms=duration_ms,sources=[],context='',candidates=[],
                        retrieval={},payloads=[],initial_context_sha256=None,
                        base_generation_payload=None,generation_requested=False)
        requests = [self._namespaced(run,qid,mode,item) for item in state['requests']]
        parent = next((r for r in requests if r['metadata'].get('underlying_kind') == 'answer'), None)
        rag = parent['metadata'].get('rag', {}) if parent else {}
        payloads = self._payloads(run['id'],qid,mode)
        generation = next((p['payload'] for p in payloads if p['kind'] == 'answer'), None)
        neutral = rag.get('base_generation_payload') or (
            self._normalized_generation(generation,mode) if generation else None)
        base_hash = rag.get('base_generation_sha256') or (_hash(neutral) if neutral else None)
        accepted = next((m['content'] for m in state['messages']
                         if parent and m['role']=='assistant' and m['request_id']==
                         parent['metadata']['source_request_id']), None)
        status = parent['status'] if parent else 'interrupted'
        if rag.get('status') == 'no_context' or (parent and parent.get('code') == 'no_context'):
            status = 'no_context'
        result = dict(status=status,
                    text=(accepted if accepted is not None else parent['text']) if parent else '',
                    code=parent['code'] if parent else 'interrupted',
                    requests=requests,summary=_summary(requests),
                    token_accounting=accounting(requests),duration_ms=duration_ms,
                    sources=rag.get('sources',[]), context=rag.get('context',''),
                    candidates=rag.get('candidates',[]),retrieval=rag,
                    payloads=payloads,initial_context_sha256=base_hash,
                    base_generation_payload=neutral,
                    generation_requested=bool(parent and parent['usage_status'] != 'not_requested'))
        if status == 'no_context' and (generation is not None or parent['usage_status'] != 'not_requested'):
            result['status'] = 'error'
            result['code'] = 'invalid_no_context_evidence'
        if neutral is not None and base_hash != _hash(neutral):
            result['status'] = 'error'
            result['code'] = 'invalid_base_generation_hash'
        if generation is not None and self._normalized_generation(generation, mode) != neutral:
            result['status'] = 'error'
            result['code'] = 'generation_input_mismatch'
        return result

    @staticmethod
    def _namespaced(run,qid,mode,item):
        result = json.loads(_json(item))
        source_id = result['id']
        prefix = f"eval:{run['id']}:{qid}:{mode}:"
        result['id'] = prefix + str(source_id)
        meta = result['metadata']
        meta['underlying_kind'] = meta.get('kind')
        meta['kind'] = 'chat_evaluation'
        meta['evaluation_run_id'] = run['id']
        meta['question_id'] = qid
        meta['mode'] = mode
        meta['source_request_id'] = source_id
        if meta.get('parent_request_id') is not None:
            meta['source_parent_request_id'] = meta['parent_request_id']
            meta['parent_request_id'] = prefix + str(meta['parent_request_id'])
        return result

    def _receipts(self, run):
        result = []
        for q in run['questions']:
            if q['pair']:
                for mode in run['modes']:
                    if q['pair'].get(mode) is not None:
                        result.extend(self._mode_result(run,q['id'],mode)['requests'])
        return result

    def _totals(self,run):
        summary = _summary(self._receipts(run))
        reviewed = run['reviewer'] is not None
        modes = {}
        for mode in run['modes']:
            results = [q['pair'][mode] for q in run['questions'] if q['pair'][mode] is not None]
            receipts = [r for result in results for r in result['requests']]
            reviewed_rows = [q['assessment']['modes'][mode] for q in run['questions']
                             if reviewed and isinstance(q['assessment'], dict)]
            modes[mode] = {**_summary(receipts),
                'correct_facts': sum(f['correct'] for row in reviewed_rows for f in row['facts']) if reviewed else None,
                'supported_facts': sum(f['supported'] for row in reviewed_rows for f in row['facts']) if reviewed else None,
                'total_facts': sum(len(row['facts']) for row in reviewed_rows) if reviewed else None,
                'full_answers': sum(row['full_answer'] for row in reviewed_rows) if reviewed else None,
                'context_sufficient': sum(row['context_sufficient'] for row in reviewed_rows) if reviewed else None,
                'candidate_sufficient': sum(row['candidate_sufficient'] for row in reviewed_rows) if reviewed else None,
                'q10_abstained': next((row['abstained'] for q,row in
                    ((q,q['assessment']['modes'][mode]) for q in run['questions']) if q['id']=='q10'), None)
                    if reviewed else None,
                'unsupported_claims': sum(len(row['unsupported_claims']) for row in reviewed_rows) if reviewed else None,
                'no_context': sum(r['status']=='no_context' for r in results),
                'error': sum(r['status'] in ('error','interrupted') for r in results),
                'rejected': sum(r['status']=='rejected' for r in results),
                'duration_ms': sum(r['duration_ms'] or 0 for r in results)}
        result = {**summary, 'assessment': 'complete' if reviewed else 'pending',
                  'questions_complete': sum(q['status']=='complete' for q in run['questions']),
                  'modes': modes}
        if reviewed:
            result['paired'] = {mode: {
                'gains': [q['id'] for q in run['questions'] if q['assessment']['modes'][mode]['full_answer']
                          and not q['assessment']['modes']['rag']['full_answer']],
                'losses': [q['id'] for q in run['questions'] if q['assessment']['modes']['rag']['full_answer']
                           and not q['assessment']['modes'][mode]['full_answer']]}
                for mode in run['modes'] if mode != 'rag'}
        return result

    def _evidence_hash(self,run):
        evidence = dict(version=run['version'], modes=run['modes'],id=run['id'],
            owner=run['owner'],gold_sha256=run['gold_sha256'],
            settings=run['settings'],config_snapshot=run['config_snapshot'],
            code_snapshot=run['code_snapshot'],index_snapshot=run['index_snapshot'],
            questions=[dict(id=q['id'],question=q['question'],expected_facts=q['expected_facts'],
                expected_sources=q['expected_sources'],unanswerable=q['unanswerable'],
                status=q['status'],base_memory_sha256=q.get('base_memory_sha256'),
                pair=q['pair']) for q in run['questions']])
        return _hash(evidence)

    def question(self,supplied):
        identity = self._identity(supplied)
        run = self._owned(supplied.get('run_id'),identity)
        qid = supplied.get('question_id')
        q = next((item for item in run['questions'] if item['id']==qid),None)
        if q is None or not isinstance(supplied.get('prompt'),str) or supplied['prompt'].strip()!=q['question']:
            raise ValueError('Передайте точный текст фиксированного контрольного вопроса.')
        if q['status']!='not_started':
            return run
        if run['version'] != 3:
            raise ValueError('Исторический контрольный прогон доступен только для чтения.')
        modes = tuple(run['modes'])
        from rag.chat import RAG_MODES
        if tuple(RAG_MODES) != MODES:
            raise ValueError('Режимы оценки не совпадают с режимами основного агента.')
        dialogue = self.workspace.state()['workspace']['active_dialogue']
        current_agent = replace(load_agent_config(), context_mode=dialogue['mode'])
        current_rag = load_rag_config()
        current_index = self._verified_index(current_rag)
        if (self.gold_sha256()!=run['gold_sha256'] or
            asdict(current_rag)!=run['config_snapshot']['rag'] or
            asdict(current_agent)!=run['config_snapshot']['agent'] or
            any(current_index[key]!=run['index_snapshot'][key] for key in
                ('sha256','verified_chunks','chunk_ids_sha256')) or
            set(_runtime_sources())!=set(run['code_snapshot']) or
            any(_file_hash(TASK_ROOT/path)!=digest for path,digest in run['code_snapshot'].items())):
            raise ValueError('Замороженная конфигурация, gold или индекс изменились.')
        q['status']='pending'
        q['pair'][modes[0]]={'status':'pending'}
        self._save(run)  # Idempotency boundary precedes every paid call.
        # Both areas are copied from one owner state before the first API call.
        for mode in modes:
            self._clone_memory(self._area(run,qid,mode))
        q['base_memory_sha256'] = _file_hash(self._area(run,qid,modes[0])/'memory.sqlite3')
        if any(_file_hash(self._area(run,qid,mode)/'memory.sqlite3')!=q['base_memory_sha256']
               for mode in modes):
            q['status']='failed'
            q['pair'][modes[0]]={'status':'error','code':'memory_clone_mismatch'}
            self._save(run)
            return run
        self._save(run)
        for index,mode in enumerate(modes):
            if index:
                q['pair'][mode]={'status':'pending'}
                self._save(run)
            area=self._area(run,qid,mode)
            wrapped=_CapturedTransport(self.transport,
                lambda kind,payload,m=mode:self._capture(run['id'],qid,m,kind,payload))
            holder=_EvaluationWorkspace(area,wrapped,self.data_dir,identity[0])
            started=monotonic()
            try:
                holder.agent().run(q['question'],rag_mode=mode,
                                   use_working=False,use_long_term=False)
            except Exception:
                # The internal SQLite ledger, including paid stages, is the authority.
                pass
            finally:
                holder.close()
            q['pair'][mode]=self._mode_result(run,qid,mode,round((monotonic()-started)*1000))
            q['pair'][mode]['index_sha256']=_file_hash(self.data_dir/'indexing.sqlite3')
            q['pair'][mode]['base_memory_sha256']=q['base_memory_sha256']
            self._save(run)
            if q['pair'][mode]['status'] not in ('ok','no_context'):
                break
        results=[q['pair'][mode] for mode in modes]
        hashes=[result['initial_context_sha256'] for result in results if result is not None]
        q['status']='complete' if (all(result is not None and result['status'] in ('ok','no_context')
            for result in results) and all(hash_value is not None for hash_value in hashes)
            and len(set(hashes))==1 and all(result['index_sha256']==run['index_snapshot']['sha256']
            for result in results) and all(result['retrieval'].get('mode')==mode and
            result['retrieval'].get('original_query')==q['question'] and
            all(result['retrieval'].get('settings',{}).get(key)==value
                for key,value in run['settings'].items())
            for mode,result in zip(modes,results))) else 'failed'
        run['status']='complete' if all(item['status']=='complete' for item in run['questions']) else (
            'partial' if any(item['status'] in ('complete','failed','interrupted') for item in run['questions']) else 'pending')
        run['evidence_sha256']=self._evidence_hash(run)
        run['totals']={**run.get('totals',{}),**self._totals(run)}
        self._save(run)
        return run

    def augment(self,state):
        if self.workspace is None or 'personalization' not in state:
            return {**state,'chat_evaluations':[],'chat_evaluation_requests':[]}
        identity=_owner(state)
        runs=self._all(identity,branch=identity[3])
        dialogue_runs=self._all(identity)
        receipts=[]
        for run in dialogue_runs:
            for receipt in self._receipts(run):
                if run['owner']['branch_id']!=identity[3]:
                    receipt={**receipt,'text':'','metadata':{
                        'kind':'chat_evaluation',
                        'underlying_kind':receipt['metadata'].get('underlying_kind'),
                        'parent_request_id':receipt['metadata'].get('parent_request_id'),
                        'redacted':True}}
                receipts.append(receipt)
        seen=set()
        receipts=[r for r in receipts if not (r['id'] in seen or seen.add(r['id']))]
        base=state['summary']
        extra=_summary(receipts)
        unknown=base['unknown_cost_requests']+extra['unknown_cost_requests']
        total=Decimal(base['known_cost_usd'])+Decimal(extra['known_cost_usd'])
        prior=state['token_accounting']
        token=accounting(receipts)
        return {**state,'chat_evaluations':runs,'chat_evaluation_requests':receipts,
            'summary':dict(known_cost_usd=format(total,'f'),cost_complete=unknown==0,
                unknown_cost_requests=unknown,api_requests=base['api_requests']+extra['api_requests']),
            'token_accounting':dict(known_input_tokens=prior['known_input_tokens']+token['known_input_tokens'],
                known_output_tokens=prior['known_output_tokens']+token['known_output_tokens'],
                known_total_tokens=prior['known_total_tokens']+token['known_total_tokens'],
                complete=prior['complete'] and token['complete'],
                unknown_requests=prior['unknown_requests']+token['unknown_requests'])}

    def trace(self,receipt_id,identity):
        if not isinstance(receipt_id,str):
            raise ValueError('Запрос не найден.')
        for run in self._all(identity,branch=identity[3]):
            for item in self._receipts(run):
                if item['id']==receipt_id:
                    context=item['metadata'].get('context',{})
                    return dict(status='ok',context=context,selection=context.get('selection',{}),
                        metadata=item['metadata'],request=item,
                        payloads=self._payloads(run['id'],item['metadata']['question_id'],item['metadata']['mode']))
        raise ValueError('Запрос не найден в активном диалоге.')

    def export(self,run_id,profile_id=None):
        run=self._get(run_id)
        if profile_id is not None and run['owner']['profile_id']!=positive_id(profile_id):
            raise ValueError('Контрольный прогон принадлежит другому профилю.')
        run['totals']={**run.get('totals',{}),**self._totals(run)}
        run['evidence_sha256']=self._evidence_hash(run)
        return run

    def apply_assessment(self,report,profile_id=None):
        if not isinstance(report,dict) or set(report)!={'version','run_id','evidence_sha256','reviewer','questions'}:
            raise ValueError('Неверный формат независимой оценки.')
        if type(report['version']) is not int or report['version'] not in (2, 3):
            raise ValueError('Оценка требует версию 2 или 3.')
        run=self._get(report['run_id'])
        if profile_id is not None and run['owner']['profile_id']!=positive_id(profile_id):
            raise ValueError('Контрольный прогон принадлежит другому профилю.')
        if (run['version'] != report['version'] or run['reviewer'] is not None or
            any(q['status']!='complete' for q in run['questions'])):
            raise ValueError('Оценка возможна только для полного прогона той же версии.')
        if report['evidence_sha256']!=self._evidence_hash(run):
            raise ValueError('Оценка относится к другой версии свидетельств.')
        reviewer=report['reviewer']
        if (not isinstance(reviewer,dict) or set(reviewer)!={'kind','profile','model','reasoning_effort'}
            or any(not isinstance(v,str) or not v.strip() for v in reviewer.values())):
            raise ValueError('Укажите независимого проверяющего и модель.')
        rows=report['questions']
        if not isinstance(rows,list) or len(rows)!=10 or [x.get('id') for x in rows if isinstance(x,dict)]!=[q['id'] for q in run['questions']]:
            raise ValueError('Оцените ровно десять вопросов в фиксированном порядке.')
        for q,row in zip(run['questions'],rows):
            if (set(row)!={'id','modes','conclusion'} or not isinstance(row['modes'],dict)
                or set(row['modes'])!=set(run['modes'])
                or not isinstance(row['conclusion'],str) or not row['conclusion'].strip()):
                raise ValueError('Неполный разбор вопроса.')
            for mode in run['modes']:
                value=row['modes'][mode]
                if (not isinstance(value,dict) or set(value)!={
                    'facts','full_answer','unsupported_claims','abstained','reason',
                    'candidate_sufficient','candidate_reason','context_sufficient','context_reason'}
                    or any(type(value[key]) is not bool for key in
                        ('full_answer','abstained','candidate_sufficient','context_sufficient'))
                    or any(not isinstance(value[key],str) or not value[key].strip() for key in
                        ('reason','candidate_reason','context_reason'))
                    or not isinstance(value['unsupported_claims'],list)
                    or any(not isinstance(s,str) or not s.strip() for s in value['unsupported_claims'])
                    or not isinstance(value['facts'],list)
                    or len(value['facts'])!=len(q['expected_facts'])):
                    raise ValueError('Неверная оценка полного ответа.')
                result=q['pair'][mode]
                known={s['label'] for s in result['sources']}
                for i,fact in enumerate(value['facts']):
                    if (not isinstance(fact,dict) or set(fact)!={
                        'fact_index','correct','supported','source_labels','reason'}
                        or type(fact['fact_index']) is not int or fact['fact_index']!=i
                        or type(fact['correct']) is not bool or type(fact['supported']) is not bool
                        or not isinstance(fact['reason'],str) or not fact['reason'].strip()
                        or not isinstance(fact['source_labels'],list)
                        or any(type(label) is not str or label not in known
                               for label in fact['source_labels'])
                        or len(fact['source_labels'])!=len(set(fact['source_labels']))
                        or (fact['supported'] and not fact['source_labels'])
                        or (not fact['supported'] and fact['source_labels'])
                        or (result['status']=='no_context' and fact['supported'])):
                        raise ValueError('Неверная оценка факта или метка источника.')
                if result['status']=='no_context' and value['context_sufficient']:
                    raise ValueError('Пустой контекст не может быть достаточным.')
                expected_full=(all(f['correct'] for f in value['facts']) and
                    not value['unsupported_claims'] and value['abstained'] and
                    (run['version'] == 2 or result['status'] == 'ok') if q['unanswerable'] else
                    result['status']=='ok' and not value['abstained'] and
                    all(f['correct'] and f['supported'] for f in value['facts']) and
                    not value['unsupported_claims'])
                if value['full_answer']!=expected_full:
                    raise ValueError('Полнота ответа противоречит оценке фактов и опоры на источники.')
        for q,row in zip(run['questions'],rows):
            q['assessment']=row
        run['reviewer']=reviewer
        run['assessment_status']='reviewed'
        run['totals']=self._totals(run)
        self._save(run)
        return run

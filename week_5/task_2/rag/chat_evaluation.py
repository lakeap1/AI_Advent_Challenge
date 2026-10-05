"""Durable, owner-scoped evaluation of the production chat agent."""

from dataclasses import replace
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
from rag.chat import RagChatAgent, RagWorkspace
from rag.chat_store import RagSQLiteStore


TASK_ROOT = Path(__file__).resolve().parents[1]
RAG_INSTRUCTION_MARKER = '\nЛокальные фрагменты в input'


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _hash(value):
    return sha256(_json(value).encode('utf-8')).hexdigest()


def _utc():
    return datetime.now(timezone.utc).isoformat()


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
        self._transport.kind = metadata.get('kind')
        return super()._invoke(payload, metadata, ip, op, facts=facts,
                               output_policy=output_policy)


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
            changed = False
            for q in run['questions']:
                if q.get('status') == 'pending':
                    q['status'] = 'interrupted'
                    for mode in ('plain', 'rag'):
                        if q['pair'].get(mode) is not None:
                            q['pair'][mode] = self._mode_result(run, q['id'], mode)
                    changed = True
            if changed:
                run['status'] = 'interrupted'
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
        run = dict(id=uuid4().hex, owner=owner, status='pending',
                   assessment_status='pending', created_at=_utc(),
                   gold_sha256=self.gold_sha256(), evidence_sha256=None,
                   reviewer=None, questions=[{
                       **q, 'status': 'not_started', 'pair': {'plain': None, 'rag': None},
                       'assessment': 'pending'} for q in self.questions()],
                   totals={'assessment': 'pending', 'questions_complete': 0,
                           'known_cost_usd': '0', 'cost_complete': True,
                           'api_requests': 0, 'unknown_cost_requests': 0})
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
        if mode == 'rag':
            if not value['input'] or not value['input'][0].get('content','').startswith(
                    'Локальные справочные фрагменты для текущего вопроса'):
                return None
            value['input'] = value['input'][1:]
            at = value['instructions'].find(RAG_INSTRUCTION_MARKER)
            if at < 0:
                return None
            value['instructions'] = value['instructions'][:at]
        return value

    def _mode_result(self, run, qid, mode, duration_ms=None):
        state = self._source_state(run,qid,mode)
        if state is None:
            return dict(status='interrupted',text='',code='interrupted',requests=[],
                        summary=_summary([]),token_accounting=accounting([]),
                        duration_ms=duration_ms,sources=[],context='',payloads=[],
                        initial_context_sha256=None)
        requests = [self._namespaced(run,qid,mode,item) for item in state['requests']]
        parent = next((r for r in requests if r['metadata'].get('underlying_kind') == 'answer'), None)
        rag = parent['metadata'].get('rag', {}) if parent else {}
        payloads = self._payloads(run['id'],qid,mode)
        generation = next((p['payload'] for p in payloads if p['kind'] == 'answer'), None)
        neutral = self._normalized_generation(generation,mode) if generation else None
        accepted = next((m['content'] for m in state['messages']
                         if parent and m['role']=='assistant' and m['request_id']==
                         parent['metadata']['source_request_id']), None)
        return dict(status=parent['status'] if parent else 'interrupted',
                    text=(accepted if accepted is not None else parent['text']) if parent else '',
                    code=parent['code'] if parent else 'interrupted',
                    requests=requests,summary=_summary(requests),
                    token_accounting=accounting(requests),duration_ms=duration_ms,
                    sources=rag.get('sources',[]), context=rag.get('context',''),
                    payloads=payloads,initial_context_sha256=_hash(neutral) if neutral else None)

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
                for mode in ('plain','rag'):
                    if q['pair'].get(mode) is not None:
                        result.extend(self._mode_result(run,q['id'],mode)['requests'])
        return result

    def _totals(self,run):
        summary = _summary(self._receipts(run))
        return {**summary, 'assessment': 'pending' if run['reviewer'] is None else 'complete',
                'questions_complete': sum(q['status']=='complete' for q in run['questions'])}

    def _evidence_hash(self,run):
        evidence = dict(id=run['id'],owner=run['owner'],gold_sha256=run['gold_sha256'],
            questions=[dict(id=q['id'],question=q['question'],expected_facts=q['expected_facts'],
                expected_sources=q['expected_sources'],unanswerable=q['unanswerable'],
                status=q['status'],pair=q['pair']) for q in run['questions']])
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
        q['status']='pending'
        q['pair']['plain']={'status':'pending'}
        self._save(run)  # Idempotency boundary precedes every paid call.
        for mode in ('plain','rag'):
            if mode=='rag' and q['pair']['plain']['status']!='ok':
                break
            if mode=='rag':
                q['pair']['rag']={'status':'pending'}
                self._save(run)  # The RAG ledger must be discoverable after interruption.
            area=self._area(run,qid,mode)
            if mode=='plain':
                self._clone_memory(area)
                self._clone_memory(self._area(run,qid,'rag'))
            wrapped=_CapturedTransport(self.transport,
                lambda kind,payload,m=mode:self._capture(run['id'],qid,m,kind,payload))
            holder=_EvaluationWorkspace(area,wrapped,self.data_dir,identity[0])
            started=monotonic()
            try:
                holder.agent().run(q['question'],use_rag=(mode=='rag'),
                                   use_working=False,use_long_term=False)
            except Exception:
                # The internal SQLite ledger, including paid stages, is the authority.
                pass
            finally:
                holder.close()
            q['pair'][mode]=self._mode_result(run,qid,mode,round((monotonic()-started)*1000))
            self._save(run)
        plain=q['pair']['plain']
        rag=q['pair']['rag']
        q['status']='complete' if (rag and plain['status']=='ok' and rag['status']=='ok' and
            plain['initial_context_sha256'] is not None and
            plain['initial_context_sha256']==rag['initial_context_sha256']) else 'failed'
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
        if not isinstance(report,dict) or set(report)!={'run_id','evidence_sha256','reviewer','questions'}:
            raise ValueError('Неверный формат независимой оценки.')
        run=self._get(report['run_id'])
        if profile_id is not None and run['owner']['profile_id']!=positive_id(profile_id):
            raise ValueError('Контрольный прогон принадлежит другому профилю.')
        if run['reviewer'] is not None or any(q['status']!='complete' for q in run['questions']):
            raise ValueError('Оценка возможна только для полного нового прогона.')
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
            if (set(row)!={'id','facts','plain','rag','context_sufficient','context_reason','conclusion'}
                or type(row['context_sufficient']) is not bool
                or not isinstance(row['context_reason'],str) or not row['context_reason'].strip()
                or not isinstance(row['conclusion'],str) or not row['conclusion'].strip()):
                raise ValueError('Неполный разбор вопроса.')
            facts=row['facts']
            if not isinstance(facts,list) or len(facts)!=len(q['expected_facts']):
                raise ValueError('Оцените каждый атомарный факт.')
            known={s['label'] for s in q['pair']['rag']['sources']}
            for i,fact in enumerate(facts):
                if (not isinstance(fact,dict) or set(fact)!={'fact_index','plain_correct','plain_reason',
                    'rag_correct','rag_supported','rag_source_labels','rag_reason'}
                    or type(fact['fact_index']) is not int or fact['fact_index']!=i
                    or any(type(fact[k]) is not bool for k in ('plain_correct','rag_correct','rag_supported'))
                    or any(not isinstance(fact[k],str) or not fact[k].strip() for k in ('plain_reason','rag_reason'))
                    or not isinstance(fact['rag_source_labels'],list)
                    or (not q['unanswerable'] and fact['rag_supported'] and not fact['rag_source_labels'])
                    or any(type(label) is not str or label not in known for label in fact['rag_source_labels'])
                    or len(fact['rag_source_labels'])!=len(set(fact['rag_source_labels']))
                    ):
                    raise ValueError('Неверная оценка факта или метка источника.')
            for mode in ('plain','rag'):
                value=row[mode]
                if (not isinstance(value,dict) or set(value)!={'full_answer','unsupported_claims','abstained','reason'}
                    or type(value['full_answer']) is not bool or type(value['abstained']) is not bool
                    or not isinstance(value['unsupported_claims'],list)
                    or any(not isinstance(s,str) or not s.strip() for s in value['unsupported_claims'])
                    or not isinstance(value['reason'],str) or not value['reason'].strip()):
                    raise ValueError('Неверная оценка полного ответа.')
                expected_full=(all(f['plain_correct'] for f in facts) if mode=='plain'
                    else all(f['rag_correct'] and (f['rag_supported'] or q['unanswerable'])
                             for f in facts))
                expected_full=(expected_full and not value['unsupported_claims'] and
                    (not q['unanswerable'] or value['abstained']))
                if value['full_answer']!=expected_full:
                    raise ValueError('Полнота ответа противоречит оценке фактов и опоры на источники.')
        for q,row in zip(run['questions'],rows):
            q['assessment']=row
        run['reviewer']=reviewer
        run['assessment_status']='reviewed'
        run['totals']={**self._totals(run),
            'plain_correct_facts':sum(f['plain_correct'] for row in rows for f in row['facts']),
            'rag_correct_facts':sum(f['rag_correct'] for row in rows for f in row['facts']),
            'rag_supported_facts':sum(f['rag_supported'] for row in rows for f in row['facts']),
            'plain_full_answers':sum(row['plain']['full_answer'] for row in rows),
            'rag_full_answers':sum(row['rag']['full_answer'] for row in rows),
            'plain_q10_abstained':rows[-1]['plain']['abstained'],
            'rag_q10_abstained':rows[-1]['rag']['abstained']}
        self._save(run)
        return run

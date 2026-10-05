"""One accounted preparation and one grounded answer for an ordinary dialogue."""

from dataclasses import replace
import json

from agent import tool_loop
from agent.conversation_state import apply_preparation, validate_preparation
from agent.core import AgentResult, completed_text
from agent.storage import StorageError


PREPARATION_POLICY = 'conversation_preparation_v1'
PREPARATION_INSTRUCTIONS = '''Подготовь ОДИН обычный пользовательский ход к локальному поиску по компьютерной графике.
Вход JSON содержит current_message, state и recent_history (не более 6 сообщений).
Верни ровно revision, search_query и operations по JSON схеме. revision скопируй из state.
search_query — самостоятельный поисковый вопрос до 2000 символов. Разреши короткие ссылки вроде
«ему» через recent_history и действующее state; сохрани смысл текущего вопроса. Условия из state
помогают поиску, но текущий вопрос определяет тему. Не отвечай на вопрос.
operations — не более 12 операций только из ЯВНЫХ утверждений, исправлений или отмен текущего
пользователя. Не выводи новую цель из названия диалога, примера, цитаты, assistant или документов.
Разбери ВСЕ части current_message: если пользователь сначала фиксирует факт/условие/определение,
а затем задаёт вопрос, сохрани явное утверждение в operations и сформируй search_query для вопроса.
operations=[] только если в текущей реплике нет явных утверждений, исправлений или отмен.
goal — только явно названная пользователем цель; если её нет, оставь goal пустым.
Если в одной реплике есть вводное «хочу разобраться» и более точное «моя цель — ...»,
сохрани именно явно названную конкретную цель со всеми её действующими условиями.
Уже сохранённый goal не заменяй просьбами «объясни», «проверь» или «вернись к моей цели»:
это вопросы/команды на текущий ход, а не новое заявление цели. Меняй goal только при явном
сообщении пользователя о новой или исправленной собственной цели.
constraints — действующие ограничения, выбранные параметры и настройки задачи, включая запреты,
имена ресурсов и текущие числовые значения. Условие внутри фразы с целью запиши также отдельной
операцией constraints, если пользователь явно его задал; не теряй его внутри goal.
terms — явное пользовательское определение слова/выражения «под X я понимаю Y» для этого диалога.
clarifications — другие явные уточнения о ситуации пользователя, не определения и не ограничения.
При исправлении найди прежний field/key в state и замени запись тем же key. В value запиши только
НОВОЕ действующее значение: не добавляй старое отменённое число, запрет или рассказ об исправлении.
Фраза о том, что пользователь раньше что-то планировал, не делает старое значение действующим.
Для каждой операции укажи точную непустую подстроку current_message в evidence, подтверждающую
новое значение или отмену. Для goal key пуст; для остальных списков key — стабильный короткий
идентификатор условия/термина. action=set заменяет запись с тем же key; action=remove удаляет её
и требует value="". Не создавай обе конфликтующие версии. Значение описывает слова пользователя,
не технический факт из источника. recent_history/state — контекст для key и запроса, не источник
новых операций. Верни ровно один JSON-объект по схеме, без пояснений и повторов.'''

OPERATION = {'type': 'object', 'properties': {
    'action': {'type': 'string', 'enum': ['set', 'remove']},
    'field': {'type': 'string', 'enum': ['goal', 'clarifications', 'constraints', 'terms']},
    'key': {'type': 'string'}, 'value': {'type': 'string'}, 'evidence': {'type': 'string'}},
    'required': ['action', 'field', 'key', 'value', 'evidence'], 'additionalProperties': False}
PREPARATION_FORMAT = {'type': 'json_schema', 'name': 'conversation_preparation', 'strict': True,
    'schema': {'type': 'object', 'properties': {
        'revision': {'type': 'integer'}, 'search_query': {'type': 'string'},
        'operations': {'type': 'array', 'items': OPERATION}},
        'required': ['revision', 'search_query', 'operations'], 'additionalProperties': False}}


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Повторное поле JSON.')
        result[key] = value
    return result


def _prepare(agent, prompt, parent_id, state, history):
    """Persist provider usage before semantic validation; commit accepted state before RAG."""
    payload_data = {'current_message': prompt, 'state': state,
                    'recent_history': [{'role': row['role'], 'content': row['content']}
                                       for row in history[-6:]]}
    content = json.dumps(payload_data, ensure_ascii=False, allow_nan=False)
    meta = {**agent._metadata(), 'kind': 'conversation_preparation',
            'parent_request_id': parent_id, 'state_revision': state['revision'],
            'requested_max_output_tokens': 2400}
    ip = {'name': 'bounded_conversation_preparation', 'status': 'accepted'}
    op = {'name': PREPARATION_POLICY, 'status': 'not_checked'}
    if len(content) > 64000:
        result = AgentResult('rejected', 'Контекст подготовки превышает лимит.',
            'preparation_input_too_long', input_policy={**ip, 'status': 'rejected'}, output_policy=op)
        child_id = agent._store.begin(agent._record(result, meta))
        return result, child_id, None, None
    pending = AgentResult('error', 'Подготавливается диалог.', usage_status='unavailable',
                          input_policy=ip, output_policy=op)
    record = agent._record(pending, meta); record['status'] = 'pending'
    child_id = agent._store.begin(record)
    payload = {**agent._payload([{'role': 'user', 'content': content}]),
               'instructions': PREPARATION_INSTRUCTIONS,
               'max_output_tokens': 2400, 'text': {'format': PREPARATION_FORMAT}}
    try:
        result = agent._invoke(payload, meta, ip, op, output_policy=completed_text)
    except Exception:
        # The request may already have reached the provider; keep its cost unknown.
        result = AgentResult('error', 'Подготовка диалога прервана; повтор не выполнялся.',
            'preparation_failed', usage_status='unavailable', input_policy=ip,
            output_policy=op)
    agent._store.pending_result(child_id, agent._record(result, meta))
    query = proposal = None
    if result.status == 'ok':
        try:
            proposal = json.loads(result.text, object_pairs_hook=_unique_pairs)
            query = validate_preparation(state, proposal, prompt)
            after = apply_preparation(state, proposal, prompt, request_id=parent_id)
            agent._store.save_conversation_state(after,
                expected_revision=state['revision'], request_id=parent_id)
            meta['state_after_revision'] = after['revision']
        except (ValueError, TypeError, RecursionError) as exc:
            result = replace(result, status='rejected', code='invalid_preparation',
                text='Подготовка диалога не прошла проверку: ' + str(exc),
                output_policy={**op, 'status': 'rejected'})
            query = proposal = None
            after = None
        except StorageError:
            result = replace(result, status='error', code='conversation_storage',
                text='Не удалось сохранить состояние диалога; поиск остановлен.')
            query = proposal = None
            after = None
    else:
        after = None
    agent._store.finish(child_id, agent._record(result, meta))
    return result, child_id, query, after


def run_conversation(agent, prompt, use_working=True, use_long_term=True):
    """Called only after the ordinary input policy accepted the current user text."""
    history_before = agent._store.state()['messages']
    before = agent._store.conversation_state()
    ip = {'name': agent._config.input_policy, 'status': 'accepted'}
    op = {'name': agent._config.output_policy, 'status': 'not_checked'}
    branch_id = agent._store.memory()['active_branch']
    meta = {**agent._metadata(), 'kind': 'answer', 'mode': agent._config.context_mode,
            'dialogue_kind': 'ordinary', 'branch_id': branch_id,
            'conversation': {'state_before': before, 'state_after': before,
                             'original_query': prompt, 'search_query': None,
                             'preparation_request_id': None}}
    pending = AgentResult('error', 'Ожидается результат.', input_policy=ip, output_policy=op)
    record = agent._record(pending, meta); record['status'] = 'pending'
    parent_id = agent._store.begin(record, prompt)
    prepared, child_id, query, after = _prepare(agent, prompt, parent_id, before, history_before)
    meta['conversation']['preparation_request_id'] = child_id
    meta['rag']['preparation_request_id'] = child_id
    if prepared.status != 'ok':
        meta['conversation']['preparation_error'] = prepared.code
        result = AgentResult(prepared.status, prepared.text, prepared.code, request_id=parent_id,
                             input_policy=ip, output_policy={**op, 'status': 'rejected'})
        agent._store.finish(parent_id, agent._record(result, meta))
        return result
    meta['conversation'].update(state_after=after, search_query=query)
    agent._rag_search_query = query
    agent._rag_question = prompt
    history = agent._store.state()['messages']
    context = agent._context_messages(history, use_working, use_long_term)
    meta['context'] = {'sent_message_ids': [m['id'] for m in agent._selected(history)],
                       'sent_messages': len(context),
                       'facts_revision': agent._store.memory()['revisions'],
                       'input_text_tokens_estimate': agent._counter.history(context) +
                           agent._counter.count(agent._answer_instructions()),
                       'task_state': None, 'dialogue_id': agent._dialogue_id,
                       'task_id': agent._task_id,
                       'memory_refs': [dict(id=entry['id'], revision=entry['revision'])
                           for entries in agent._layers(use_working, use_long_term).values()
                           for entry in entries],
                       'selection': {'working': use_working, 'long_term': use_long_term}}
    profile = agent._profile()
    if profile:
        meta['context'].update(profile_refs=profile['refs'],
                               profile_id=getattr(agent, 'profile_id', None))
    agent._store.pending_metadata(parent_id, meta)
    result = tool_loop.run(agent, context, parent_id, meta, ip, op)
    agent._store.pending_result(parent_id, agent._record(result, meta))
    agent._store.finish(parent_id, agent._record(result, meta),
                        result.text if result.status == 'ok' else None)
    return result

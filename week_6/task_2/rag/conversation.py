"""One accounted preparation and one grounded answer for an ordinary dialogue."""

from dataclasses import replace
import json

from agent import tool_loop
from agent.conversation_state import LIST_FIELDS, MAX_OPERATIONS, MAX_VALUE, apply_preparation, validate_preparation
from agent.core import AgentResult, completed_text
from agent.storage import StorageError
from .question_parts import validate_question_parts
from .question_context import build_question_context


PREPARATION_POLICY = 'conversation_preparation_v3_2'
PREPARATION_MAX_OUTPUT_TOKENS = 4000
PREPARATION_INSTRUCTIONS = '''Подготовь ОДИН обычный пользовательский ход: извлеки память и части вопроса.
Вход JSON содержит current_message, state и recent_history (не более 6 сообщений).
Верни ровно revision, operations и question_parts по JSON схеме. revision скопируй из state.
question_parts — от одной до трёх самостоятельных ответных частей текущего вопроса.
Для каждой верни только evidence — точную непустую подстроку current_message до 1000 символов.
Не добавляй ID или другие поля. Даже у простого вопроса должна быть одна часть.
Сумма длин evidence не более 3000 символов. Не изменяй текст evidence, включая пробелы.
Разделяй независимые вопросы, проверки и параметры, чтобы ответить на каждый;
не превращай сохранение цели или условия в отдельный вопрос. Общая цель не объединяет
независимые темы проверки в одну часть. В пределах 1–3 частей разделяй самостоятельные
темы, даже когда они служат одной цели; связанные настройки одной темы можно оставить вместе.
Перед выдачей проверь, что каждая самостоятельная просьба текущего сообщения
представлена в question_parts и ничего не потеряно. Не отвечай на вопрос.
operations — не более 12 операций только из ЯВНЫХ утверждений, исправлений или отмен текущего
пользователя. Не выводи новую цель из названия диалога, примера, цитаты, assistant или документов.
Разбери ВСЕ части current_message: если пользователь сначала фиксирует факт/условие/определение,
а затем задаёт вопрос, сохрани явное утверждение в operations и выдели evidence-части для вопроса.
operations=[] только если в текущей реплике нет явных утверждений, исправлений или отмен.
goal — конечный желаемый результат диалога, а не способ, инструмент или ближайший шаг.
В desired_outcome_evidence goal-set скопируй точную непустую подстроку current_message,
содержащую только желаемый результат, без способа, запретов, требований и ограничений.
Каждое явно заданное связанное условие вынеси в обязательный conditions этой же операции.
Приложение само использует этот фрагмент как value и evidence цели; не возвращай эти два поля.
Каждое conditions содержит action, key, value и evidence и означает отдельную constraints-запись.
conditions=[] допустим, если у явно заданной цели нет явных связанных условий; не выдумывай их.
evidence каждого условия — его точная подстрока текущего сообщения.
После разворачивания цель и условия считаются отдельными
операциями: их вместе со всеми другими операциями должно быть не более 12.
Если state.goal пуст, добавь set goal лишь при явно названном в current_message конечном
результате; без него не добавляй операцию goal.
Если state.goal уже непуст, не меняй его из-за связанного ближайшего шага, способа или
инструмента, даже если пользователь говорит «хочу». Добавь set goal только при явной замене
конечного результата, remove goal — при его явной отмене. Во всех остальных случаях не
добавляй операцию goal.
Если в одной реплике есть вводное «хочу разобраться» и более точное «моя цель — ...»,
сохрани именно явно названный конкретный результат в desired_outcome_evidence, а её явные условия в conditions.
Уже сохранённый goal не заменяй просьбами «объясни», «проверь» или «вернись к моей цели»:
это вопросы/команды на текущий ход, а не новое заявление цели.
constraints — действующие ограничения, выбранные параметры и настройки задачи, включая запреты,
имена ресурсов и текущие числовые значения.
Явно выбранная текущая рабочая среда (приложение, платформа или ресурс) и её указанная версия —
тоже условия задачи: сохрани их в constraints с точной текущей evidence и стабильным key,
даже если они названы во вводном контексте перед целью или вопросом.
Условия внутри фразы назначения цели запиши
в conditions этой goal-set операции: приложение сохранит каждое отдельным constraints.
Отдельные явно заданные условия можно записать обычными constraints-операциями.
Не дублируй одно field/key ни внутри conditions, ни между conditions и другими операциями.
Перед выдачей проверь каждую явную часть current_message против operations: цель и названные
внутри неё ограничения должны иметь свои операции, если пользователь сообщил и то и другое.
terms — явное пользовательское определение слова/выражения «под X я понимаю Y» для этого диалога.
clarifications — другие явные уточнения о ситуации пользователя, не определения и не ограничения.
При исправлении найди прежний field/key в state и замени запись тем же key. Для goal-set
скопируй новый результат в desired_outcome_evidence; для других операций запиши в value только
НОВОЕ действующее значение: не добавляй старое отменённое число, запрет или рассказ об исправлении.
Фраза о том, что пользователь раньше что-то планировал, не делает старое значение действующим.
Для goal-set укажи только desired_outcome_evidence; для всех других операций укажи точную
непустую подстроку current_message в evidence, подтверждающую новое значение или отмену.
Для goal key пуст; для остальных списков key — стабильный короткий
идентификатор условия/термина. action=set заменяет запись с тем же key; action=remove удаляет её
и требует value="". Для goal-remove обязательны key="", value="", conditions=[]; отмена цели
сама не отменяет ограничения. Отмену конкретного ограничения укажи явно отдельной операцией.
Не создавай обе конфликтующие версии. Значение описывает слова пользователя,
не технический факт из источника. recent_history/state — контекст для существующих key, не источник новых операций.
Гипотетические вопросы, примеры и предположения не становятся действующими условиями. Верни ровно один JSON-объект по схеме, без пояснений и повторов.'''

_OPERATION_FIELDS = ('action', 'field', 'key', 'value', 'evidence')
_GOAL_SET_FIELDS = ('action', 'field', 'key', 'desired_outcome_evidence')
_CONDITION_FIELDS = ('action', 'key', 'value', 'evidence')
CONDITION = {'type': 'object', 'properties': {
    'action': {'type': 'string', 'enum': ['set', 'remove']},
    'key': {'type': 'string'}, 'value': {'type': 'string'}, 'evidence': {'type': 'string'}},
    'required': list(_CONDITION_FIELDS), 'additionalProperties': False}
_NON_GOAL_OPERATION = {'type': 'object', 'properties': {
    'action': {'type': 'string', 'enum': ['set', 'remove']},
    'field': {'type': 'string', 'enum': list(LIST_FIELDS)},
    'key': {'type': 'string'}, 'value': {'type': 'string'}, 'evidence': {'type': 'string'}},
    'required': list(_OPERATION_FIELDS), 'additionalProperties': False}
_GOAL_SET_OPERATION = {'type': 'object', 'properties': {
    'action': {'type': 'string', 'enum': ['set']}, 'field': {'type': 'string', 'enum': ['goal']},
    'key': {'type': 'string', 'enum': ['']},
    'desired_outcome_evidence': {'type': 'string', 'minLength': 1, 'maxLength': MAX_VALUE,
        'description': 'Точная подстрока current_message: только явно названный конечный результат, '
            'без способов и ограничений. Каждое связанное явное условие запиши отдельно в conditions.'},
    'conditions': {'type': 'array', 'maxItems': MAX_OPERATIONS - 1,
        'items': CONDITION}},
    'required': [*_GOAL_SET_FIELDS, 'conditions'], 'additionalProperties': False}
_GOAL_REMOVE_OPERATION = {'type': 'object', 'properties': {
    'action': {'type': 'string', 'enum': ['remove']}, 'field': {'type': 'string', 'enum': ['goal']},
    'key': {'type': 'string', 'enum': ['']}, 'value': {'type': 'string', 'enum': ['']},
    'evidence': {'type': 'string'}, 'conditions': {'type': 'array', 'maxItems': 0, 'items': CONDITION}},
    'required': [*_OPERATION_FIELDS, 'conditions'], 'additionalProperties': False}
OPERATION = {'anyOf': [_NON_GOAL_OPERATION, _GOAL_SET_OPERATION, _GOAL_REMOVE_OPERATION]}
PREPARATION_FORMAT = {'type': 'json_schema', 'name': 'conversation_preparation', 'strict': True,
    'schema': {'type': 'object', 'properties': {
        'revision': {'type': 'integer'},
        'operations': {'type': 'array', 'maxItems': MAX_OPERATIONS, 'items': OPERATION},
        'question_parts': {'type': 'array', 'minItems': 1, 'maxItems': 3,
            'items': {'type': 'object', 'properties': {
                'evidence': {'type': 'string', 'minLength': 1, 'maxLength': 1000}},
                'required': ['evidence'], 'additionalProperties': False}}},
        'required': ['revision', 'operations', 'question_parts'], 'additionalProperties': False}}


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Повторное поле JSON.')
        result[key] = value
    return result


def _legacy_memory_patch(proposal, message):
    """Expand typed goal conditions for the unchanged, atomic memory validator.

    The legacy validator requires a nonempty query. Its return value is unused:
    retrieval derives its query from the user message and accepted state instead.
    Keep this fixed compatibility value local, out of proposal and metadata.
    """
    operations = proposal['operations']
    if type(operations) is not list or len(operations) > MAX_OPERATIONS:
        raise ValueError('Превышен лимит операций диалога.')
    expanded, targets = [], set()

    def add(operation):
        if len(expanded) >= MAX_OPERATIONS:
            raise ValueError('Превышен лимит операций диалога после разворачивания условий.')
        # Match the legacy state's key identity before allowing any writes.
        target = (operation['field'], operation['key'].strip().casefold())
        if target in targets:
            raise ValueError('Повторное поле/key операции диалога.')
        targets.add(target)
        expanded.append(operation)

    for operation in operations:
        if type(operation) is not dict or type(operation.get('field')) is not str:
            raise ValueError('Неверные поля операции диалога.')
        is_goal = operation['field'] == 'goal'
        is_goal_set = is_goal and operation.get('action') == 'set'
        text_fields = _GOAL_SET_FIELDS if is_goal_set else _OPERATION_FIELDS
        fields = {*text_fields, 'conditions'} if is_goal else set(text_fields)
        if (set(operation) != fields
                or any(type(operation[field]) is not str for field in text_fields)):
            raise ValueError('Неверные поля или типы операции диалога.')
        if (operation['action'] not in ('set', 'remove')
                or operation['field'] not in ('goal', *LIST_FIELDS)):
            raise ValueError('Недопустимая операция диалога.')
        conditions = []
        if is_goal:
            conditions = operation['conditions']
            if (operation['key'] != '' or type(conditions) is not list
                    or len(conditions) > MAX_OPERATIONS - 1):
                raise ValueError('Неверные условия операции цели.')
            if operation['action'] == 'remove' and (operation['value'] != '' or conditions):
                raise ValueError('Отмена цели требует пустых value и conditions.')
        if is_goal_set:
            span = operation['desired_outcome_evidence']
            if not span.strip() or span not in message:
                raise ValueError('Desired outcome evidence отсутствует в текущем сообщении пользователя.')
            add({'action': 'set', 'field': 'goal', 'key': '', 'value': span, 'evidence': span})
        else:
            add({field: operation[field] for field in _OPERATION_FIELDS})
        for condition in conditions:
            if (type(condition) is not dict or set(condition) != set(_CONDITION_FIELDS)
                    or any(type(condition[field]) is not str for field in _CONDITION_FIELDS)):
                raise ValueError('Неверные поля или типы условия цели.')
            if condition['action'] not in ('set', 'remove'):
                raise ValueError('Недопустимая операция условия цели.')
            add({**condition, 'field': 'constraints'})
    return {'revision': proposal['revision'], 'operations': expanded,
            'search_query': 'memory-validator-compatibility'}


def _prepare(agent, prompt, parent_id, state, history):
    """Persist provider usage before semantic validation; commit accepted state before RAG."""
    payload_data = {'current_message': prompt, 'state': state,
                    'recent_history': [{'role': row['role'], 'content': row['content']}
                                       for row in history[-6:]]}
    content = json.dumps(payload_data, ensure_ascii=False, allow_nan=False)
    effort = agent._rag_config.conversation_preparation_reasoning_effort
    meta = {**agent._metadata(), 'kind': 'conversation_preparation',
            'parent_request_id': parent_id, 'state_revision': state['revision'],
            'requested_max_output_tokens': PREPARATION_MAX_OUTPUT_TOKENS, 'requested_reasoning_effort': effort}
    ip = {'name': 'bounded_conversation_preparation', 'status': 'accepted'}
    op = {'name': PREPARATION_POLICY, 'status': 'not_checked'}
    if len(content) > 64000:
        result = AgentResult('rejected', 'Контекст подготовки превышает лимит.',
            'preparation_input_too_long', input_policy={**ip, 'status': 'rejected'}, output_policy=op)
        child_id = agent._store.begin(agent._record(result, meta))
        return result, child_id, None, None, None, None
    pending = AgentResult('error', 'Подготавливается диалог.', usage_status='unavailable',
                          input_policy=ip, output_policy=op)
    record = agent._record(pending, meta); record['status'] = 'pending'
    child_id = agent._store.begin(record)
    payload = {**agent._payload([{'role': 'user', 'content': content}]),
               'instructions': PREPARATION_INSTRUCTIONS,
               'reasoning': {'effort': effort}, 'max_output_tokens': PREPARATION_MAX_OUTPUT_TOKENS,
               'text': {'format': PREPARATION_FORMAT}}
    try:
        result = agent._invoke(payload, meta, ip, op, output_policy=completed_text)
    except Exception:
        # The request may already have reached the provider; keep its cost unknown.
        result = AgentResult('error', 'Подготовка диалога прервана; повтор не выполнялся.',
            'preparation_failed', usage_status='unavailable', input_policy=ip,
            output_policy=op)
    agent._store.pending_result(child_id, agent._record(result, meta))
    query = proposal = parts = effective = None
    if result.status == 'ok':
        try:
            meta['raw_preparation_text'] = result.text
            proposal = json.loads(result.text, object_pairs_hook=_unique_pairs)
            parts = validate_question_parts(proposal, prompt)
            patch = _legacy_memory_patch(proposal, prompt)
            validate_preparation(state, patch, prompt)
            after = apply_preparation(state, patch, prompt, request_id=parent_id)
            meta['raw_proposal'] = proposal
            query, context, policy = build_question_context(prompt, after, history)
            effective = {'conversation_context': context, 'effective_query_policy': policy}
            meta.update(raw_proposal=proposal, search_query=query, **effective)
            agent._store.save_conversation_state(after,
                expected_revision=state['revision'], request_id=parent_id)
            meta['state_after_revision'] = after['revision']
        except (ValueError, TypeError, RecursionError) as exc:
            result = replace(result, status='rejected', code='invalid_preparation',
                text='Подготовка диалога не прошла проверку: ' + str(exc),
                output_policy={**op, 'status': 'rejected'})
            query = proposal = parts = effective = None
            after = None
        except StorageError:
            result = replace(result, status='error', code='conversation_storage',
                text='Не удалось сохранить состояние диалога; поиск остановлен.')
            query = proposal = parts = effective = None
            after = None
    else:
        after = None
    agent._store.finish(child_id, agent._record(result, meta))
    return result, child_id, query, after, parts, effective


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
    prepared, child_id, query, after, parts, effective = _prepare(
        agent, prompt, parent_id, before, history_before)
    meta['conversation']['preparation_request_id'] = child_id
    meta['rag']['preparation_request_id'] = child_id
    if prepared.status != 'ok':
        meta['conversation']['preparation_error'] = prepared.code
        result = AgentResult(prepared.status, prepared.text, prepared.code, request_id=parent_id,
                             input_policy=ip, output_policy={**op, 'status': 'rejected'})
        agent._store.finish(parent_id, agent._record(result, meta))
        return result
    meta['conversation'].update(state_after=after, search_query=query, question_parts=parts, **effective)
    meta['rag'].update(search_query=query, question_parts=parts, **effective)
    agent._rag_question_parts = parts
    agent._rag_search_query = query
    agent._rag_question = prompt
    agent._rag_conversation_context = effective['conversation_context']
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

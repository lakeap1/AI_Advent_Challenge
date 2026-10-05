"""Bounded Responses loop; every MCP call is chosen by the model."""
import hashlib
import json


class OrchestrationError(RuntimeError):
    pass


def _bytes(content):
    if not isinstance(content, str) or not content.strip():
        raise OrchestrationError('Пустой или повреждённый отчёт.')
    try:
        return content.encode('utf-8')
    except UnicodeEncodeError:
        raise OrchestrationError('Отчёт содержит некорректный Unicode.') from None


def _call(response, used_ids):
    if not isinstance(response, dict) or response.get('status') != 'completed' or response.get('error') or not isinstance(response.get('output'), list):
        raise OrchestrationError('Модель вернула незавершённый или повреждённый ответ.')
    calls = [item for item in response['output'] if isinstance(item, dict) and item.get('type') == 'function_call']
    if len(calls) > 1 or any(not isinstance(item, dict) or item.get('type') not in ('function_call','reasoning','message') for item in response['output']):
        raise OrchestrationError('Параллельные или неизвестные ответы модели запрещены.')
    if any(item.get('type') == 'reasoning' and item.get('status') not in (None,'completed') for item in response['output']):
        raise OrchestrationError('Шаг рассуждения не завершён.')
    if not calls:
        if not any(item.get('type') == 'message' for item in response['output']):
            raise OrchestrationError('Финальный ответ модели отсутствует.')
        return None
    if any(item.get('type') == 'message' for item in response['output']):
        raise OrchestrationError('Сообщение модели смешано с MCP-вызовом.')
    item = calls[0]
    call_id = item.get('call_id')
    if item.get('status') not in (None, 'completed') or not isinstance(call_id, str) or not call_id or call_id in used_ids:
        raise OrchestrationError('Повторный или повреждённый call_id.')
    name = item.get('name')
    if not isinstance(name, str):
        raise OrchestrationError('Имя MCP-инструмента повреждено.')
    try:
        args = json.loads(item['arguments'])
    except (KeyError, ValueError, TypeError):
        raise OrchestrationError('Аргументы MCP не являются JSON.') from None
    if not isinstance(args, dict):
        raise OrchestrationError('Аргументы MCP должны быть объектом.')
    return name, call_id, args


async def execute_flow(router, choose, question, *, before_save=None, max_calls=10, max_steps=12, on_event=None):
    if not isinstance(question, str) or not question.strip() or len(question) > 2000:
        raise OrchestrationError('Вопрос должен содержать от 1 до 2000 символов.')
    tools = await router.discover()
    tool_names = {item['name'] for item in tools}
    required = {'research__lookup_wikipedia','research__search_stackexchange',
                'processing__compare_sources','processing__prepare_report',
                'library__save_report','library__read_report'}
    if not required <= tool_names:
        raise OrchestrationError('Каталог MCP неполный.')
    events = []
    def emit(value):
        events.append(value)
        if on_event:
            on_event(value)
    messages = [{'role': 'user', 'content': question}]
    materials = {}
    comparison = None
    report = None
    saved = None
    verified = False
    used_ids = set()
    for step in range(1, max_steps + 1):
        choice = 'auto' if step < max_steps and len(used_ids) < max_calls else 'none'
        response = await choose(messages, tools, choice)
        emit({'type': 'model_step', 'step': step, 'tool_choice': choice, 'response': response})
        selected = _call(response, used_ids)
        if selected is None:
            if not verified:
                raise OrchestrationError('Модель завершила ответ до проверенного чтения отчёта.')
            return {'verified': True, 'saved': saved, 'events': events}
        name, call_id, args = selected
        if choice == 'none' or len(used_ids) >= max_calls:
            raise OrchestrationError('Лимит MCP-вызовов исчерпан.')
        if name not in tool_names:
            raise OrchestrationError('Модель выбрала неизвестный MCP-инструмент.')
        if verified:
            raise OrchestrationError('Повторный вызов после проверки отчёта запрещён.')
        if name.startswith('research__'):
            if name == 'research__lookup_wikipedia' and any(m.get('provider') == 'wikipedia' for m in materials.values()):
                raise OrchestrationError('Источник Wikipedia уже получен.')
            if name == 'research__search_stackexchange' and any(m.get('provider') == 'stackexchange' for m in materials.values()):
                raise OrchestrationError('Источник Stack Exchange уже получен.')
            if comparison is not None:
                raise OrchestrationError('Поиск после сравнения запрещён.')
        elif name == 'processing__compare_sources':
            ids = args.get('material_ids')
            if comparison is not None or not isinstance(ids, list) or len(ids) != 2 or set(ids) != set(materials) or {materials[i]['provider'] for i in ids} != {'wikipedia','stackexchange'}:
                raise OrchestrationError('Сравнение требует двух фактических источников этого запуска.')
        elif name == 'processing__prepare_report':
            if comparison is None or report is not None or args.get('material_ids') != comparison['material_ids']:
                raise OrchestrationError('Отчёт требует фактического сравнения тех же материалов.')
            _bytes(args.get('text'))
        elif name == 'library__save_report':
            if report is None or saved is not None or args.get('report_id') != report['report_id']:
                raise OrchestrationError('Сохранение требует принятого отчёта этого запуска.')
            if before_save:
                denied = before_save(report['content'])
                if denied:
                    raise OrchestrationError(str(denied))
        elif name == 'library__read_report':
            if saved is None or args.get('report_id') != saved['report_id']:
                raise OrchestrationError('Чтение требует сохранённого отчёта этого запуска.')
        else:
            raise OrchestrationError('Неизвестная зависимость MCP-инструмента.')
        used_ids.add(call_id)
        event = {'type': 'call', 'sequence': len(used_ids), 'server': name.split('__',1)[0], 'tool': name.split('__',1)[1],
                 'qualified_name': name, 'call_id': call_id, 'arguments': args, 'status': 'running'}
        emit(event)
        try:
            result = await router.call(name, args)
            if not isinstance(result, dict):
                raise OrchestrationError('MCP вернул повреждённый результат.')
            if name.startswith('research__'):
                expected = 'wikipedia' if name.endswith('lookup_wikipedia') else 'stackexchange'
                mid = result.get('material_id')
                if result.get('provider') != expected or not isinstance(mid, str) or not mid or mid in materials or not result.get('sources'):
                    raise OrchestrationError('Источник MCP не соответствует вызову.')
                materials[mid] = result
            elif name == 'processing__compare_sources':
                if result.get('material_ids') != args['material_ids'] or not result.get('comparison_id'):
                    raise OrchestrationError('Сравнение подменило материалы.')
                comparison = result
            elif name == 'processing__prepare_report':
                if result.get('material_ids') != args['material_ids'] or not result.get('report_id'):
                    raise OrchestrationError('Отчёт подменил материалы.')
                _bytes(result.get('content'))
                report = result
            elif name == 'library__save_report':
                if result.get('report_id') != report['report_id'] or result.get('content') != report['content']:
                    raise OrchestrationError('Сохранённый текст не совпадает с принятым отчётом.')
                data = _bytes(result['content'])
                if result.get('sha256') != hashlib.sha256(data).hexdigest() or result.get('bytes_written') != len(data):
                    raise OrchestrationError('Контрольная сумма или размер файла неверны.')
                saved = result
            else:
                data = _bytes(result.get('content'))
                if any(result.get(k) != saved.get(k) for k in ('report_id','filename','content','sha256','bytes_written')) or hashlib.sha256(data).hexdigest() != result.get('sha256') or len(data) != result.get('bytes_written'):
                    raise OrchestrationError('Обратное чтение не совпало с сохранённым файлом.')
                verified = True
            event.update(result=result, status='success')
            if on_event:
                on_event(event)
        except Exception as exc:
            event.update(status='error', error=str(exc)[:500])
            if on_event:
                on_event(event)
            raise
        messages.extend(response['output'])
        messages.append({'type': 'function_call_output', 'call_id': call_id, 'output': json.dumps(result, ensure_ascii=False)})
    raise OrchestrationError('Лимит шагов модели исчерпан.')

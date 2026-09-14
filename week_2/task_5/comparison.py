"""Воспроизводимое сравнение: реальные независимые диалоги, без готовых ответов."""
import argparse
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import os
from uuid import uuid4
from dotenv import load_dotenv
from agent import create_agent
from agent.tokens import accounting

MODES = ('sliding', 'facts', 'branching')
CONTROL = 'Напомни текущие условия моего проекта с учётом всех исправлений. Только JSON с ключами project, tool, engine, texture, accent, ban. Неизвестное обозначь null, не угадывай.'
SCENARIO = [
    'Мой проект «Этюд-427»: работаю в Blender, проверяю в Godot. Текстура 512, акцент бирюзовый, фототекстуры запрещены. Почему силуэт важен? Ответ до 35 слов.',
    'Чем растровая графика отличается от векторной? До 35 слов.',
    'Зачем 3D-модели UV-развёртка? До 35 слов.',
    'Что меняет roughness в материале? До 35 слов.',
    'Почему нормали влияют на освещение? До 35 слов.',
    CONTROL,
    'Исправление моих условий: теперь текстура 1024, акцент янтарный. Остальное без изменений. Зачем mipmaps? До 35 слов.',
    'Что такое texel density и зачем её выравнивать? До 35 слов.',
    'Чем ортографическая камера отличается от перспективной? До 35 слов.',
    'Почему цветовую текстуру и карту roughness читают в разных цветовых пространствах? До 35 слов.',
    'Как размер источника света влияет на тень? До 35 слов.',
    CONTROL,
]
EXPECTED = {
    6: dict(project='Этюд-427', tool='Blender', engine='Godot', texture=512, accent='бирюзовый', ban='фототекстуры'),
    12: dict(project='Этюд-427', tool='Blender', engine='Godot', texture=1024, accent='янтарный', ban='фототекстуры'),
}

RECORDING_FILES = {mode: f'{mode}-session.json' for mode in MODES}
BRANCH_SCENARIO = (
    ('Мягкий свет', 'В этой ветке выбираю мягкий свет большой площадной лампы. Как получить мягкую тень? До 35 слов.'),
    ('Жёсткий свет', 'В этой ветке выбираю жёсткий свет маленького источника. Как получить резкую тень? До 35 слов.'),
)
BRANCH_PROBE = 'Какой свет выбран именно в этой ветке и каким размером источника он получается? До 25 слов.'
RESULT_FIELDS = ('status', 'code', 'usage', 'cost_usd', 'usage_status', 'input_policy', 'output_policy')


def check_memory(text, expected):
    try: value = json.loads(text)
    except (ValueError, TypeError): return {'passed': False, 'correct_fields': [], 'expected': expected, 'actual': text}
    # Canonical strings may vary in case; ban may state the prohibition explicitly.
    def matches(key, actual, want):
        if key == 'texture':
            return (type(actual) is int and actual == want) or (isinstance(actual, str) and actual == str(want))
        if not isinstance(actual, str): return False
        if key == 'ban': return actual.casefold() in ('фототекстуры', 'фототекстуры запрещены', 'запрет фототекстур')
        return actual.casefold() == want.casefold()
    fields = [k for k,v in expected.items() if isinstance(value,dict) and matches(k,value.get(k),v)]
    return dict(passed=len(fields)==len(expected), correct_fields=fields, expected=expected, actual=value)


def _require(condition, message):
    if not condition:
        raise ValueError(f'Некорректная UI-запись: {message}')


def _load_recording(path, expected_mode):
    try:
        session = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ValueError(f'Не удалось прочитать {path.name}: {exc}') from exc
    _require(isinstance(session, dict), f'{path.name}: корень должен быть JSON-объектом')
    _require(session.get('mode') == expected_mode, f'{path.name}: ожидался mode={expected_mode}')
    _require(isinstance(session.get('run_id'), str) and session['run_id'].strip(), f'{path.name}: отсутствует run_id')
    return session


def _validate_result(result, prompt, mode, used_request_ids, expected_chat_id=None, expected_branch_id=None):
    _require(isinstance(result, dict), f'{mode}: result должен быть объектом')
    _require(result.get('status') == 'ok', f'{mode}: ответ для «{prompt}» не имеет status=ok')
    _require(isinstance(result.get('text'), str) and result['text'].strip(), f'{mode}: пустой текст ответа')
    request_id = result.get('request_id')
    _require(type(request_id) is int and request_id > 0, f'{mode}: неверный request_id')
    _require(request_id not in used_request_ids, f'{mode}: request_id={request_id} использован повторно')

    state = result.get('state')
    _require(isinstance(state, dict), f'{mode}: result.state отсутствует')
    chat_id = state.get('chat_id')
    _require(isinstance(chat_id, str) and chat_id.strip(), f'{mode}: state.chat_id отсутствует')
    if expected_chat_id is not None:
        _require(chat_id == expected_chat_id, f'{mode}: chat_id изменился внутри записи')

    messages = state.get('messages')
    requests = state.get('requests')
    _require(isinstance(messages, list) and isinstance(requests, list), f'{mode}: state не содержит messages/requests')
    request_messages = [item for item in messages if isinstance(item, dict) and item.get('request_id') == request_id]
    _require(
        [(item.get('role'), item.get('content')) for item in request_messages]
        == [('user', prompt), ('assistant', result['text'])],
        f'{mode}: prompt или ответ подменён относительно state.messages для request_id={request_id}',
    )
    request_records = [item for item in requests if isinstance(item, dict) and item.get('id') == request_id]
    _require(len(request_records) == 1, f'{mode}: request_id={request_id} отсутствует или дублируется в state.requests')
    record = request_records[0]
    for field in RESULT_FIELDS:
        _require(field in result and record.get(field) == result[field], f'{mode}: поле {field} не совпадает с state.requests')
    metadata = record.get('metadata')
    _require(isinstance(metadata, dict), f'{mode}: metadata запроса отсутствует')
    _require(metadata.get('mode') == mode and metadata.get('kind') == 'answer', f'{mode}: запрос относится к другому режиму или типу')
    if expected_branch_id is not None:
        _require(metadata.get('branch_id') == expected_branch_id, f'{mode}: ответ относится к другой ветке')
        _require(state.get('active_branch') == expected_branch_id, f'{mode}: snapshot снят не в ожидаемой ветке')
    context = metadata.get('context')
    _require(isinstance(context, dict), f'{mode}: context запроса отсутствует')
    for name in ('summary', 'token_accounting', 'memory'):
        _require(isinstance(state.get(name), dict), f'{mode}: state.{name} отсутствует')

    used_request_ids.add(request_id)
    clean_result = {key: deepcopy(value) for key, value in result.items() if key != 'state'}
    return clean_result, state, chat_id, context, record


def _same(values, label):
    first = values[0]
    _require(all(value == first for value in values[1:]), f'{label} различается между режимами')
    return deepcopy(first)


def _ledger_aggregates(requests, mode):
    try:
        token_accounting = accounting(requests)
        called = [item for item in requests if item['usage_status'] != 'not_requested']
        unknown_cost_requests = sum(item['cost_usd'] is None for item in called)
        known_cost = sum(
            (Decimal(item['cost_usd']) for item in called if item['cost_usd'] is not None),
            Decimal(0),
        )
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        raise ValueError(f'Некорректная UI-запись: {mode}: ledger содержит некорректные usage или cost') from exc
    summary = {
        'known_cost_usd': format(known_cost, 'f'),
        'cost_complete': unknown_cost_requests == 0,
        'unknown_cost_requests': unknown_cost_requests,
        'api_requests': len(called),
    }
    return summary, token_accounting


def _validate_ledger(state, answer_results, mode, *, require_facts=False):
    requests = state.get('requests')
    _require(isinstance(requests, list), f'{mode}: итоговый ledger отсутствует')
    ids = [item.get('id') for item in requests if isinstance(item, dict)]
    _require(len(ids) == len(requests) and all(type(item_id) is int for item_id in ids), f'{mode}: неверные request id в ledger')
    _require(len(ids) == len(set(ids)), f'{mode}: request id дублируются в ledger')

    answers = [item for item in requests if isinstance(item.get('metadata'), dict) and item['metadata'].get('kind') == 'answer']
    facts = [item for item in requests if isinstance(item.get('metadata'), dict) and item['metadata'].get('kind') == 'facts']
    answer_ids = set(answer_results)
    _require({item['id'] for item in answers} == answer_ids and len(answers) == len(answer_ids), f'{mode}: состав answer ledger не совпадает с записанными ответами')
    for answer in answers:
        result = answer_results[answer['id']]
        for field in RESULT_FIELDS:
            _require(answer.get(field) == result.get(field), f'{mode}: поле {field} answer ledger не совпадает с записанным result')
    if require_facts:
        parents = [item['metadata'].get('parent_request_id') for item in facts]
        _require(len(facts) == len(answer_ids), f'{mode}: для каждого ответа требуется ровно один facts-вызов')
        _require(len(set(parents)) == len(parents) and set(parents) == set(answer_ids), f'{mode}: facts-вызовы неверно связаны с answer request')
        _require(len(requests) == len(answers) + len(facts), f'{mode}: ledger содержит посторонние вызовы')
    else:
        _require(not facts and len(requests) == len(answers), f'{mode}: ledger содержит посторонние вызовы')

    expected_summary, expected_tokens = _ledger_aggregates(requests, mode)
    _require(state.get('summary') == expected_summary, f'{mode}: summary не соответствует ledger')
    _require(state.get('token_accounting') == expected_tokens, f'{mode}: token_accounting не соответствует ledger')


def export_recorded_sessions(recording_dir, output):
    """Собрать renderer-совместимый отчёт только из JSON, записанных после UI-ответов."""
    recording_dir, output = Path(recording_dir), Path(output)
    if output.exists():
        raise ValueError('Результат уже существует: укажите новое имя --output.')

    sessions = {
        mode: _load_recording(recording_dir / filename, mode)
        for mode, filename in RECORDING_FILES.items()
    }
    run_id = _same([session['run_id'] for session in sessions.values()], 'run_id')
    modes = {}
    chat_ids = {}
    final_states = {}
    pricing_values = []

    for mode, session in sessions.items():
        turns = session.get('turns')
        _require(isinstance(turns, list) and len(turns) == len(SCENARIO), f'{mode}: требуется ровно {len(SCENARIO)} ходов')
        used_request_ids = set()
        chat_id = None
        answer_results = {}
        exported_turns = []
        final_state = None
        final_record = None
        for number, (item, expected_prompt) in enumerate(zip(turns, SCENARIO), 1):
            _require(isinstance(item, dict), f'{mode}: ход {number} должен быть объектом')
            _require(item.get('number') == number, f'{mode}: нарушена нумерация хода {number}')
            _require(item.get('prompt') == expected_prompt, f'{mode}: сценарий хода {number} не совпадает')
            result, state, current_chat_id, context, record = _validate_result(
                item.get('result'), expected_prompt, mode, used_request_ids, chat_id
            )
            chat_id = current_chat_id
            final_state = state
            final_record = record
            answer_results[result['request_id']] = result
            exported_turns.append({
                'number': number,
                'prompt': expected_prompt,
                'result': result,
                'check': check_memory(result['text'], EXPECTED[number]) if number in EXPECTED else None,
                'facts': deepcopy(state['memory'].get('facts', {})),
                'context': deepcopy(context),
            })

        _require(len(used_request_ids) == len(SCENARIO), f'{mode}: ответы сценария дублируются')
        _require(isinstance(final_state.get('summary'), dict), f'{mode}: итоговый summary отсутствует')
        _require(isinstance(final_state.get('token_accounting'), dict), f'{mode}: итоговый token_accounting отсутствует')
        _validate_ledger(final_state, answer_results, mode, require_facts=mode == 'facts')
        modes[mode] = {
            'turns': exported_turns,
            'summary': deepcopy(final_state['summary']),
            'token_accounting': deepcopy(final_state['token_accounting']),
            'requests': deepcopy(final_state['requests']),
        }
        chat_ids[mode] = chat_id
        final_states[mode] = final_state
        pricing_values.append(final_record['metadata'].get('pricing'))

    _require(len(set(chat_ids.values())) == len(MODES), 'режимы должны быть записаны в трёх независимых чатах')
    contexts = [final_states[mode]['context'] for mode in MODES]
    memories = [final_states[mode]['memory'] for mode in MODES]
    conditions = {
        'model': _same([context.get('model') for context in contexts], 'model'),
        'reasoning_effort': _same([context.get('reasoning_effort') for context in contexts], 'reasoning_effort'),
        'keep_last_messages': _same([memory.get('keep_last_messages') for memory in memories], 'keep_last_messages'),
        'max_output_tokens': _same([context.get('max_output_tokens') for context in contexts], 'max_output_tokens'),
        'pricing': _same(pricing_values, 'pricing'),
        'note': (
            'Три режима записаны последовательно в UI. Кеш провайдера не контролировался. '
            'Дополнительные ветки продолжили показанный branching-чат; общие итоги взяты из последнего snapshot этого чата.'
        ),
    }

    branch_session = sessions['branching']
    experiment = branch_session.get('branching_experiment')
    _require(isinstance(experiment, dict), 'branching: отсутствует branching_experiment')
    checkpoint = experiment.get('checkpoint')
    _require(
        isinstance(checkpoint, dict) and type(checkpoint.get('id')) is int
        and checkpoint.get('name') == 'Два варианта освещения',
        'branching: неверная контрольная точка',
    )
    recorded_branches = experiment.get('branches')
    _require(isinstance(recorded_branches, list) and len(recorded_branches) == len(BRANCH_SCENARIO), 'branching: требуются две ветки')
    branch_answer_results = {turn['result']['request_id']: turn['result'] for turn in modes['branching']['turns']}
    used_request_ids = set(branch_answer_results)
    used_branch_ids = set()
    exported_branches = []
    final_probe_state = None
    for item, (expected_name, expected_prompt) in zip(recorded_branches, BRANCH_SCENARIO):
        _require(isinstance(item, dict), 'branching: ветка должна быть объектом')
        branch = item.get('branch')
        _require(isinstance(branch, dict), 'branching: отсутствует описание ветки')
        branch_id = branch.get('id')
        _require(type(branch_id) is int and branch_id not in used_branch_ids, 'branching: неверный или повторный branch id')
        _require(branch.get('name') == expected_name and item.get('prompt') == expected_prompt, f'branching: подменена ветка {expected_name}')
        used_branch_ids.add(branch_id)
        result, result_state, current_chat_id, _, _ = _validate_result(
            item.get('result'), expected_prompt, 'branching', used_request_ids, chat_ids['branching'], branch_id
        )
        branch_answer_results[result['request_id']] = result
        _require(branch in result_state.get('branches', []), f'branching: ветка {expected_name} отсутствует в snapshot')
        _require(checkpoint in result_state.get('checkpoints', []), f'branching: checkpoint отсутствует в snapshot ветки {expected_name}')
        probe = item.get('probe')
        _require(isinstance(probe, dict) and probe.get('prompt') == BRANCH_PROBE, f'branching: подменён probe ветки {expected_name}')
        probe_result, final_probe_state, current_chat_id, _, _ = _validate_result(
            probe.get('result'), BRANCH_PROBE, 'branching', used_request_ids, current_chat_id, branch_id
        )
        branch_answer_results[probe_result['request_id']] = probe_result
        exported_branches.append({
            'branch': deepcopy(branch),
            'prompt': expected_prompt,
            'result': result,
            'probe': {'prompt': BRANCH_PROBE, 'result': probe_result},
        })

    _require(isinstance(final_probe_state.get('summary'), dict), 'branching: итоговый summary веток отсутствует')
    _require(isinstance(final_probe_state.get('token_accounting'), dict), 'branching: итоговый token_accounting веток отсутствует')
    _validate_ledger(final_probe_state, branch_answer_results, 'branching experiment')
    report = {
        'schema_version': 1,
        'created_at': datetime.now(timezone.utc).isoformat(),
        'scenario': deepcopy(SCENARIO),
        'evaluation': {
            'version': 3,
            'note': 'Контрольные ответы проверяются независимым EXPECTED; texture допускает int или точную десятичную строку, поскольку тип не задан в промпте.',
        },
        'conditions': conditions,
        'provenance': {'source': 'recorded_ui_sessions', 'run_id': run_id, 'chat_ids': chat_ids},
        'modes': modes,
        'branching_experiment': {
            'checkpoint': deepcopy(checkpoint),
            'branches': exported_branches,
            'summary_including_common_run': deepcopy(final_probe_state['summary']),
            'token_accounting_including_common_run': deepcopy(final_probe_state['token_accounting']),
        },
        'complete': True,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with output.open('x', encoding='utf-8') as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
    except FileExistsError:
        raise ValueError('Результат уже существует: укажите новое имя --output.') from None
    return report


def run(output, data_dir):
    output, data_dir = Path(output), Path(data_dir)
    if output.exists(): raise ValueError('Результат уже существует: укажите новое имя --output.')
    data_dir.mkdir(parents=True, exist_ok=False)
    agents = {mode:create_agent(mode=mode, db_path=data_dir / f'{mode}.sqlite3') for mode in MODES}
    report = dict(schema_version=1, created_at=datetime.now(timezone.utc).isoformat(), scenario=SCENARIO,
        evaluation=dict(version=2, note='Проверка значений; размер текстуры допускает int или точную десятичную строку, поскольку тип не задан в промпте.'),
        conditions=dict(model=agents['sliding']._config.model, reasoning_effort=agents['sliding']._config.reasoning_effort, keep_last_messages=agents['sliding']._config.keep_last_messages,
                        max_output_tokens=agents['sliding']._config.max_output_tokens,
                        pricing=asdict(agents['sliding']._config.pricing),
                        note='Один прогон, независимые ответы. Порядок режимов чередуется. Кеш не контролируется. Ветки ниже измеряются отдельно.'),
        modes={mode:dict(turns=[]) for mode in MODES})
    def save():
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    try:
        for number,prompt in enumerate(SCENARIO,1):
            order = MODES[(number-1)%3:] + MODES[:(number-1)%3]
            for mode in order:
                agent = agents[mode]
                result = agent.run(prompt)
                state = agent.state()
                turn = dict(number=number, prompt=prompt, result=asdict(result),
                    check=check_memory(result.text,EXPECTED[number]) if number in EXPECTED else None,
                    facts=state['memory']['facts'], context=next(r['metadata'].get('context') for r in state['requests'] if r['id']==result.request_id))
                report['modes'][mode]['turns'].append(turn)
                report['modes'][mode].update(summary=state['summary'],token_accounting=state['token_accounting'],requests=state['requests'])
                save()
                print(f'{mode} {number}/12: {result.status}', flush=True)
                if result.status != 'ok': raise RuntimeError(f'{mode}: {result.code}; прогон сохранён как неполный')
        branch = agents['branching']
        cp = branch.checkpoint('Два варианта освещения')
        extras = []
        for name, text in [('Мягкий свет','В этой ветке выбираю мягкий свет большой площадной лампы. Как получить мягкую тень? До 35 слов.'),
                           ('Жёсткий свет','В этой ветке выбираю жёсткий свет маленького источника. Как получить резкую тень? До 35 слов.')]:
            item = branch.branch(cp['id'],name)
            result = branch.run(text)
            extras.append(dict(branch=item,prompt=text,result=asdict(result)))
        for item in extras:
            branch.switch(item['branch']['id'])
            prompt = 'Какой свет выбран именно в этой ветке и каким размером источника он получается? До 25 слов.'
            item['probe'] = dict(prompt=prompt,result=asdict(branch.run(prompt)))
        report['branching_experiment'] = dict(checkpoint=cp,branches=extras,
            summary_including_common_run=branch.state()['summary'],token_accounting_including_common_run=branch.state()['token_accounting'])
        report['complete'] = all(x['result']['status']=='ok' and x['probe']['result']['status']=='ok' for x in extras)
        save()
        return report
    finally:
        for agent in agents.values(): agent.close()


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',default='results/reference.json')
    parser.add_argument('--data-dir',default='data/comparison-'+uuid4().hex[:10])
    parser.add_argument('--from-recording-dir', help='Собрать отчёт из трёх записанных UI-сессий без API-вызовов.')
    args=parser.parse_args()
    if args.from_recording_dir:
        export_recorded_sessions(args.from_recording_dir, args.output)
        print(f'Отчёт записан: {args.output}', flush=True)
    else:
        load_dotenv(Path(__file__).with_name('.env'))
        run(args.output,args.data_dir)

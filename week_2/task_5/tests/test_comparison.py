from copy import deepcopy
from decimal import Decimal
import json

import pytest

from comparison import (
    BRANCH_PROBE,
    BRANCH_SCENARIO,
    EXPECTED,
    MODES,
    SCENARIO,
    export_recorded_sessions,
)


PRICING = {
    'model': 'gpt-5.6-luna',
    'input_usd_per_million': '0.20',
    'cached_input_usd_per_million': '0.02',
    'cache_write_usd_per_million': '0.25',
    'output_usd_per_million': '1.20',
    'checked_at': '2026-09-14',
    'source': 'https://example.test/pricing',
    'max_input_tokens': 272000,
}


def _refresh_aggregates(state):
    called = [item for item in state['requests'] if item['usage_status'] != 'not_requested']
    known = [item['usage'] for item in called if item['usage'] is not None]
    unknown_cost = sum(item['cost_usd'] is None for item in called)
    state['summary'] = {
        'api_requests': len(called),
        'known_cost_usd': format(sum((Decimal(item['cost_usd']) for item in called if item['cost_usd'] is not None), Decimal(0)), 'f'),
        'cost_complete': unknown_cost == 0,
        'unknown_cost_requests': unknown_cost,
    }
    state['token_accounting'] = {
        'known_input_tokens': sum(item['input_tokens'] for item in known),
        'known_output_tokens': sum(item['output_tokens'] for item in known),
        'known_total_tokens': sum(item['total_tokens'] for item in known),
        'complete': len(known) == len(called),
        'unknown_requests': len(called) - len(known),
    }


def _facts_record(parent_request_id):
    return {
        'id': parent_request_id + 1,
        'status': 'ok',
        'text': '',
        'code': '',
        'usage': {
            'input_tokens': 100,
            'output_tokens': 10,
            'total_tokens': 110,
            'cached_input_tokens': 0,
            'reasoning_tokens': 0,
            'cache_write_input_tokens': 0,
        },
        'cost_usd': '0.000032',
        'usage_status': 'reported',
        'input_policy': {'name': 'nonempty_text', 'status': 'accepted'},
        'output_policy': {'name': 'completed_text_and_facts_schema', 'status': 'accepted'},
        'metadata': {'kind': 'facts', 'parent_request_id': parent_request_id, 'pricing': PRICING},
    }


def _response(mode, chat_id, prompt, text, request_id, messages, requests, *, facts=None, branch_id=1):
    usage = {
        'input_tokens': 100,
        'output_tokens': 10,
        'total_tokens': 110,
        'cached_input_tokens': 0,
        'reasoning_tokens': 0,
        'cache_write_input_tokens': 0,
    }
    input_policy = {'name': 'nonempty_text', 'status': 'accepted'}
    output_policy = {'name': 'completed_text', 'status': 'accepted'}
    context = {'sent_messages': request_id, 'marker': f'{mode}-{request_id}'}
    record = {
        'id': request_id,
        'status': 'ok',
        'text': '',
        'code': '',
        'usage': usage,
        'cost_usd': '0.000032',
        'usage_status': 'reported',
        'input_policy': input_policy,
        'output_policy': output_policy,
        'metadata': {
            'requested_model': 'gpt-5.6-luna',
            'actual_model': 'gpt-5.6-luna',
            'pricing': PRICING,
            'kind': 'answer',
            'mode': mode,
            'branch_id': branch_id,
            'context': context,
        },
    }
    current_messages = deepcopy(messages) + [
        {'id': request_id * 2 - 1, 'role': 'user', 'content': prompt, 'request_id': request_id},
        {'id': request_id * 2, 'role': 'assistant', 'content': text, 'request_id': request_id},
    ]
    current_requests = deepcopy(requests) + [record]
    state = {
        'active_branch': branch_id,
        'branches': [{'id': 1, 'name': 'Основная'}],
        'checkpoints': [],
        'chat_id': chat_id,
        'messages': current_messages,
        'requests': current_requests,
        'summary': {},
        'token_accounting': {},
        'memory': {
            'mode': mode,
            'facts': facts or {},
            'keep_last_messages': 6,
        },
        'context': {
            'model': 'gpt-5.6-luna',
            'max_output_tokens': 1600,
        },
    }
    _refresh_aggregates(state)
    result = {
        'status': 'ok',
        'text': text,
        'code': '',
        'usage': usage,
        'cost_usd': '0.000032',
        'usage_status': 'reported',
        'request_id': request_id,
        'input_policy': input_policy,
        'output_policy': output_policy,
        'state': state,
    }
    return result, current_messages, current_requests


def _sessions():
    sessions = {}
    for mode in MODES:
        messages = []
        requests = []
        turns = []
        for number, prompt in enumerate(SCENARIO, 1):
            request_id = number * 2 - 1 if mode == 'facts' else number
            text = json.dumps(EXPECTED[number], ensure_ascii=False) if number in EXPECTED else f'Ответ {mode} {number}'
            facts = {'snapshot': f'{mode}-{number}'}
            result, messages, requests = _response(
                mode, f'{mode}-chat', prompt, text, request_id, messages, requests, facts=facts
            )
            if mode == 'facts':
                requests.append(_facts_record(request_id))
                result['state']['requests'] = deepcopy(requests)
                _refresh_aggregates(result['state'])
            turns.append({'number': number, 'prompt': prompt, 'result': result})
        sessions[mode] = {'run_id': 'one-recording', 'mode': mode, 'turns': turns}

    checkpoint = {'id': 1, 'name': 'Два варианта освещения'}
    base_state = sessions['branching']['turns'][-1]['result']['state']
    all_requests = deepcopy(base_state['requests'])
    branch_items = []
    branch_results = []
    for offset, (name, prompt) in enumerate(BRANCH_SCENARIO, 2):
        branch = {'id': offset, 'name': name}
        text = f'Ответ ветки {name}'
        result, _, all_requests = _response(
            'branching', 'branching-chat', prompt, text, offset + 11,
            base_state['messages'], all_requests, branch_id=offset,
        )
        result['state']['branches'] = [{'id': 1, 'name': 'Основная'}, branch]
        result['state']['checkpoints'] = [checkpoint]
        branch_results.append((branch, prompt, result))

    for index, (branch, prompt, result) in enumerate(branch_results, 15):
        probe_result, _, all_requests = _response(
            'branching', 'branching-chat', BRANCH_PROBE, f'Probe {branch["name"]}', index,
            result['state']['messages'], all_requests, branch_id=branch['id'],
        )
        probe_result['state']['branches'] = [{'id': 1, 'name': 'Основная'}, branch]
        probe_result['state']['checkpoints'] = [checkpoint]
        branch_items.append({
            'branch': branch,
            'prompt': prompt,
            'result': result,
            'probe': {'prompt': BRANCH_PROBE, 'result': probe_result},
        })
    sessions['branching']['branching_experiment'] = {'checkpoint': checkpoint, 'branches': branch_items}
    return sessions


def _write_sessions(directory, sessions):
    directory.mkdir()
    for mode, session in sessions.items():
        (directory / f'{mode}-session.json').write_text(
            json.dumps(session, ensure_ascii=False), encoding='utf-8'
        )


def test_export_uses_snapshots_and_last_probe_ledger(tmp_path):
    sessions = _sessions()
    recording_dir = tmp_path / 'recording'
    output = tmp_path / 'report.json'
    _write_sessions(recording_dir, sessions)

    report = export_recorded_sessions(recording_dir, output)

    assert output.exists()
    assert report['provenance'] == {
        'source': 'recorded_ui_sessions',
        'run_id': 'one-recording',
        'chat_ids': {mode: f'{mode}-chat' for mode in MODES},
    }
    turn = report['modes']['sliding']['turns'][5]
    assert turn['facts'] == {'snapshot': 'sliding-6'}
    assert turn['context']['marker'] == 'sliding-6'
    assert turn['check']['expected'] == EXPECTED[6]
    assert 'state' not in turn['result']
    assert report['modes']['branching']['summary']['api_requests'] == 12
    last_probe_state = sessions['branching']['branching_experiment']['branches'][-1]['probe']['result']['state']
    assert report['branching_experiment']['summary_including_common_run'] == last_probe_state['summary']
    assert report['branching_experiment']['token_accounting_including_common_run'] == last_probe_state['token_accounting']
    assert report['branching_experiment']['summary_including_common_run']['api_requests'] == 16
    assert report['branching_experiment']['token_accounting_including_common_run']['known_total_tokens'] == 1760


@pytest.mark.parametrize('corruption', ['run_id', 'scenario', 'result'])
def test_export_rejects_mismatched_sessions(tmp_path, corruption):
    sessions = _sessions()
    if corruption == 'run_id':
        sessions['facts']['run_id'] = 'another-recording'
    elif corruption == 'scenario':
        sessions['sliding']['turns'][1]['prompt'] = 'Подменённый вопрос'
    else:
        sessions['sliding']['turns'][0]['result']['text'] = 'Подменённый ответ'
    recording_dir = tmp_path / 'recording'
    _write_sessions(recording_dir, sessions)

    with pytest.raises(ValueError, match='Некорректная UI-запись'):
        export_recorded_sessions(recording_dir, tmp_path / 'report.json')


@pytest.mark.parametrize(
    'corruption',
    ['extra_request', 'facts_parent', 'extra_branch_request', 'summary_cost',
     'token_input', 'token_output', 'token_total', 'token_complete'],
)
def test_export_rejects_invalid_ledger_or_aggregate(tmp_path, corruption):
    sessions = _sessions()
    sliding_state = sessions['sliding']['turns'][-1]['result']['state']
    facts_state = sessions['facts']['turns'][-1]['result']['state']
    branch_state = sessions['branching']['branching_experiment']['branches'][-1]['probe']['result']['state']
    if corruption == 'extra_request':
        extra = deepcopy(sliding_state['requests'][0])
        extra['id'] = 99
        sliding_state['requests'].append(extra)
        _refresh_aggregates(sliding_state)
    elif corruption == 'facts_parent':
        next(item for item in facts_state['requests'] if item['metadata']['kind'] == 'facts')['metadata']['parent_request_id'] = 999
    elif corruption == 'extra_branch_request':
        extra = deepcopy(branch_state['requests'][0])
        extra['id'] = 99
        branch_state['requests'].append(extra)
        _refresh_aggregates(branch_state)
    elif corruption == 'summary_cost':
        sliding_state['summary']['known_cost_usd'] = '999'
    elif corruption == 'token_complete':
        sliding_state['token_accounting']['complete'] = False
    else:
        field = {
            'token_input': 'known_input_tokens',
            'token_output': 'known_output_tokens',
            'token_total': 'known_total_tokens',
        }[corruption]
        sliding_state['token_accounting'][field] += 1
    recording_dir = tmp_path / 'recording'
    _write_sessions(recording_dir, sessions)

    with pytest.raises(ValueError, match='Некорректная UI-запись'):
        export_recorded_sessions(recording_dir, tmp_path / 'report.json')


def test_export_preserves_unknown_usage_and_cost(tmp_path):
    sessions = _sessions()
    result = sessions['sliding']['turns'][-1]['result']
    result['usage'] = None
    result['cost_usd'] = None
    result['usage_status'] = 'unavailable'
    record = next(item for item in result['state']['requests'] if item['id'] == result['request_id'])
    record['usage'] = None
    record['cost_usd'] = None
    record['usage_status'] = 'unavailable'
    _refresh_aggregates(result['state'])
    recording_dir = tmp_path / 'recording'
    _write_sessions(recording_dir, sessions)

    report = export_recorded_sessions(recording_dir, tmp_path / 'report.json')

    assert report['modes']['sliding']['turns'][-1]['result']['usage'] is None
    assert report['modes']['sliding']['summary']['cost_complete'] is False
    assert report['modes']['sliding']['summary']['unknown_cost_requests'] == 1
    assert report['modes']['sliding']['token_accounting']['complete'] is False
    assert report['modes']['sliding']['token_accounting']['unknown_requests'] == 1


def test_export_never_overwrites_existing_report(tmp_path):
    output = tmp_path / 'report.json'
    output.write_text('keep', encoding='utf-8')

    with pytest.raises(ValueError, match='уже существует'):
        export_recorded_sessions(tmp_path / 'unused', output)
    assert output.read_text(encoding='utf-8') == 'keep'


def test_comparison_endpoint_uses_configured_report(monkeypatch, tmp_path):
    import app as app_module
    from test_strategies import make

    task_dir = tmp_path / 'task'
    task_dir.mkdir()
    report_path = task_dir / 'recorded.json'
    expected = {'provenance': {'source': 'recorded_ui_sessions', 'run_id': 'shown-run'}}
    report_path.write_text(json.dumps(expected), encoding='utf-8')
    monkeypatch.setattr(app_module, '__file__', str(task_dir / 'app.py'))
    monkeypatch.setenv('CHAT_COMPARISON_PATH', str(report_path))
    agent, _ = make('sliding')
    client = app_module.create_app(agent).test_client()

    response = client.get('/api/comparison')
    assert response.status_code == 200
    assert response.json == expected

    monkeypatch.setenv('CHAT_COMPARISON_PATH', str(task_dir / 'missing.json'))
    assert client.get('/api/comparison').status_code == 404
    agent.close()

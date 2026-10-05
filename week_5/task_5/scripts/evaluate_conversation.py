"""Run two fixed ordinary conversations through the application API, one attempt per turn.

The JSON is structural evidence. Semantic answer quality requires a separate review.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from uuid import uuid4

from dotenv import load_dotenv


TASK_ROOT = Path(__file__).resolve().parents[1]
if str(TASK_ROOT) not in sys.path:
    sys.path.insert(0, str(TASK_ROOT))

from agent.transport import ResponsesTransport
from app import create_app
from indexing.client import OpenAIEmbedder
from rag.config import load_config
from rag.retrieval import read_index


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def write_incremental(path: Path, value: dict, *, create: bool = False) -> None:
    """Never overwrite another run; replace later snapshots with complete JSON."""
    path = Path(path)
    encoded = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    if create:
        with path.open('x', encoding='utf-8', newline='\n') as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        return
    temporary = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8', newline='\n') as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def load_scenarios(path: Path) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    scenarios = data.get('scenarios') if isinstance(data, dict) else None
    if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(scenarios, list) or len(scenarios) != 2:
        raise ValueError('Expected two version-1 scenarios.')
    ids = []
    for scenario in scenarios:
        if not isinstance(scenario, dict) or scenario.get('dialogue_kind') != 'ordinary':
            raise ValueError('Each scenario must be an ordinary dialogue.')
        identity, turns = scenario.get('id'), scenario.get('turns')
        if not isinstance(identity, str) or not identity or identity in ids:
            raise ValueError('Scenario IDs must be distinct nonempty strings.')
        ids.append(identity)
        if not isinstance(turns, list) or len(turns) != 12:
            raise ValueError('Each scenario needs exactly 12 turns.')
        for number, turn in enumerate(turns, 1):
            if (not isinstance(turn, dict) or turn.get('number') != number
                    or not isinstance(turn.get('prompt'), str) or not turn['prompt'].strip()
                    or not isinstance(turn.get('supporting_facts'), list)
                    or not turn['supporting_facts']
                    or any(not isinstance(fact, str) or not fact.strip()
                           for fact in turn['supporting_facts'])):
                raise ValueError('Scenario turn is incomplete or unordered.')
            expected = turn.get('expected_state', {})
            if not isinstance(expected, dict) or set(expected) - {
                    'goal_contains', 'constraints_present', 'constraints_absent', 'terms_present'}:
                raise ValueError('Invalid fixed state expectation.')
            for key, value in expected.items():
                if key == 'goal_contains':
                    if not isinstance(value, str) or not value.strip():
                        raise ValueError('Invalid goal expectation.')
                elif not isinstance(value, list) or any(not isinstance(item, str) or not item.strip()
                                                         for item in value):
                    raise ValueError('Invalid state expectation list.')
        late = turns[-1].get('expected_state', {})
        if not late.get('goal_contains') or not late.get('constraints_present') or not late.get('terms_present'):
            raise ValueError('Late turn must test goal, conditions and terminology.')
    return scenarios


def _values(state: dict, field: str, *, include_keys: bool = False) -> list[str]:
    rows = state.get(field, [])
    if not isinstance(rows, list):
        return []
    return [(str(row.get('key', '')) + ' ' if include_keys else '') + str(row.get('value', ''))
            for row in rows if isinstance(row, dict)]


def _contains(values: list[str], expected: str) -> bool:
    def normalize(value: str) -> str:
        return re.sub(r'(?<![0-9.,])\d+(?:[.,]\d+)?(?![0-9])',
                      lambda match: format(Decimal(match.group().replace(',', '.')).normalize(), 'f'),
                      value.casefold())

    marker = normalize(expected)
    if marker in {'два', 'три'}:
        variants = {'два': ('два', 'две', 'двух', '2'),
                    'три': ('три', 'трёх', 'трех', '3')}
        pattern = r'(?<!\w)(?:' + '|'.join(variants[marker]) + r')(?!\w)'
        return any(re.search(pattern, normalize(value)) is not None for value in values)
    pattern = (r'(?<![0-9.,])' if marker[0].isdigit() else '') + re.escape(marker)
    if marker[-1].isdigit():
        pattern += r'(?![0-9.,])'
    return any(re.search(pattern, normalize(value)) is not None for value in values)


def assess_turn(turn: dict, response: dict) -> dict:
    """Only structural and lexical checks. A reviewer must read facts and excerpts."""
    problems = []
    state = response.get('state') if isinstance(response.get('state'), dict) else {}
    memory = state.get('conversation_state') if isinstance(state.get('conversation_state'), dict) else {}
    requests = state.get('requests') if isinstance(state.get('requests'), list) else []
    parent_id = response.get('request_id')
    parents = [row for row in requests if isinstance(row, dict)
               and row.get('metadata', {}).get('kind') == 'answer']
    parent = next((row for row in parents if row.get('id') == parent_id), None)
    if parent is None and parents:
        parent = parents[-1]
    rag = parent.get('metadata', {}).get('rag', {}) if parent else {}
    used = rag.get('used_sources') if isinstance(rag.get('used_sources'), list) else []
    selected = rag.get('sources') if isinstance(rag.get('sources'), list) else []
    text = response.get('text')
    if response.get('status') != 'ok' or not isinstance(text, str) or not text.strip():
        problems.append('Answerable turn has no successful nonempty answer.')
    elif text.strip().casefold().startswith(('не знаю', 'не могу ответить', 'недостаточно данных')):
        problems.append('Answerable turn received a refusal.')
    retrievals = state.get('retrievals') if isinstance(state.get('retrievals'), list) else []
    if not parent or parent.get('status') != 'ok' or not any(
            isinstance(row, dict) and row.get('provider') == 'local_index'
            and row.get('status') == 'ok' and row.get('request_id') == parent.get('id')
            for row in retrievals):
        problems.append('No successful local retrieval for this answer.')
    if not used or not selected or any(
            not isinstance(source, dict) or not source.get('source')
            or not source.get('section') or not source.get('chunk_id') or not source.get('text')
            or not any(source == candidate for candidate in selected) for source in used):
        problems.append('Used source fragments are empty or differ from selected fragments.')
    expected = turn.get('expected_state', {})
    goal = str(memory.get('goal', '')).casefold()
    if expected.get('goal_contains') and expected['goal_contains'].casefold() not in goal:
        problems.append('Goal does not contain: ' + expected['goal_contains'])
    constraints = _values(memory, 'constraints')
    terms = _values(memory, 'terms', include_keys=True)
    for value in expected.get('constraints_present', []):
        if not _contains(constraints, value):
            problems.append('Current constraints miss: ' + value)
    for value in expected.get('constraints_absent', []):
        if _contains(constraints, value):
            problems.append('Revoked or hypothetical constraint remains: ' + value)
    for value in expected.get('terms_present', []):
        if not _contains(terms, value):
            problems.append('Current terms miss: ' + value)
    return {'passed': not problems, 'problems': problems,
            'semantic_support': 'pending_independent_review'}


class PayloadCapture:
    """Record complete Responses payloads before provider delivery, without headers."""

    def __init__(self, delegate, events: list[dict]):
        self.delegate = delegate
        self.events = events

    def create(self, payload, timeout):
        self.events.append({'kind': 'responses', 'payload': json.loads(json.dumps(
            payload, ensure_ascii=False, allow_nan=False))})
        return self.delegate.create(payload, timeout)


class EmbeddingCapture:
    """Record the embedding body at the same boundary as the production embedder."""

    def __init__(self, config, events: list[dict]):
        self.delegate = OpenAIEmbedder(config)
        self.config = config
        self.events = events

    def embed(self, texts):
        self.events.append({'kind': 'embedding', 'payload': {
            'model': self.config.model, 'input': list(texts),
            'dimensions': self.config.dimensions, 'encoding_format': 'float'}})
        return self.delegate.embed(texts)


def _new_rows(before: dict, after: dict, key: str) -> list[dict]:
    seen = {row['id'] for row in before.get(key, [])}
    return [row for row in after.get(key, []) if row['id'] not in seen]


def totals(scenarios: list[dict]) -> dict:
    requests = [row for scenario in scenarios for turn in scenario.get('turns', [])
                for row in turn.get('requests', []) if row.get('usage_status') != 'not_requested']
    known = sum((Decimal(row['cost_usd']) for row in requests if row.get('cost_usd') is not None), Decimal(0))
    unknown = sum(row.get('cost_usd') is None for row in requests)
    return {'api_requests': len(requests), 'known_cost_usd': format(known, 'f'),
            'cost_complete': unknown == 0, 'unknown_cost_requests': unknown,
            'known_total_tokens': sum(row['usage']['total_tokens'] for row in requests
                                      if isinstance(row.get('usage'), dict)
                                      and isinstance(row['usage'].get('total_tokens'), int))}


def _reopen(data_dir: Path, transport: PayloadCapture, dialogue_id: int) -> tuple[object, dict]:
    app = create_app(data_dir=data_dir, transport=transport)
    client = app.test_client()
    state = client.get('/api/state').get_json()
    if state['workspace']['active_dialogue']['id'] != dialogue_id:
        raise RuntimeError('Reopen selected a different dialogue.')
    return app, state


def _same_saved_state(expected: dict, actual: dict) -> bool:
    """Compare all persisted conversation evidence used by reopen and isolation checks."""
    return all(actual.get(key) == expected.get(key)
               for key in ('conversation_state', 'messages', 'requests', 'retrievals'))


def run(data_dir: Path, output: Path, scenarios_path: Path, *, env_file: Path | None = None) -> dict:
    scenarios_path = Path(scenarios_path).resolve(strict=True)
    scenarios = load_scenarios(scenarios_path)
    if env_file is not None:
        load_dotenv(Path(env_file).resolve(strict=True), override=False)
    key = os.environ.get('OPENAI_API_KEY')
    if not key:
        raise ValueError('OPENAI_API_KEY is absent from the environment.')
    data_dir = Path(data_dir).resolve(strict=True)
    output = Path(output).resolve()
    runtime_dir = output.with_name(output.stem + '.workspace')
    if output.exists() or runtime_dir.exists():
        raise FileExistsError('Evaluation output or workspace already exists.')
    config = load_config()
    chunks = read_index(TASK_ROOT, data_dir, config)
    source_index = data_dir / 'indexing.sqlite3'
    source_hash = digest(source_index)
    output.parent.mkdir(parents=True, exist_ok=True)
    runtime_dir.mkdir()
    target_index = runtime_dir / 'indexing.sqlite3'
    with sqlite3.connect(source_index.as_uri() + '?mode=ro', uri=True) as incoming:
        incoming.execute('PRAGMA query_only=ON')
        with sqlite3.connect(target_index) as outgoing:
            incoming.backup(outgoing)
    if source_hash != digest(source_index):
        raise RuntimeError('Source index changed during evaluation snapshot.')
    if [row['chunk_id'] for row in read_index(TASK_ROOT, runtime_dir, config)] != [row['chunk_id'] for row in chunks]:
        raise RuntimeError('Isolated index differs from verified source index.')
    report = {'version': 1, 'status': 'running', 'created_at': datetime.now(timezone.utc).isoformat(),
              'scope': 'two independent ordinary dialogues, 12 answerable turns each, one attempt per turn',
              'scenario_sha256': digest(scenarios_path), 'scenario_path': str(scenarios_path),
              'index': {'source_sha256': source_hash, 'runtime_sha256': digest(target_index),
                        'verified_chunks': len(chunks)},
              'agent_config_sha256': digest(TASK_ROOT / 'agent' / 'config.toml'),
              'rag_config_sha256': digest(TASK_ROOT / 'rag' / 'config.toml'),
              'scenarios': [], 'totals': totals([]),
              'semantic_assessment': 'pending_independent_review'}
    write_incremental(output, report, create=True)
    events: list[dict] = []
    capture = PayloadCapture(ResponsesTransport(key), events)
    from rag import chat
    original_embedder = chat.OpenAIEmbedder
    chat.OpenAIEmbedder = lambda embed_config: EmbeddingCapture(embed_config, events)
    app = create_app(data_dir=runtime_dir, transport=capture)
    first_identity = None
    try:
        client = app.test_client()
        task_id = client.get('/api/state').get_json()['workspace']['active_dialogue']['task_id']
        for scenario in scenarios:
            created = client.post('/api/dialogue', json={'name': scenario['title'],
                'task_id': task_id, 'mode': 'sliding', 'dialogue_kind': 'ordinary'})
            if created.status_code != 200:
                raise RuntimeError('Could not create ordinary scenario dialogue.')
            state = created.get_json()['state']
            identity = state['workspace']['active_dialogue']['id']
            entry = {'id': scenario['id'], 'dialogue_id': identity, 'dialogue_kind': 'ordinary',
                     'title': scenario['title'], 'turns': [], 'reopen': None}
            report['scenarios'].append(entry)
            write_incremental(output, report)
            for turn in scenario['turns']:
                before = client.get('/api/state').get_json()
                start_events = len(events)
                failure = None
                try:
                    sent = client.post('/api/ask', json={'prompt': turn['prompt'],
                        'use_working': True, 'use_long_term': True, 'rag_mode': 'filter'})
                    body = sent.get_json() or {}
                    http_status = sent.status_code
                except Exception as error:
                    body = {}
                    http_status = None
                    failure = type(error).__name__
                after = client.get('/api/state').get_json()
                result = {k: value for k, value in body.items() if k != 'state'}
                result['state'] = after
                findings = assess_turn(turn, result)
                if failure:
                    findings['passed'] = False
                    findings['problems'].append('HTTP invocation raised ' + failure)
                parent = next((row for row in reversed(after.get('requests', []))
                    if row.get('metadata', {}).get('kind') == 'answer'
                    and row.get('metadata', {}).get('conversation', {}).get('original_query') == turn['prompt']), None)
                rag = parent.get('metadata', {}).get('rag', {}) if parent else {}
                record = {'number': turn['number'], 'prompt': turn['prompt'],
                          'supporting_facts': turn['supporting_facts'],
                          'expected_state': turn.get('expected_state', {}),
                          'http_status': http_status, 'result': {k: value for k, value in result.items() if k != 'state'},
                          'answer': body.get('text'), 'state_before': before,
                          'state_after': after, 'conversation_state_before': before.get('conversation_state'),
                          'conversation_state_after': after.get('conversation_state'),
                          'selected_sources': rag.get('sources', []), 'used_sources': rag.get('used_sources', []),
                          'retrievals': _new_rows(before, after, 'retrievals'),
                          'requests': _new_rows(before, after, 'requests'),
                          'provider_payloads': events[start_events:],
                          'structural': findings,
                          'semantic_assessment': 'pending_independent_review'}
                entry['turns'].append(record)
                report['totals'] = totals(report['scenarios'])
                if not findings['passed']:
                    report['status'] = 'failed_structural'
                write_incremental(output, report)
                if failure:
                    raise RuntimeError('Turn failed before HTTP response; evidence saved.')
            app.extensions['workspace'].close()
            app, reopened = _reopen(runtime_dir, capture, identity)
            client = app.test_client()
            final = entry['turns'][-1]['state_after']
            persisted = _same_saved_state(final, reopened)
            entry['reopen'] = {'passed': persisted, 'state': reopened}
            if not persisted:
                report['status'] = 'failed_structural'
            write_incremental(output, report)
            if first_identity is None:
                first_identity = identity
                first_state = reopened
        switched = client.post('/api/open', json={'dialogue_id': first_identity})
        isolated = switched.status_code == 200 and _same_saved_state(
            first_state, switched.get_json()['state'])
        report['isolation'] = {'passed': isolated, 'first_dialogue_id': first_identity,
                               'reopened_first_state': switched.get_json().get('state') if switched.is_json else None}
        if not isolated:
            report['status'] = 'failed_structural'
        if report['status'] == 'running':
            report['status'] = 'complete_structural'
        write_incremental(output, report)
        return report
    except Exception as error:
        report['status'] = 'interrupted'
        report['failure_type'] = type(error).__name__
        report['totals'] = totals(report['scenarios'])
        write_incremental(output, report)
        raise
    finally:
        app.extensions['workspace'].close()
        chat.OpenAIEmbedder = original_embedder


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, required=True,
                        help='Directory with verified indexing.sqlite3')
    parser.add_argument('--output', type=Path, required=True,
                        help='New JSON output; an isolated sibling .workspace is created')
    parser.add_argument('--env-file', type=Path, help='Optional local .env with OPENAI_API_KEY')
    parser.add_argument('--scenarios', type=Path, required=True, help='Fixed 2×12 scenario JSON')
    args = parser.parse_args(argv)
    report = run(args.data_dir, args.output, args.scenarios, env_file=args.env_file)
    print(json.dumps({'status': report['status'], 'output': str(args.output),
                      'turns': sum(len(row['turns']) for row in report['scenarios'])}, ensure_ascii=False))
    return 0 if report['status'] == 'complete_structural' else 2


if __name__ == '__main__':
    raise SystemExit(main())

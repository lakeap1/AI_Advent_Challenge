"""Run the ten fixed questions through the production main-chat agent once."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from decimal import Decimal
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import sys
from uuid import uuid4

from dotenv import load_dotenv


TASK_ROOT = Path(__file__).resolve().parents[1]
if str(TASK_ROOT) not in sys.path:
    sys.path.insert(0, str(TASK_ROOT))

from agent.transport import ResponsesTransport
from rag.chat import RagProfileWorkspace
from rag.config import load_config
from rag.retrieval import read_index


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n',
                         encoding='utf-8', newline='\n')
    temporary.replace(path)


def load_questions(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding='utf-8'))
    questions = data.get('questions') if isinstance(data, dict) else None
    if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(questions, list) or len(questions) != 10:
        raise ValueError('Expected exactly ten version-1 questions.')
    ids = [item.get('id') for item in questions if isinstance(item, dict)]
    if (len(ids) != 10 or len(set(ids)) != 10 or
            any(not isinstance(item.get('id'), str) or
                not isinstance(item.get('question'), str) or not item['question'].strip() or
                not isinstance(item.get('expected_facts'), list) or not item['expected_facts'] or
                not all(isinstance(fact, str) and fact.strip() for fact in item['expected_facts']) or
                not isinstance(item.get('expected_sources'), list) or not item['expected_sources'] or
                not all(isinstance(source, dict) and isinstance(source.get('file'), str)
                        and source['file'] for source in item['expected_sources']) or
                item.get('unanswerable') is not False for item in questions)):
        raise ValueError('All ten questions must be unique, complete and answerable.')
    return questions


class PayloadCapture:
    """Capture request bodies before delivery; authorization headers never enter this ledger."""

    def __init__(self, delegate):
        self.delegate = delegate
        self.payloads: list[dict] = []

    def create(self, payload: dict, timeout: float):
        self.payloads.append(json.loads(json.dumps(payload, ensure_ascii=False, allow_nan=False)))
        return self.delegate.create(payload, timeout)


def totals(questions: list[dict]) -> dict:
    requests = [request for question in questions for request in question.get('requests', [])
                if request.get('usage_status') != 'not_requested']
    known = sum((Decimal(str(row['cost_usd'])) for row in requests
                 if row.get('cost_usd') is not None), Decimal('0'))
    unknown = sum(row.get('cost_usd') is None for row in requests)
    return {'api_requests': len(requests), 'known_cost_usd': format(known, 'f'),
            'cost_complete': unknown == 0, 'unknown_cost_requests': unknown,
            'known_total_tokens': sum(row.get('usage', {}).get('total_tokens', 0)
                                      for row in requests if isinstance(row.get('usage'), dict))}


def source_flags(result, requests: list[dict]) -> dict:
    parent = next((row for row in requests if row.get('id') == result.request_id), None)
    rag = parent.get('metadata', {}).get('rag', {}) if parent else {}
    selected = rag.get('sources') if isinstance(rag.get('sources'), list) else []
    used = rag.get('used_sources') if isinstance(rag.get('used_sources'), list) else []
    grounding = rag.get('grounding') if isinstance(rag.get('grounding'), dict) else {}
    claims = grounding.get('claims') if isinstance(grounding.get('claims'), list) else []
    source_keys = ('label', 'source', 'section', 'chunk_id', 'text',
                   'file', 'line_start', 'line_end', 'document_hash')
    selected_by_label = {row.get('label'): row for row in selected if isinstance(row, dict)}
    used_by_label = {row.get('label'): row for row in used if isinstance(row, dict)}
    fragment_match = bool(used) and len(used_by_label) == len(used) and all(
        isinstance(row, dict) and all(isinstance(row.get(key), str) and row[key].strip()
                                      for key in ('label', 'source', 'chunk_id', 'text'))
        and (isinstance(row.get('section'), str) and bool(row['section'].strip())
             or isinstance(row.get('section'), list) and bool(row['section'])
             and all(isinstance(part, str) and part.strip() for part in row['section']))
        and row.get('label') in selected_by_label
        and all(row.get(key) == selected_by_label[row['label']].get(key) for key in source_keys)
        for row in used)
    claims_linked = bool(claims) and all(
        isinstance(claim, dict) and isinstance(claim.get('text'), str) and bool(claim['text'].strip())
        and isinstance(claim.get('source_labels'), list) and bool(claim['source_labels'])
        and all(label in used_by_label for label in claim['source_labels'])
        for claim in claims)
    answered = result.status == 'ok' and grounding.get('status') == 'answered'
    return {'answer_present': answered, 'used_fragments_match_selected': fragment_match,
            'every_claim_linked_to_used_source': claims_linked,
            'structural_pass': answered and fragment_match and claims_linked,
            'semantic_support': 'pending_independent_review'}


def evaluate_questions(workspace, questions: list[dict], capture: PayloadCapture,
                       report: dict, output: Path) -> dict:
    """Create one clean dialogue per question and save every attempt atomically."""
    task_id = workspace.memory.workspace()['active_dialogue']['task_id']
    for item in questions:
        workspace.memory.create_dialogue('Оценка ' + item['id'], task_id, 'sliding')
        agent = workspace.agent()
        start = len(capture.payloads)
        try:
            result = agent.run(item['question'], rag_mode='filter',
                               use_working=False, use_long_term=False)
        except Exception as error:
            state = agent.state()
            report['questions'].append({
                'id': item['id'], 'question': item['question'],
                'expected_facts': item['expected_facts'],
                'expected_sources': item['expected_sources'],
                'unanswerable': False,
                'dialogue_id': workspace.memory.workspace()['active_dialogue']['id'],
                'result': {'status': 'interrupted', 'failure_type': type(error).__name__},
                'messages': state.get('messages', []),
                'requests': state.get('requests', []),
                'retrievals': state.get('retrievals', []),
                'summary': state.get('summary', {}),
                'token_accounting': state.get('token_accounting', {}),
                'payloads': capture.payloads[start:],
                'structural': {'structural_pass': False,
                               'semantic_support': 'pending_independent_review'},
                'assessment': 'pending_independent_review',
            })
            report['status'] = 'interrupted'
            report['totals'] = totals(report['questions'])
            atomic_json(output, report)
            raise
        state = agent.state()
        requests = state.get('requests', [])
        flags = source_flags(result, requests)
        report['questions'].append({
            'id': item['id'], 'question': item['question'],
            'expected_facts': item['expected_facts'],
            'expected_sources': item['expected_sources'],
            'unanswerable': False,
            'dialogue_id': workspace.memory.workspace()['active_dialogue']['id'],
            'result': asdict(result),
            'messages': state.get('messages', []),
            'requests': requests,
            'retrievals': state.get('retrievals', []),
            'summary': state.get('summary', {}),
            'token_accounting': state.get('token_accounting', {}),
            'payloads': capture.payloads[start:],
            'structural': flags,
            'assessment': 'pending_independent_review',
        })
        report['status'] = ('complete_structural' if len(report['questions']) == 10 and
                            all(q['structural']['structural_pass'] for q in report['questions'])
                            else 'incomplete')
        report['totals'] = totals(report['questions'])
        atomic_json(output, report)
    return report


def run(data_dir: Path, output: Path, *, env_file: Path | None = None) -> dict:
    if env_file is not None:
        load_dotenv(env_file, override=False)
    key = os.environ.get('OPENAI_API_KEY')
    if not key:
        raise ValueError('OPENAI_API_KEY is absent from the environment.')
    data_dir = data_dir.resolve(strict=True)
    output = output.resolve()
    runtime_dir = output.with_name(output.stem + '.workspace')
    if output.exists() or runtime_dir.exists():
        raise FileExistsError('Evaluation output or workspace already exists.')
    config = load_config()
    source_chunks = read_index(TASK_ROOT, data_dir, config)
    source_index = data_dir / 'indexing.sqlite3'
    source_sha = digest(source_index)
    questions_path = TASK_ROOT / 'evaluation' / 'questions.json'
    questions = load_questions(questions_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    runtime_dir.mkdir()
    target_index = runtime_dir / 'indexing.sqlite3'
    with sqlite3.connect(source_index.resolve().as_uri() + '?mode=ro', uri=True) as incoming:
        incoming.execute('PRAGMA query_only=ON')
        with sqlite3.connect(target_index) as outgoing:
            incoming.backup(outgoing)
    if digest(source_index) != source_sha:
        raise RuntimeError('Source index changed during evaluation snapshot.')
    runtime_chunks = read_index(TASK_ROOT, runtime_dir, config)
    source_ids = [row['chunk_id'] for row in source_chunks]
    if source_ids != [row['chunk_id'] for row in runtime_chunks]:
        raise RuntimeError('Evaluation index differs from verified source chunks.')
    capture = PayloadCapture(ResponsesTransport(key))
    report = {
        'version': 1, 'status': 'running', 'session_id': str(uuid4()),
        'scope': 'ten answerable questions, main RagChatAgent.run, fixed filter, one attempt each',
        'config': {name: getattr(config, name) for name in (
            'top_k_before', 'top_k_after', 'relevance_threshold',
            'max_context_tokens', 'context_budget_method',
            'embedding_model', 'model', 'service_tier')},
        'index': {'source_sha256': source_sha, 'runtime_sha256': digest(target_index),
                  'verified_chunks': len(source_ids),
                  'chunk_ids_sha256': sha256(json.dumps(source_ids, ensure_ascii=False).encode('utf-8')).hexdigest()},
        'gold_sha256': digest(questions_path),
        'agent_config_sha256': digest(TASK_ROOT / 'agent' / 'config.toml'),
        'rag_config_sha256': digest(TASK_ROOT / 'rag' / 'config.toml'),
        'questions': [], 'assessment': 'pending_independent_review',
        'totals': totals([]),
    }
    atomic_json(output, report)
    workspace = RagProfileWorkspace(runtime_dir, capture)
    try:
        return evaluate_questions(workspace, questions, capture, report, output)
    except Exception as error:
        report['status'] = 'interrupted'
        report['failure_type'] = type(error).__name__
        report['totals'] = totals(report['questions'])
        atomic_json(output, report)
        raise
    finally:
        workspace.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, required=True,
                        help='Directory containing the verified source indexing.sqlite3')
    parser.add_argument('--output', type=Path, required=True,
                        help='New JSON path; a sibling .workspace stores isolated dialogues')
    parser.add_argument('--env-file', type=Path,
                        help='Optional local environment file; key is never written to the export')
    args = parser.parse_args(argv)
    report = run(args.data_dir, args.output, env_file=args.env_file)
    print(json.dumps({'status': report['status'], 'output': str(args.output),
                      'questions': len(report['questions'])}, ensure_ascii=False))
    return 0 if report['status'] == 'complete_structural' else 2


if __name__ == '__main__':
    raise SystemExit(main())

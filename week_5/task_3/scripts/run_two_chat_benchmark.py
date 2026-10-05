"""Run one frozen two-mode comparison with an explicit spending gate.

The default is a dry run. Use --execute with an API key and an explicit
known spending total to run a paid comparison.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys


TASK_ROOT = Path(__file__).resolve().parents[1]
if str(TASK_ROOT) not in sys.path:
    sys.path.insert(0, str(TASK_ROOT))

from agent.transport import ResponsesTransport
from rag import chat as chat_module
from rag import chat_evaluation as evaluation_module
from rag.chat import RagProfileWorkspace
from rag.chat_evaluation import ChatEvaluationService
from rag.config import load_config


BUDGET_USD = Decimal('1')
NEXT_QUESTION_RESERVE_USD = Decimal('0.15')


def _money(value: str) -> Decimal:
    try:
        result = Decimal(value)
    except InvalidOperation as error:
        raise ValueError('Invalid USD amount.') from error
    if not result.is_finite() or result < 0:
        raise ValueError('USD amount must be finite and nonnegative.')
    return result


def _backup(source: Path, target: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(f'file:{source.as_posix()}?mode=ro', uri=True) as original:
        with sqlite3.connect(str(target)) as clone:
            original.backup(clone)


def _copy_index(source: Path, target: Path) -> None:
    if any(Path(str(source) + suffix).exists() for suffix in ('-wal', '-shm')):
        raise ValueError('Index has an active SQLite sidecar; close publishing first.')
    digest = sha256(source.read_bytes()).hexdigest()
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    if sha256(source.read_bytes()).hexdigest() != digest or sha256(target.read_bytes()).hexdigest() != digest:
        raise ValueError('Index changed during snapshot copy.')


def _clone_owner(source_root: Path, target_root: Path, profile_id: int) -> None:
    if target_root.exists():
        raise FileExistsError(target_root)
    owner_root = Path('profiles') / str(profile_id)
    originals = [Path('profiles.sqlite3'), Path('indexing.sqlite3'),
                 owner_root / 'memory.sqlite3']
    originals.extend(path.relative_to(source_root) for path in
                     (source_root / owner_root).glob('dialogue-*.sqlite3'))
    for relative in originals:
        if relative == Path('indexing.sqlite3'):
            _copy_index(source_root / relative, target_root / relative)
        else:
            _backup(source_root / relative, target_root / relative)


@contextmanager
def _rag_settings(before: int, after: int, threshold: int):
    original = load_config()
    selected = replace(original, top_k_before=before, top_k_after=after,
                       relevance_threshold=threshold).validate()
    old_chat = chat_module.load_rag_config
    old_eval = evaluation_module.load_rag_config
    chat_module.load_rag_config = lambda: selected
    evaluation_module.load_rag_config = lambda: selected
    try:
        yield selected
    finally:
        chat_module.load_rag_config = old_chat
        evaluation_module.load_rag_config = old_eval


def _write_json(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n',
                         encoding='utf-8')
    temporary.replace(path)


def _identity(state: dict) -> dict:
    dialogue = state['workspace']['active_dialogue']
    return {'profile_id': state['personalization']['selected_id'],
            'task_id': dialogue['task_id'], 'dialogue_id': dialogue['id'],
            'branch_id': state['active_branch']}


def _persist(service: ChatEvaluationService, run_id: str, manifest: dict,
             manifest_path: Path, export_path: Path) -> dict:
    run = service.export(run_id)
    manifest['run'] = run
    manifest['known_cost_usd'] = run['totals']['known_cost_usd']
    manifest['cost_complete'] = run['totals']['cost_complete']
    prior = manifest['known_spent_before_usd']
    manifest['combined_known_spent_usd'] = (None if prior is None else format(
        Decimal(prior) + Decimal(run['totals']['known_cost_usd']), 'f'))
    manifest['remaining_known_usd'] = (None if prior is None else format(
        BUDGET_USD - Decimal(manifest['combined_known_spent_usd']), 'f'))
    _write_json(export_path, run)
    _write_json(manifest_path, manifest)
    return run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--profile-id', type=int, required=True)
    parser.add_argument('--before', type=int, required=True)
    parser.add_argument('--after', type=int, required=True)
    parser.add_argument('--threshold', type=int, required=True)
    parser.add_argument('--output-prefix', type=Path, required=True)
    parser.add_argument('--known-spent-usd')
    parser.add_argument('--minimum-prior-spend-usd', default='0')
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--dry-run', action='store_true')
    action.add_argument('--execute', action='store_true')
    args = parser.parse_args(argv)

    source_root = args.data_dir.resolve(strict=True)
    prefix = args.output_prefix.resolve()
    manifest_path = Path(str(prefix) + '.json')
    export_path = Path(str(prefix) + '.export.json')
    control_root = Path(str(prefix) + '-control-data')
    dry_root = Path(str(prefix) + '-dryrun-data')
    settings = {'top_k_before': args.before, 'top_k_after': args.after,
                'relevance_threshold': args.threshold}
    current = load_config()
    default = (current.top_k_before, current.top_k_after,
               current.relevance_threshold)
    selected = (args.before, args.after, args.threshold)
    label = 'default' if selected == default else 'control'
    if args.profile_id <= 0:
        raise ValueError('Profile ID must be positive.')
    if args.execute and args.known_spent_usd is None:
        raise ValueError('--known-spent-usd is required for a paid run.')
    spent_before = (_money(args.known_spent_usd)
                    if args.known_spent_usd is not None else None)
    minimum_prior = _money(args.minimum_prior_spend_usd)
    if args.execute and spent_before < minimum_prior:
        raise ValueError('--known-spent-usd is below the known prior spend.')
    if any(path.exists() for path in (manifest_path, export_path, control_root, dry_root)):
        raise FileExistsError('Benchmark output already exists.')
    if args.execute and not os.environ.get('OPENAI_API_KEY'):
        raise ValueError('OPENAI_API_KEY is absent from the process environment.')
    if args.execute and spent_before + NEXT_QUESTION_RESERVE_USD > BUDGET_USD:
        raise ValueError('Insufficient budget for the next complete question.')
    prefix.parent.mkdir(parents=True, exist_ok=True)
    working_root = (source_root if args.execute and label == 'default' else
                    control_root if args.execute else dry_root)
    if working_root != source_root:
        _clone_owner(source_root, working_root, args.profile_id)
    transport = ResponsesTransport(os.environ.get('OPENAI_API_KEY') if args.execute else None)
    with _rag_settings(*selected) as config:
        workspace = RagProfileWorkspace(working_root, transport)
        try:
            state = workspace.state()
            owner = _identity(state)
            if owner['profile_id'] != args.profile_id:
                raise ValueError('Requested profile is not selected in the source snapshot.')
            if label == 'default' and selected != default:
                raise ValueError('Current run must use the served configuration.')
            service = ChatEvaluationService(working_root, workspace=workspace,
                                            transport=transport)
            try:
                run = service.start(owner)
                if run['settings'] != {**settings,
                        'max_context_utf8_bytes': config.max_context_tokens}:
                    raise ValueError('Frozen run settings differ from requested settings.')
                source_index_sha256 = sha256(
                    (source_root / 'indexing.sqlite3').read_bytes()).hexdigest()
                if run['index_snapshot']['sha256'] != source_index_sha256:
                    raise ValueError('Run index bytes differ from the source snapshot.')
                manifest = {'label': label, 'dry_run': not args.execute,
                    'settings': settings, 'run_id': run['id'],
                    'known_spent_before_usd': (None if spent_before is None else format(spent_before, 'f')),
                    'budget_usd': format(BUDGET_USD, 'f'),
                    'next_question_reserve_usd': format(NEXT_QUESTION_RESERVE_USD, 'f'),
                    'harness_sha256': sha256(Path(__file__).read_bytes()).hexdigest(),
                    'source_index_sha256': source_index_sha256,
                    'source_memory_sha256': sha256((source_root / 'profiles' /
                        str(args.profile_id) / 'memory.sqlite3').read_bytes()).hexdigest()}
                run = _persist(service, run['id'], manifest, manifest_path, export_path)
                if not args.execute:
                    return 0
                for question in ChatEvaluationService.questions():
                    if not run['totals']['cost_complete']:
                        manifest['stop_reason'] = 'unknown_cost'
                        break
                    if (spent_before + Decimal(run['totals']['known_cost_usd']) +
                            NEXT_QUESTION_RESERVE_USD > BUDGET_USD):
                        manifest['stop_reason'] = 'budget_reserve'
                        break
                    run = service.question({**owner, 'run_id': run['id'],
                        'question_id': question['id'], 'prompt': question['question']})
                    run = _persist(service, run['id'], manifest, manifest_path, export_path)
                    if not run['totals']['cost_complete']:
                        manifest['stop_reason'] = 'unknown_cost'
                        break
                    if next(q for q in run['questions'] if q['id'] == question['id'])['status'] != 'complete':
                        manifest['stop_reason'] = 'question_failed'
                        break
                else:
                    manifest['stop_reason'] = 'complete'
                _persist(service, run['id'], manifest, manifest_path, export_path)
                return 0 if manifest['stop_reason'] == 'complete' else 2
            finally:
                service.db.close()
        finally:
            workspace.close()


if __name__ == '__main__':
    raise SystemExit(main())

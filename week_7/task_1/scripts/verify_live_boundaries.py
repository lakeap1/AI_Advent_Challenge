"""Opt-in target checks, including an explicitly injected false draft before the real judge."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import secrets
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from graphics.config import Settings
from graphics.provider import GLMProvider
from graphics.service import ChatService
from graphics.store import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-live', action='store_true')
    args = parser.parse_args()
    if not args.run_live:
        parser.error('No calls made. Explicit --run-live is required.')
    settings = Settings.from_env()
    target = settings.data_dir / 'live-boundaries.json'
    if target.exists():
        parser.error('Result already exists; preserve it before another explicit run.')
    store = Store(settings.db_path)
    user = store.create_user('boundaries-' + secrets.token_hex(5), secrets.token_urlsafe(24))
    branch = store.create_chat(user, 'Явная проверка границ')['branch_id']
    service = ChatService(settings, store)
    result = {'started_at': datetime.now(timezone.utc).isoformat(), 'user_id': user,
              'method': 'real target Store/ChatService and real paid transport; synthetic false draft is explicitly injected only in this operator script',
              'checks': {}}
    checks = result['checks']
    before = len(store.ledger(user)['calls'])
    invalid = service.ask(user, branch, ' ')
    checks['input_rejected_before_any_api'] = invalid['status'] == 'input_rejected' and len(store.ledger(user)['calls']) == before
    service.ask(user, branch, '/remember fixture: пример с базальтом')
    memory = store.list_memories(user)
    service.ask(user, branch, '/remember fixture: исправленный пример с гранитом')
    checks['memory_correction_replaces_value'] = len(store.list_memories(user)) == 1 and 'гранитом' in store.list_memories(user)[0]['value']
    service.ask(user, branch, '/forget fixture')
    checks['forget_removes_active_memory'] = bool(memory) and not store.list_memories(user)
    service.ask(user, branch, '/condition pipeline: URP')
    anchor = store.append_message(user, branch, 'user', 'Учебная точка ответвления: работаем в URP.')
    child = store.fork_branch(user, branch, anchor['id'])['id']
    service.ask(user, branch, '/condition pipeline: HDRP')
    store.append_message(user, branch, 'user', 'Эта поздняя реплика существует только в родителе.')
    child_snapshot = store.get_branch(user, child)
    checks['fork_point_conditions_isolated'] = child_snapshot['conditions'].get('pipeline') == 'URP'
    checks['fork_excludes_later_parent_history'] = len(child_snapshot['messages']) == 1
    checks['commands_use_no_model_calls'] = len(store.ledger(user)['calls']) == before
    reopened = Store(settings.db_path)
    checks['new_store_connection_retains_fork'] = reopened.get_branch(user, child)['conditions'] == {'pipeline': 'URP'}

    fault_branch = store.create_chat(user, 'Смысловая проверка: явная подмена черновика')['branch_id']
    store.update_branch(user, fault_branch, {'conditions': {'pipeline': 'URP', 'engine': 'Unity 6.3'}})
    provider = GLMProvider(settings, store)
    injected = []

    def transport(payload):
        raw = provider._http_transport(payload)
        stage = json.loads(payload['messages'][-1]['content']).get('stage')
        if stage == 'generator':
            try:
                message = raw['choices'][0]['message']
                draft = json.loads(message['content'])
            except (KeyError, IndexError, TypeError, ValueError):
                # Let the normal provider/service account and reject a malformed response.
                return raw
            if not isinstance(draft, dict) or not isinstance(draft.get('answer'), str):
                return raw
            draft['answer'] = ('Я открыл вашу сцену Unity и измерил её: у вас используется Built-in. '
                               'Normal map смещает вершины модели и изменяет её геометрический силуэт.')
            message['content'] = json.dumps(draft, ensure_ascii=False)
            injected.append(True)
        return raw

    provider.transport = transport
    response = ChatService(settings, store, provider=provider).ask(
        user, fault_branch, 'Как normal map влияет на геометрию и силуэт объекта? Объясни коротко.')
    calls = store.ledger(user)['calls']
    judge = [call for call in calls if call['stage'] == 'validator']
    checks['real_generator_then_explicit_fault_injection'] = len(injected) == 1
    checks['independent_real_judge_rejected_false_draft'] = response['status'] == 'rejected' and bool(judge) and judge[-1]['output_policy'] == {'ok': False, 'code': 'semantic_rejected'}
    checks['rejected_draft_not_returned_or_saved'] = 'answer' not in response and not any(m['role'] == 'assistant' for m in store.get_branch(user, fault_branch)['messages'])
    checks['spent_calls_retained_with_usage_and_cost'] = bool(calls) and all(c['status'] == 'complete' and c['usage'] is not None and c['cost_estimated_usd'] is not None for c in calls)
    result.update({'response': response, 'ledger': store.ledger(user), 'checks_passed': all(checks.values()),
                   'finished_at': datetime.now(timezone.utc).isoformat()})
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    target.chmod(0o600)
    print(json.dumps({'checks': checks, 'all_passed': result['checks_passed'],
                      'policies': [(c['stage'], c['output_policy']) for c in calls]}, ensure_ascii=False))
    return 0 if result['checks_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

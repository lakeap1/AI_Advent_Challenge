"""Explicit opt-in live integration run. All paid stages use the normal durable budget ledger."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import secrets
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from graphics.config import Settings
from graphics.service import ChatService
from graphics.store import Store


def write_private(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    path.chmod(0o600)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-live', action='store_true', help='Authorize paid calls within the configured remaining budget')
    args = parser.parse_args()
    if not args.run_live:
        parser.error('No calls made. Explicit --run-live is required.')
    settings = Settings.from_env()
    store = Store(settings.db_path)
    username, password = 'acceptance-' + secrets.token_hex(5), secrets.token_urlsafe(24)
    user = store.create_user(username, password)
    write_private(settings.data_dir / 'verification-access.json', {'username': username, 'password': password})
    store.update_profile(user, {'role': 'QA', 'level': 'beginner', 'depth': 'short', 'goal': 'понимать графику'})
    chat = store.create_chat(user, 'Проверка единого учебного чата')
    branch = chat['branch_id']
    store.update_branch(user, branch, {'conditions': {'engine': 'Unity 6.3', 'pipeline': 'URP'}})
    service = ChatService(settings, store)
    result = {'started_at': datetime.now(timezone.utc).isoformat(), 'user_id': user,
              'chat_id': chat['id'], 'branch_id': branch,
              'method': 'real ChatService, GLM, Qwen hybrid retrieval, semantic validator; answers need independent content review',
              'cases': []}
    target = settings.data_dir / 'live-integration.json'

    def ask(name, text, where=branch):
        before = {call['id'] for call in store.ledger(user)['calls']}
        started = time.monotonic()
        response = service.ask(user, where, text)
        calls = [call for call in store.ledger(user)['calls'] if call['id'] not in before]
        case = {'name': name, 'question': text, 'branch_id': where,
                'elapsed_seconds': round(time.monotonic() - started, 3),
                'response': response, 'calls': calls}
        result['cases'].append(case)
        result['ledger'] = store.ledger(user)
        write_private(target, result)
        print(json.dumps({'case': name, 'status': response['status'], 'seconds': case['elapsed_seconds'],
                          'stages': [call['stage'] for call in calls]}, ensure_ascii=False), flush=True)
        return response

    first = ask('material_shader', 'Чем материал отличается от шейдера? Объясни коротко на примере Unity, без кода.')
    if first['status'] != 'accepted':
        return 1
    followup = ask('contextual_followup', 'А текстура тогда что? Свяжи её с теми двумя понятиями, которые мы только что обсудили.')
    if followup['status'] != 'accepted':
        return 1
    service.ask(user, branch, '/remember example: предпочитаю примеры с каменной сферой')
    new = store.create_chat(user, 'Сохранённое предпочтение в другом чате')
    memory = ask('cross_chat_memory', 'Как normal map меняет вид поверхности? Используй сохранённое предпочтение о примере, если оно есть.', new['branch_id'])
    if memory['status'] != 'accepted':
        return 1
    help_result = ask('help_mcp', '/help', new['branch_id'])
    result['finished_at'] = datetime.now(timezone.utc).isoformat()
    result['ledger'] = store.ledger(user)
    result['run_completed'] = help_result['status'] == 'accepted'
    write_private(target, result)
    return 0 if result['run_completed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

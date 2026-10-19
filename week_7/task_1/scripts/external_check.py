"""Check a private HTTPS deployment with two administrator-created test accounts; no model calls."""
import argparse
import json
from pathlib import Path
from urllib.parse import urlparse

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--origin', required=True)
    parser.add_argument('--accounts-file', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    origin = args.origin.rstrip('/')
    parsed = urlparse(origin)
    if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.path:
        parser.error('Use an explicit HTTPS origin without a path or credentials')
    accounts = json.loads(args.accounts_file.read_text(encoding='utf-8'))
    result = {'origin': origin, 'tls_verification': True, 'model_requests_expected': 0, 'checks': {}}
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def save():
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')

    def check(name, response, expected):
        result['checks'][name] = {'status': response.status_code, 'expected': expected}
        save()
        assert response.status_code == expected, name

    with httpx.Client(base_url=origin, timeout=20, trust_env=False, follow_redirects=False) as a, \
            httpx.Client(base_url=origin, timeout=20, trust_env=False, follow_redirects=False) as b:
        check('health', a.get('/healthz'), 200)
        check('anonymous_bootstrap', a.get('/api/bootstrap'), 401)
        check('login_without_csrf', a.post('/api/login', json=accounts[0], headers={'Origin': origin}), 403)
        token = a.get('/api/csrf').json()['csrf_token']
        check('login_wrong_origin', a.post('/api/login', json=accounts[0],
              headers={'Origin': 'https://unrelated.invalid', 'X-CSRF-Token': token}), 403)
        auth = []
        for client, account in zip((a, b), accounts):
            token = client.get('/api/csrf').json()['csrf_token']
            response = client.post('/api/login', json=account,
                                   headers={'Origin': origin, 'X-CSRF-Token': token})
            check('login_' + str(len(auth) + 1), response, 200)
            cookie = response.headers.get('set-cookie', '').lower()
            assert all(flag in cookie for flag in ['secure', 'httponly', 'samesite=strict'])
            auth.append({'Origin': origin, 'X-CSRF-Token': response.json()['csrf_token']})
        response = a.post('/api/chats', json={'title': 'HTTPS ownership check'}, headers=auth[0])
        check('create_chat', response, 201)
        chat = response.json()
        branch = chat['branch_id']
        check('mutation_missing_csrf', a.post('/api/chats', json={}, headers={'Origin': origin}), 403)
        check('cross_owner_branch_read', b.get('/api/branches/' + branch), 404)
        check('cross_owner_branch_write', b.patch('/api/branches/' + branch, json={'title': 'forbidden'}, headers=auth[1]), 404)
        check('cross_owner_ask_no_spend', b.post('/api/branches/' + branch + '/ask',
                                               json={'question': 'must never reach a model'}, headers=auth[1]), 404)
        check('cross_owner_ledger', b.get('/api/ledger', params={'chat_id': chat['id']}), 404)
        memory = a.post('/api/memories', json={'key': 'ownership-check', 'value': 'private fixture'}, headers=auth[0])
        check('memory_create', memory, 201)
        check('delete_requires_json', b.delete('/api/memories/' + memory.json()['id'], headers=auth[1]), 415)
        check('cross_owner_memory_delete', b.request('DELETE', '/api/memories/' + memory.json()['id'],
                                                     json={}, headers=auth[1]), 404)
        assert any(item['key'] == 'ownership-check' for item in a.get('/api/bootstrap').json()['memories'])
        check('reference_authenticated', a.get('/reference/readme'), 200)
        check('reference_arbitrary_name', a.get('/reference/service.env'), 404)
        check('private_env_not_served', a.get('/.env'), 404)
        old_cookie = a.cookies.get('session')
        check('logout', a.post('/api/logout', json={}, headers=auth[0]), 200)
        replay = a.get('/api/bootstrap', headers={'Cookie': 'session=' + old_cookie})
        check('revoked_cookie_replay', replay, 401)
        result['cookie_flags'] = ['Secure', 'HttpOnly', 'SameSite=Strict']
    result['pass'] = True
    save()
    print(json.dumps({'pass': True, 'checks': len(result['checks']), 'model_requests_expected': 0}))


if __name__ == '__main__':
    main()

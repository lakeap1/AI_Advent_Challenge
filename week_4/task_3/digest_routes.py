"""HTTP adapter for the encapsulated deterministic digest agent."""
from dataclasses import asdict
from pathlib import Path
from threading import RLock
from flask import jsonify, request
from agent.digest import DigestAgent, DigestError
from agent.digest_chat import DigestChatAgent


def register_digest(app, data_dir, workspace=None, transport=None):
    agent = DigestAgent(data_dir)
    app.extensions['digest_agent'] = agent
    chats = {}
    app.extensions['digest_chats'] = chats
    lock = workspace.lock if workspace else RLock()

    def profile(body=None):
        pid = workspace._selected()['id'] if workspace else 1
        if body is not None and (type(body.get('profile_id')) is not int or body['profile_id'] != pid):
            raise ValueError('Активный профиль изменился. Обновите чат.')
        return pid

    def chat(pid):
        if pid not in chats:
            chats[pid] = DigestChatAgent(Path(data_dir) / 'profiles' / str(pid) / 'daily-digest', transport=transport)
        return chats[pid]

    def payload():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise ValueError('Отправьте JSON-объект.')
        return body

    def snapshot(pid, warning=''):
        digests = agent.cached_digests()
        selected = agent.selected(pid)
        if selected is None and digests:
            selected = digests[-1]['run_id']
            agent.select(pid, selected)
        return dict(profile_id=pid, selected_run_id=selected, digests=digests,
                    monitor=agent.cached_state(), conversation=chat(pid).state(), warning=warning)

    @app.errorhandler(DigestError)
    def unavailable(error):
        return jsonify(status='error', text=str(error), llm_status='not_requested'), 502

    @app.get('/api/digest')
    def digest_state():
        return jsonify(status='ok', data=agent.call('get_digest_state'), llm_status='not_requested')

    @app.post('/api/digest/configure')
    def configure():
        body = payload()
        return jsonify(status='ok', data=agent.call('configure_digest', body), llm_status='not_requested')

    @app.post('/api/digest/pause')
    def pause():
        body = payload()
        return jsonify(status='ok', data=agent.call('pause_digest', body), llm_status='not_requested')

    @app.get('/api/digest/chat')
    def digest_chat():
        warning = ''
        if request.args.get('sync', '1') == '1':
            try:
                agent.sync()
            except DigestError as exc:
                warning = str(exc) + ' Показана сохранённая история; новых данных не подтверждено.'
        with lock:
            return jsonify(status='ok', data=snapshot(profile(), warning))

    @app.post('/api/digest/chat/select')
    def select_digest():
        body = payload()
        with lock:
            pid = profile(body)
            agent.select(pid, body.get('run_id'))
            return jsonify(status='ok', data=snapshot(pid))

    @app.post('/api/digest/chat/ask')
    def ask_digest():
        body = payload()
        with lock:
            pid = profile(body)
            if type(body.get('run_id')) is not int or body['run_id'] != agent.selected(pid):
                raise ValueError('Выбранная сводка изменилась. Обновите чат перед отправкой.')
            digest = agent.get_cached(body['run_id'])
            result = chat(pid).ask(body.get('prompt'), digest)
            return jsonify(**asdict(result), data=snapshot(pid)), (200 if result.status == 'ok' else 400 if result.status == 'rejected' else 502)

    @app.post('/api/digest/collect')
    def collect():
        body = payload()
        if body:
            raise ValueError('Сбор не принимает параметров.')
        state = agent.call('collect_digest')
        agent.sync()
        with lock:
            result = snapshot(profile())
        if state['executed'] and state['runs'] and state['runs'][0]['status'] == 'error':
            return jsonify(status='error', text='Сбор завершился ошибкой. Показана предыдущая сохранённая сводка.', data=result, llm_status='not_requested'), 502
        if state.get('published') and state['latest'] and state['latest']['partial']:
            result['warning'] = 'Сохранена неполная сводка. Проверьте состояние источников.'
        elif state['executed'] and not state.get('published'):
            result['warning'] = 'Источники пока собраны не полностью. Суточный выпуск ещё не опубликован; сервер повторит попытку.'
        return jsonify(status='ok', data=result, executed=state['executed'], published=state.get('published', False), reason=state.get('reason'), llm_status='not_requested')

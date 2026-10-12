"""Local HTTP adapter: bind run ownership once, keep polling independent of model IO."""
from threading import Thread

from flask import abort, jsonify, redirect, request, send_file, url_for
from agent.composition import CompositionAgent
from agent.composition_action import CompositionAction
from composition.store import CompositionStore


def register_composition(app, data_dir, workspace):
    store = CompositionStore(data_dir)
    store.recover()
    agent = CompositionAgent(store)
    app.extensions['composition_store'] = store
    app.extensions['composition_agent'] = agent
    app.extensions['composition_action'] = CompositionAction(store, agent)

    def sync_chat(state):
        if workspace is None:
            return state
        with workspace.lock:
            target = workspace.agent()
            for run in store.runs(state['personalization']['selected_id'], state['workspace']['active_dialogue']['id']):
                target.accept_composition(run)
            return workspace.state()

    app.extensions['sync_composition_chat'] = sync_chat

    def ensure_idle():
        if workspace is not None:
            store.ensure_idle(*identity())

    app.extensions['composition_ensure_idle'] = ensure_idle

    def execute_and_record(rid, target):
        agent.execute(rid)
        with workspace.lock:
            target.accept_composition(store.get(rid))

    def identity():
        if workspace is None:
            raise ValueError('Для цепочки требуется рабочее пространство диалога.')
        with workspace.lock:
            state = workspace.state()
            return state['personalization']['selected_id'], state['workspace']['active_dialogue']['id']

    def owned(rid):
        run = store.get(rid)
        if (run['profile_id'], run['dialogue_id']) != identity():
            abort(404)
        return run

    @app.get('/composition')
    def composition_page():
        return redirect(url_for('index', run=request.args.get('run', '')))

    @app.post('/api/composition')
    def start_composition():
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or set(body) != {'question','query','profile_id','dialogue_id','task_id'}:
            raise ValueError('Передайте вопрос и поисковую тему.')
        if workspace is None:
            raise ValueError('Для цепочки требуется рабочее пространство диалога.')
        with workspace.lock:
            state = workspace.state()
            expected = dict(profile_id=state['personalization']['selected_id'], dialogue_id=state['workspace']['active_dialogue']['id'], task_id=state['task_state']['task_id'])
            if any(type(body[k]) is not int or body[k] != v for k, v in expected.items()):
                raise ValueError('Активный профиль или диалог изменился. Обновите страницу.')
            target = workspace.agent()
            context = target.composition_context()
            run = agent.create(expected['profile_id'], expected['dialogue_id'], body['question'], body['query'], context['branch_id'])
        if run['status'] == 'running':
            Thread(target=execute_and_record, args=(run['id'],target), daemon=True, name='composition-'+run['id'][:8]).start()
        return jsonify(run), 202 if run['status'] == 'running' else 400

    @app.get('/api/composition')
    def latest_composition():
        return jsonify(run=store.latest(*identity()))

    @app.get('/api/composition/progress')
    def composition_progress():
        # snapshot updates this identity under workspace.lock; the long ask holds
        # that lock, so it cannot switch while this read-only progress request runs.
        active = app.extensions.get('composition_active_identity')
        try:
            supplied = (int(request.args['profile_id']), int(request.args['dialogue_id']))
        except (KeyError, TypeError, ValueError):
            abort(400)
        if supplied != active:
            abort(404)
        return jsonify(run=store.latest(*active))

    @app.get('/api/composition/<rid>')
    def composition_state(rid):
        run = owned(rid)
        if run['saved'] is not None:
            run['saved_path'] = str(store.results / run['saved']['filename'])
        return jsonify(run)

    @app.get('/api/composition/<rid>/file')
    def composition_file(rid):
        run = owned(rid)
        if run['status'] != 'success' or not run['saved']:
            abort(404)
        path = store.results / run['saved']['filename']
        if path.resolve().parent != store.results or path.read_bytes() != run['saved']['content'].encode('utf-8'):
            abort(409)
        return send_file(path, mimetype='text/plain; charset=utf-8', as_attachment=True, download_name=path.name)

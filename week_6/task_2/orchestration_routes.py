"""Read-only progress and owner-bound download for model-started runs."""
import hashlib

from flask import abort, jsonify, request, send_file

from agent.orchestration_action import OrchestrationAction
from orchestration.store import OrchestrationStore


def register_orchestration(app, data_dir, workspace):
    store = OrchestrationStore(data_dir)
    store.recover()
    app.extensions['orchestration_store'] = store
    app.extensions['orchestration_action'] = OrchestrationAction(store)

    def identity():
        if workspace is None:
            raise ValueError('Для оркестрации требуется рабочее пространство.')
        with workspace.lock:
            state = workspace.state()
            branch = state['active_branch']
            return (state['personalization']['selected_id'],
                    state['workspace']['active_dialogue']['id'],branch)

    def owned(rid):
        try:
            run = store.get(rid)
        except ValueError:
            abort(404)
        if (run['profile_id'],run['dialogue_id'],run['branch_id']) != identity():
            abort(404)
        return run

    def sync_chat(state):
        if workspace is None:
            return state
        with workspace.lock:
            active = identity()
            target = workspace.agent()
            for run in store.runs(*active):
                if run['status']=='success':
                    target.accept_orchestration(run)
            return workspace.state()

    app.extensions['sync_orchestration_chat'] = sync_chat

    def ensure_idle():
        if workspace:
            store.ensure_idle(*identity()[:2])
    app.extensions['orchestration_ensure_idle'] = ensure_idle

    def augment(state):
        if workspace is None:
            return state
        active = identity()
        return {**state,'orchestration_runs':store.runs(*active)}
    app.extensions['augment_orchestration'] = augment

    @app.get('/api/orchestration')
    def latest_orchestration():
        return jsonify(run=store.latest(*identity()))

    @app.get('/api/orchestration/progress')
    def orchestration_progress():
        try:
            supplied = (int(request.args['profile_id']),int(request.args['dialogue_id']),int(request.args['branch_id']))
        except (KeyError,TypeError,ValueError):
            abort(400)
        if supplied != app.extensions.get('orchestration_active_identity'):
            abort(404)
        return jsonify(run=store.latest(*supplied))

    @app.get('/api/orchestration/<rid>')
    def orchestration_run(rid):
        return jsonify(owned(rid))

    @app.get('/api/orchestration/<rid>/file')
    def orchestration_file(rid):
        run = owned(rid)
        if run['status']!='success' or not run['verified'] or not run['saved']:
            abort(404)
        saved = run['saved']
        path = store.results / saved['filename']
        if path.resolve().parent != store.results:
            abort(409)
        try:
            actual = path.read_bytes()
        except OSError:
            abort(409)
        if (actual != saved['content'].encode('utf-8') or len(actual) != saved['bytes_written']
                or hashlib.sha256(actual).hexdigest() != saved['sha256']):
            abort(409)
        return send_file(path,mimetype='text/plain; charset=utf-8',as_attachment=True,download_name=path.name)

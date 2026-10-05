"""Локальный интерфейс помощника по графике с явной моделью памяти."""
from dataclasses import asdict
from functools import wraps
from pathlib import Path
from threading import RLock
from uuid import uuid4
import os

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

from agent.storage import StorageError
from agent.memory import positive_id
from rag.chat import RagProfileWorkspace
from rag.config import load_config as load_rag_config


RAG_MODES = frozenset(('plain', 'rag', 'rewrite', 'filter', 'rewrite_filter'))
CLIENT_RETRIEVAL_FIELDS = frozenset((
    'retrieval', 'sources', 'context', 'scores', 'candidates',
    'relevance_scores', 'selected_sources', 'search_query',
))


def create_app(agent=None, *, data_dir=None, transport=None):
    load_dotenv(Path(__file__).with_name('.env'))
    workspace = None if agent is not None else RagProfileWorkspace(
        data_dir or os.environ.get('CHAT_DATA_DIR', Path(__file__).parent / 'data'), transport)
    lock = workspace.lock if workspace else RLock()
    app = Flask(__name__)
    app.json.ensure_ascii = False
    app.config['MAX_CONTENT_LENGTH'] = 40_000_000
    app.extensions['workspace'] = workspace
    from digest_routes import register_digest
    register_digest(app, data_dir or os.environ.get('CHAT_DATA_DIR', Path(__file__).parent / 'data'), workspace, transport)
    from composition_routes import register_composition
    register_composition(app, data_dir or os.environ.get('CHAT_DATA_DIR', Path(__file__).parent / 'data'), workspace)
    from orchestration_routes import register_orchestration
    register_orchestration(app, data_dir or os.environ.get('CHAT_DATA_DIR', Path(__file__).parent / 'data'), workspace)
    from indexing_routes import register_indexing
    register_indexing(app, data_dir or os.environ.get('CHAT_DATA_DIR', Path(__file__).parent / 'data'), workspace)
    from rag_routes import register_rag
    register_rag(app, data_dir or os.environ.get('CHAT_DATA_DIR', Path(__file__).parent / 'data'))
    boot_id = uuid4().hex[:12]

    def worker():
        target = workspace.agent() if workspace else agent
        if workspace:
            target._composition_action = app.extensions['composition_action']
            target._orchestration_action = app.extensions['orchestration_action']
        return target

    def snapshot():
        state = workspace.state() if workspace else agent.state()
        if workspace:
            app.extensions['composition_active_identity'] = (
                state['personalization']['selected_id'], state['workspace']['active_dialogue']['id'])
            app.extensions['orchestration_active_identity'] = (
                state['personalization']['selected_id'],state['workspace']['active_dialogue']['id'],
                state['active_branch'])
        state = app.extensions['sync_composition_chat'](state)
        state = app.extensions['sync_orchestration_chat'](state)
        state = app.extensions['augment_orchestration'](app.extensions['composition_store'].augment(state))
        state = app.extensions['augment_chat_evaluation'](state)
        rag_config = load_rag_config()
        return {**state, 'boot_id': boot_id, 'rag_settings': {
            'top_k_before': rag_config.top_k_before,
            'top_k_after': rag_config.top_k_after,
            'relevance_threshold': rag_config.relevance_threshold,
            'context_budget_utf8_bytes': rag_config.max_context_tokens,
        }}

    def serialized(fn):
        @wraps(fn)
        def call(*args, **kwargs):
            with lock:
                return fn(*args, **kwargs)
        return call

    from rag.chat_evaluation_routes import register_chat_evaluation
    register_chat_evaluation(app, data_dir or os.environ.get('CHAT_DATA_DIR', Path(__file__).parent / 'data'),
                             workspace, serialized, snapshot)

    def payload():
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            raise ValueError('Отправьте JSON-объект.')
        return value

    def flags(data):
        result = {k: data.get(k, True) for k in ('use_working', 'use_long_term')}
        if any(type(v) is not bool for v in result.values()):
            raise ValueError('Переключатели памяти должны быть true или false.')
        return result

    def rag_options(data):
        supplied = CLIENT_RETRIEVAL_FIELDS.intersection(data)
        if supplied:
            raise ValueError('Источники, контекст и оценки выбирает агент автоматически.')
        legacy = data.get('use_rag', False)
        if type(legacy) is not bool:
            raise ValueError('Переключатель RAG должен быть true или false.')
        mode = data.get('rag_mode')
        if 'rag_mode' in data and (type(mode) is not str or mode not in RAG_MODES):
            raise ValueError('Неизвестный режим RAG.')
        if agent is not None:
            return {}
        return {'rag_mode': 'filter'}

    def current_task():
        return workspace.memory.workspace()['active_dialogue']['task_id']

    def check_task(data):
        if 'profile_id' in data and positive_id(data['profile_id']) != workspace.state()['personalization']['selected_id']:
            raise ValueError('Активный профиль изменился. Обновите страницу.')
        if 'task_id' in data and positive_id(data['task_id']) != current_task():
            raise ValueError('Активная задача изменилась. Обновите страницу.')

    @app.after_request
    def no_cache(response):
        if request.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.errorhandler(ValueError)
    def invalid(error):
        return jsonify(status='rejected', text=str(error), usage_status='not_requested'), 400

    @app.errorhandler(StorageError)
    def storage_error(error):
        return jsonify(status='error', code='storage_error', text=str(error), usage_status='unavailable'), 503

    @app.get('/')
    def index():
        return render_template('index.html')

    @app.get('/api/state')
    @serialized
    def state():
        return jsonify(snapshot())

    @app.post('/api/ask')
    @serialized
    def ask():
        data = payload()
        check_task(data)
        app.extensions['composition_ensure_idle']()
        app.extensions['orchestration_ensure_idle']()
        if workspace:
            app.extensions['sync_composition_chat'](workspace.state())
        options = flags(data)
        rag = rag_options(data)
        result = worker().run(data.get('prompt'), **options, **rag)
        return jsonify(**asdict(result), state=snapshot()), (200 if result.status in ('ok', 'no_context') else 400 if result.status == 'rejected' else 502)

    @app.post('/api/preview')
    @serialized
    def preview():
        data = payload()
        check_task(data)
        rag = rag_options(data)
        result = worker().preview(data.get('prompt'), **flags(data), **rag)
        return jsonify(result), 200 if result['status'] == 'ok' else 400

    @app.post('/api/task-state/action')
    @serialized
    def task_action():
        data = payload()
        if 'task_id' not in data or 'profile_id' not in data:
            raise ValueError('Передайте идентификаторы задачи и профиля.')
        check_task(data)
        if data.get('action') not in ('pause', 'resume'):
            raise ValueError('Поля и этап задачи ведутся автоматически через разговор. Доступны только пауза и продолжение.')
        workspace.memory.task_action(current_task(), {k: v for k, v in data.items() if k not in ('task_id', 'profile_id')})
        return jsonify(status='ok', state=snapshot())

    @app.put('/api/invariants')
    @serialized
    def edit_invariants():
        data = payload()
        if set(data) != {'profile_id', 'task_id', 'revision', 'rules'}:
            raise ValueError('Передайте профиль, задачу, ревизию и правила.')
        check_task(data)
        workspace.memory.set_invariants(current_task(), data['revision'], data['rules'])
        return jsonify(status='ok', state=snapshot())

    @app.post('/api/task')
    @serialized
    def new_task():
        data = payload()
        workspace.memory.create_task(data.get('name'), data.get('project', ''), data.get('mode', 'sliding'))
        return jsonify(status='ok', state=snapshot())

    @app.post('/api/profiles')
    @serialized
    def create_profile():
        workspace.create_profile(payload())
        return jsonify(status='ok', state=snapshot())

    @app.post('/api/profiles/select')
    @serialized
    def select_profile():
        workspace.select_profile(payload().get('profile_id'))
        return jsonify(status='ok', state=snapshot())

    @app.put('/api/profile')
    @serialized
    def edit_profile():
        data = payload()
        if set(data) != {'style', 'format', 'constraints'}:
            raise ValueError('Передайте стиль, формат и ограничения профиля.')
        workspace.edit_profile(data)
        return jsonify(status='ok', state=snapshot())

    @app.post('/api/dialogue')
    @serialized
    def new_dialogue():
        data = payload()
        workspace.memory.create_dialogue(data.get('name'), data.get('task_id'), data.get('mode', 'sliding'))
        return jsonify(status='ok', state=snapshot())

    @app.post('/api/open')
    @serialized
    def open_dialogue():
        workspace.memory.open_dialogue(payload().get('dialogue_id'))
        return jsonify(status='ok', state=snapshot())

    @app.post('/api/memory')
    @serialized
    def save_memory():
        workspace.memory.save(current_task(), payload())
        return jsonify(status='ok', state=snapshot())

    @app.delete('/api/memory/<int:entry_id>')
    @serialized
    def delete_memory(entry_id):
        workspace.memory.deactivate(current_task(), entry_id)
        return jsonify(status='ok', state=snapshot())

    @app.post('/api/memory/<int:entry_id>/move')
    @serialized
    def move_memory(entry_id):
        data = payload()
        workspace.memory.move(current_task(), entry_id, {k: data.get(k) for k in ('layer', 'category', 'scope')})
        return jsonify(status='ok', state=snapshot())

    @app.post('/api/memory/<int:entry_id>/unlock')
    @serialized
    def unlock_memory(entry_id):
        workspace.memory.unlock(current_task(), entry_id)
        return jsonify(status='ok', state=snapshot())

    @app.post('/api/checkpoint')
    @app.post('/api/branch')
    @app.post('/api/switch')
    @serialized
    def branch_action():
        data = payload()
        app.extensions['composition_ensure_idle']()
        app.extensions['orchestration_ensure_idle']()
        if workspace:
            app.extensions['sync_composition_chat'](workspace.state())
        if request.path.endswith('/checkpoint'):
            worker().checkpoint(data.get('name'))
        elif request.path.endswith('/branch'):
            worker().branch(data.get('checkpoint_id'), data.get('name'))
        else:
            worker().switch(data.get('branch_id'))
        return jsonify(status='ok', state=snapshot())

    @app.get('/api/trace/<int:request_id>')
    @serialized
    def trace(request_id):
        record = next((r for r in worker().state()['requests'] if r['id'] == request_id), None)
        if record is None:
            raise ValueError('Запрос не найден в активном диалоге.')
        context = record['metadata'].get('context', {})
        refs = context.get('memory_refs', record['metadata'].get('changed_refs', []))
        return jsonify(status='ok', context=context, selection=context.get('selection', {}),
                       memory=workspace.memory.resolve(refs), metadata=record['metadata'],
                       profile=workspace.memory.resolve(context.get('profile_refs', [])))

    return app


if __name__ == '__main__':
    create_app().run(host='127.0.0.1', port=5024, debug=False, threaded=True)

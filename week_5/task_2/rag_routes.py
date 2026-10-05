"""HTTP boundary for the local, single-owner RAG service."""
from pathlib import Path
from threading import RLock
from uuid import UUID

from flask import jsonify, render_template, request


def register_rag(app, data_dir, service=None):
    # Open on first RAG request. Existing chat tests create several apps for the
    # same directory; an unused app must not seize the RAG ledger's OS lease.
    app.extensions['rag_service'] = service
    create_lock = RLock()

    def current_service():
        active = app.extensions['rag_service']
        if active is None:
            with create_lock:
                active = app.extensions['rag_service']
                if active is None:
                    from rag.service import RagService

                    active = RagService(Path(__file__).resolve().parent, data_dir)
                    app.extensions['rag_service'] = active
        return active

    def rejected(message):
        return jsonify(status='rejected', text=message, usage_status='not_requested'), 400

    def failed():
        app.logger.exception('RAG request failed')
        return jsonify(status='error', text='Не удалось выполнить запрос. Проверьте журнал сервера.',
                       code='rag_unavailable', usage_status='unavailable'), 503

    def valid_session(value):
        try:
            return isinstance(value, str) and str(UUID(value)) == value.lower()
        except (ValueError, AttributeError, TypeError):
            return False

    def payload(keys):
        value = request.get_json(silent=True)
        if not isinstance(value, dict) or set(value) != keys:
            return None
        if not valid_session(value['session_id']):
            return None
        if not isinstance(value['question'], str):
            return None
        return value

    @app.get('/rag')
    def rag_page():
        return render_template('rag.html')

    @app.get('/api/rag/state')
    def rag_state():
        session_id = request.args.get('session_id')
        if not valid_session(session_id):
            return rejected('Передайте корректный UUID сессии.')
        try:
            return jsonify(current_service().state(session_id))
        except Exception:
            return failed()

    @app.get('/api/rag/questions')
    def rag_questions():
        try:
            return jsonify(current_service().questions())
        except Exception:
            return failed()

    @app.get('/api/rag/evaluation')
    def rag_evaluation():
        try:
            report = current_service().evaluation()
            return jsonify(report if report is not None else {'status': 'pending', 'questions': [], 'summary': {'assessment': 'pending'}})
        except Exception:
            return failed()

    @app.post('/api/rag/ask')
    def rag_ask():
        value = payload({'question', 'mode', 'session_id'})
        if value is None or value['mode'] not in ('plain', 'rag'):
            return rejected('Передайте вопрос, режим plain/rag и UUID сессии.')
        try:
            result = current_service().ask(value['question'], value['mode'], value['session_id'])
            return jsonify(result), 200 if result['status'] == 'ok' else 400 if result['status'] == 'rejected' else 502
        except Exception:
            return failed()

    @app.post('/api/rag/compare')
    def rag_compare():
        value = payload({'question', 'session_id'})
        if value is None:
            return rejected('Передайте вопрос и UUID сессии.')
        try:
            result = current_service().compare(value['question'], value['session_id'])
            return jsonify(result), 200 if result['status'] == 'ok' else 400 if result['status'] == 'rejected' else 502
        except Exception:
            return failed()

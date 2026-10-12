"""Day 28 routes around the preserved Day 27 Flask application."""

from pathlib import Path

from flask import jsonify, render_template, request, send_from_directory
from jinja2 import ChoiceLoader, FileSystemLoader

from bootstrap import TASK_ROOT, load_day27_app
from local_rag.service import LocalRag


def create_app(*, data_dir=None, local_transport=None, cloud_transport=None):
    data = Path(data_dir) if data_dir is not None else TASK_ROOT / 'data'
    legacy = load_day27_app()
    app = legacy.create_app(data_dir=data / 'legacy', local_transport=local_transport)
    app.json.ensure_ascii = False
    app.config['MAX_CONTENT_LENGTH'] = 32_000
    app.jinja_loader = ChoiceLoader([FileSystemLoader(TASK_ROOT / 'templates'), app.jinja_loader])
    rag = LocalRag(data, local_transport=local_transport, cloud_transport=cloud_transport)
    app.extensions['local_rag'] = rag

    original_home = app.view_functions['index']
    app.add_url_rule('/chat', endpoint='day27_chat', view_func=original_home)

    def home():
        return render_template('local_rag.html')

    app.view_functions['index'] = home

    @app.get('/day28-static/<path:filename>')
    def day28_static(filename):
        return send_from_directory(TASK_ROOT / 'static', filename)

    def invalid(code, message):
        return jsonify(status='rejected', error={'code': code, 'message': message},
                       ledger=rag.store.ledger()), 400

    def payload(fields):
        value = request.get_json(silent=True)
        if not isinstance(value, dict) or not set(value).issubset(fields):
            return None
        return value

    @app.get('/api/local-rag/state')
    def local_rag_state():
        return jsonify(rag.state())

    @app.post('/api/local-rag/build')
    def local_rag_build():
        value = payload(set())
        if value != {}:
            return invalid('invalid_payload', 'Для построения индекса передайте пустой JSON-объект.')
        result = rag.build_index()
        return jsonify(result), 200 if result['status'] == 'ok' else 503

    @app.post('/api/local-rag/ask')
    def local_rag_ask():
        value = payload({'prompt', 'mode'})
        if value is None or 'prompt' not in value or (type(value.get('mode', 'local')) is not str):
            return invalid('invalid_payload', 'Передайте prompt и необязательный mode.')
        result = rag.ask(value['prompt'], mode=value.get('mode', 'local'))
        status = 200 if result['status'] in ('ok', 'no_context') else 400 if result['status'] == 'rejected' else 503
        return jsonify(result), status

    return app


if __name__ == '__main__':
    create_app().run(host='127.0.0.1', port=5028, debug=False)

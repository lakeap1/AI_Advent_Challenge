"""Day 29 routes around the preserved local RAG application."""

import json
from pathlib import Path

from flask import jsonify, render_template, request, send_from_directory
from jinja2 import ChoiceLoader, FileSystemLoader

from bootstrap import TASK_ROOT, load_day28_app
from optimization.service import OptimizationService


def create_app(*, data_dir=None, local_transport=None):
    data = Path(data_dir) if data_dir is not None else TASK_ROOT / 'data'
    day28 = load_day28_app()
    app = day28.create_app(data_dir=data / 'legacy', local_transport=local_transport)
    app.json.ensure_ascii = False
    app.config['MAX_CONTENT_LENGTH'] = 32_000
    app.jinja_loader = ChoiceLoader([FileSystemLoader(TASK_ROOT / 'templates'), app.jinja_loader])

    service = OptimizationService(data, local_transport=local_transport)
    app.extensions['optimization'] = service

    old_home = app.view_functions['index']
    app.add_url_rule('/day28', endpoint='day28_home', view_func=old_home)

    def home():
        return render_template('optimization.html')

    app.view_functions['index'] = home

    @app.get('/day29-static/<path:filename>')
    def day29_static(filename):
        return send_from_directory(TASK_ROOT / 'static', filename)

    def invalid(message):
        return jsonify(status='rejected', error={'code': 'invalid_payload', 'message': message}), 400

    def payload(fields):
        value = request.get_json(silent=True)
        return value if isinstance(value, dict) and set(value).issubset(fields) else None

    @app.get('/api/optimization/state')
    def optimization_state():
        state = service.state()
        report_path = data / 'selected-report.json'
        if report_path.is_file():
            try:
                report = json.loads(report_path.read_text(encoding='utf-8'))
                state['benchmark'] = {'kind': 'saved', 'report': report} if isinstance(report, dict) else {
                    'kind': 'unavailable'}
            except (OSError, UnicodeError, json.JSONDecodeError):
                state['benchmark'] = {'kind': 'unavailable'}
        return jsonify(state)

    @app.post('/api/optimization/build')
    def optimization_build():
        if payload(set()) != {}:
            return invalid('Для построения индекса передайте пустой JSON-объект.')
        result = service.build_index()
        return jsonify(result), 200 if result['status'] == 'ok' else 503

    @app.post('/api/optimization/ask')
    def optimization_ask():
        value = payload({'prompt', 'preset'})
        if value is None or 'prompt' not in value or type(value.get('preset', 'optimized')) is not str:
            return invalid('Передайте prompt и необязательный строковый preset.')
        result = service.ask(value['prompt'], preset=value.get('preset', 'optimized'))
        status = 200 if result['status'] in ('ok', 'no_context') else 400 if result['status'] == 'rejected' else 503
        return jsonify(result), status

    return app


if __name__ == '__main__':
    create_app().run(host='127.0.0.1', port=5029, debug=False)

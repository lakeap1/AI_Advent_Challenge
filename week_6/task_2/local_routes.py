"""Flask routes for the independent local chat."""

from flask import jsonify, request

from local_agent import LocalChat


def register_local(app, data_dir, transport=None):
    service = LocalChat(data_dir, transport=transport)
    app.extensions['local_chat'] = service

    @app.get('/api/local/state')
    def local_state():
        return jsonify(service.state())

    @app.get('/api/local/runtime')
    def local_runtime():
        return jsonify(service.runtime_status())

    @app.post('/api/local/ask')
    def local_ask():
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or set(body) != {'prompt'} or not isinstance(body['prompt'], str):
            return jsonify(status='rejected', answer=None, receipt=None,
                           error='Передайте JSON-объект с текстовым полем prompt.',
                           state=service.state()), 400
        result = service.ask(body['prompt'])
        code = {'ok': 200, 'rejected': 400, 'blocked': 409, 'error': 503}[result['status']]
        return jsonify(result), code

"""Dedicated private Flask app; no day-27 public/cloud routes are imported."""

from hmac import compare_digest
import os
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from .bootstrap import build_chat
from .private_service.gateway import PrivateGateway
from .private_service.transport import LocalOllamaTransport


def _token(value):
    if value is None:
        path = os.environ.get('DAY30_TOKEN_FILE')
        if not path:
            raise ValueError('DAY30_TOKEN_FILE обязателен для запуска приватного API.')
        value = Path(path).read_text(encoding='ascii').strip()
    if (not isinstance(value, str) or len(value) < 32 or
            any(ord(char) < 33 or ord(char) > 126 for char in value)):
        raise ValueError('Bearer ключ должен содержать минимум 32 печатных ASCII символа.')
    return value


def create_app(*, data_dir=None, token=None, config_path=None, local_transport=None, clock=None):
    secret = _token(token)
    root = Path(__file__).resolve().parent
    chosen_data = data_dir or os.environ.get('DAY30_DATA_DIR') or root / 'data'
    chosen_config = config_path or root / 'config.toml'
    transport = local_transport if local_transport is not None else LocalOllamaTransport()
    gateway = PrivateGateway(None, transport, clock=clock)
    chat = build_chat(chosen_data, chosen_config, gateway.counted_transport)
    gateway.chat = chat
    app = Flask(__name__, template_folder='templates', static_folder='static')
    app.config['MAX_CONTENT_LENGTH'] = 32000
    app.extensions['private_service'] = {'gateway': gateway, 'chat': chat}
    secret_bytes = secret.encode('ascii')

    def authenticated():
        header = request.headers.get('Authorization', '')
        candidate = header[7:] if header.startswith('Bearer ') else ''
        return compare_digest(candidate.encode('utf-8'), secret_bytes)

    def unauthorized():
        return jsonify(status='error', code='unauthorized', error='Неверный ключ доступа.'), 401

    @app.errorhandler(413)
    def too_large(_error):
        return jsonify(status='rejected', code='request_too_large',
                       error='Тело запроса превышает лимит 32 000 байт.'), 413

    @app.get('/')
    def index():
        return render_template('private_chat.html')

    @app.get('/api/private/state')
    def state():
        if not authenticated():
            return unauthorized()
        return jsonify(gateway.state())

    @app.get('/api/private/runtime')
    def runtime():
        if not authenticated():
            return unauthorized()
        return jsonify(gateway.runtime())

    @app.post('/api/private/ask')
    def ask():
        if not authenticated():
            return unauthorized()
        body = request.get_json(silent=True)
        valid = isinstance(body, dict) and set(body) == {'prompt'} and isinstance(body['prompt'], str)
        result, status, headers = gateway.ask(body['prompt'] if valid else None)
        if not valid and status == 400:
            result['code'] = 'invalid_request'
            result['error'] = 'Ожидается JSON с единственным строковым полем prompt.'
        return jsonify(result), status, headers

    return app

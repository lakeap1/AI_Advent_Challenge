"""Локальный HTTP-адаптер агента с восстановлением диалога."""
from dataclasses import asdict
from pathlib import Path
import os
import json
from uuid import uuid4

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

from agent import create_agent
from agent.storage import StorageError


def create_app(agent=None):
    load_dotenv(Path(__file__).with_name(".env"))
    if agent is None:
        data_dir = Path(os.environ.get('CHAT_DATA_DIR', Path(__file__).parent / 'data'))
        workers = {mode: create_agent(db_path=data_dir / f'{mode}.sqlite3', mode=mode) for mode in ('full', 'compressed')}
    elif isinstance(agent, dict):
        workers = agent
    else:
        workers = {'full': agent, 'compressed': agent}

    def select(mode):
        if not isinstance(mode, str) or mode not in workers:
            return None
        return workers[mode]
    app = Flask(__name__)
    app.json.ensure_ascii = False
    app.config['MAX_CONTENT_LENGTH'] = 100_000_000
    boot_id = uuid4().hex[:12]

    def snapshot(worker):
        return {**worker.state(), "boot_id": boot_id}

    def invalid_mode():
        return jsonify(status='rejected', code='invalid_mode', text='Выберите full или compressed.', usage_status='not_requested'), 400

    @app.after_request
    def no_cache(response):
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(StorageError)
    def storage_error(error):
        return jsonify(
            status="error", code="storage_error",
            text="Не удалось прочитать или сохранить историю. Проверьте доступ к базе данных. Запрос автоматически не повторяется.",
            usage=None, cost_usd=None, usage_status="unavailable",
        ), 503

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/state")
    def state():
        worker = select(request.args.get('mode', 'compressed'))
        return jsonify(snapshot(worker)) if worker is not None else invalid_mode()

    @app.post("/api/ask")
    def ask():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify(status="rejected", code="invalid_request",
                           text="Отправьте JSON-объект с полем prompt.",
                           usage=None, cost_usd=None, usage_status="not_requested"), 400
        worker = select(payload.get('mode', 'compressed'))
        if worker is None: return invalid_mode()
        result = worker.run(payload.get("prompt"))
        status_code = 200 if result.status == "ok" else 400 if result.status == "rejected" else {
            "not_configured": 503, "authentication": 502,
            "rate_limit": 429, "timeout": 504,
            "insufficient_quota": 429, "context_length_exceeded": 400,
        }.get(result.code, 502)
        return jsonify(**asdict(result), state=snapshot(worker)), status_code

    @app.post('/api/preview')
    def preview():
        payload = request.get_json(silent=True)
        worker = select(payload.get('mode', 'compressed') if isinstance(payload, dict) else 'compressed')
        if worker is None: return invalid_mode()
        result = worker.preview(payload.get('prompt') if isinstance(payload, dict) else None)
        return jsonify(result), 200 if result['status'] == 'ok' else 400

    @app.get('/api/comparison')
    def comparison():
        path = Path(__file__).parent / 'results' / 'reference.json'
        if not path.exists():
            return jsonify(status='unavailable', text='Сравнение ещё не выполнено.'), 404
        return jsonify(json.loads(path.read_text(encoding='utf-8')))

    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=5009, debug=False)

"""Локальный HTTP-адаптер агента с восстановлением диалога."""
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

from agent import create_agent
from agent.storage import StorageError


def create_app(agent=None):
    load_dotenv(Path(__file__).with_name(".env"))
    worker = create_agent() if agent is None else agent
    app = Flask(__name__)
    app.json.ensure_ascii = False
    boot_id = uuid4().hex[:12]

    def snapshot():
        return {**worker.state(), "boot_id": boot_id}

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
        return jsonify(snapshot())

    @app.post("/api/ask")
    def ask():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify(status="rejected", code="invalid_request",
                           text="Отправьте JSON-объект с полем prompt.",
                           usage=None, cost_usd=None, usage_status="not_requested"), 400
        result = worker.run(payload.get("prompt"))
        status_code = 200 if result.status == "ok" else 400 if result.status == "rejected" else {
            "not_configured": 503, "authentication": 502,
            "rate_limit": 429, "timeout": 504,
        }.get(result.code, 502)
        return jsonify(**asdict(result), state=snapshot()), status_code

    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=5007, debug=False)

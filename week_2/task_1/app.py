"""Локальный веб-адаптер публичного контракта агента."""

from dataclasses import asdict
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

from agent import create_agent


def create_app(agent=None):
    load_dotenv(Path(__file__).with_name(".env"))
    worker = create_agent() if agent is None else agent
    app = Flask(__name__)
    app.json.ensure_ascii = False

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.post("/api/ask")
    def ask():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify(status="rejected", code="invalid_request", text="Отправьте JSON-объект с полем prompt."), 400
        result = worker.run(payload.get("prompt"))
        if result.status == "ok":
            status_code = 200
        elif result.status == "rejected":
            status_code = 400
        else:
            status_code = {"not_configured": 503, "authentication": 502, "rate_limit": 429, "timeout": 504}.get(result.code, 502)
        return jsonify(asdict(result)), status_code

    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=5006, debug=False)

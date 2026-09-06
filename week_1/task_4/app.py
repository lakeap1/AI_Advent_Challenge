import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from time import perf_counter
from uuid import uuid4

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from openai import APIError, AuthenticationError, BadRequestError, OpenAI, RateLimitError

ROOT = Path(__file__).resolve().parent
MODEL = "gpt-5.6-luna"
TEMPERATURES = (0, 0.7, 1.2)
INSTRUCTIONS = (
    "Отвечай по-русски обычным текстом с абзацами, без Markdown: без заголовков с #, "
    "выделения звёздочками, таблиц, обратных кавычек и ограждений блоков кода. "
    "Не оборачивай весь ответ в блок кода. Сохраняй символы, нужные для кода и формул. "
    "Выполняй запрос пользователя."
)
EXAMPLE_PROMPT = (
    "Придумай необычный десерт для маленького андроида, который впервые угощает человека. "
    "Выбери до 5 обычных съедобных ингредиентов и укажи их точные количества на одну порцию. "
    "Дай десерту фантастическое название. Опиши 3 шага без нагрева на языке, понятном роботу: "
    "используй образы из его мира, но сохрани выполнимость рецепта. До 120 слов."
)


def create_app(openai_client=None, results_dir=None):
    load_dotenv(ROOT / ".env")
    app = Flask(__name__)
    folder = Path(results_dir) if results_dir is not None else ROOT / "docs" / "runs"
    lock = Lock()

    @app.get("/")
    def index():
        return render_template("index.html", model=MODEL, prompt=EXAMPLE_PROMPT)

    @app.post("/api/compare")
    def compare():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify(error="Ожидается JSON-объект запроса."), 400
        prompt = payload.get("prompt")
        if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 4000:
            return jsonify(error="Введите запрос от 1 до 4000 символов."), 400
        if not lock.acquire(blocking=False):
            return jsonify(error="Сравнение уже выполняется. Дождитесь завершения."), 409
        owned = None
        try:
            client = openai_client
            if client is None:
                if not os.getenv("OPENAI_API_KEY"):
                    return jsonify(error="Добавьте OPENAI_API_KEY в локальный .env."), 503
                owned = client = OpenAI(timeout=60, max_retries=0)
            common = dict(model=MODEL, input=prompt.strip(), instructions=INSTRUCTIONS,
                          reasoning={"effort": "none"}, max_output_tokens=1200, store=False)

            def generate(temperature):
                start = perf_counter()
                item = {"temperature": temperature, "status": "error"}
                try:
                    reply = client.responses.create(**common, temperature=temperature)
                    item.update(answer=reply.output_text, model=reply.model, response_id=reply.id,
                                status=reply.status,
                                usage=({key: getattr(reply.usage, key) for key in
                                        ("input_tokens", "output_tokens", "total_tokens")}
                                       if reply.usage else None))
                    if reply.status != "completed":
                        item["error"] = "Ответ не завершён; не считайте его полным примером."
                    elif not reply.output_text.strip():
                        item.update(status="error", error="Модель вернула пустой ответ.")
                except AuthenticationError:
                    item["error"] = "API-ключ отклонён провайдером."
                except RateLimitError:
                    item["error"] = "Лимит API исчерпан. Повторите позже."
                except BadRequestError:
                    item["error"] = "API отклонил параметры запроса. Температура не подменялась."
                except APIError:
                    item["error"] = "Не удалось получить ответ OpenAI API."
                item["seconds"] = round(perf_counter() - start, 2)
                return item

            with ThreadPoolExecutor(max_workers=3) as pool:
                results = list(pool.map(generate, TEMPERATURES))
            run = {"id": uuid4().hex, "created_at": datetime.now(timezone.utc).isoformat(),
                   "request": common, "results": results}
            try:
                folder.mkdir(parents=True, exist_ok=True)
                (folder / f"{run['id']}.json").write_text(
                    json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
            except OSError:
                run["save_warning"] = "Ответы получены, но не удалось сохранить запуск на диске."
            return jsonify(run)
        finally:
            if owned is not None:
                owned.close()
            lock.release()

    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=5004, debug=False)

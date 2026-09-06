import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from time import perf_counter
from uuid import uuid4

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from openai import APIError, AuthenticationError, OpenAI, RateLimitError

from bridge import check_route, optimum

ROOT = Path(__file__).resolve().parent
MODEL = "gpt-5.6-luna"
PLAIN_TEXT = ("Отвечай по-русски обычным текстом, без Markdown: без звёздочек, "
              "заголовков с #, таблиц, обратных кавычек и LaTeX-разметки. "
              "Используй абзацы и нумерованные строки. Формулы записывай обычными символами.")
TASK = """Пять человек должны ночью перейти мост.
Каждому для перехода требуется своё время: 1, 3, 6, 8 и 12 минут.
На мосту одновременно могут находиться не более двух человек.
Для любого перехода необходим единственный фонарь.
Если идут двое, они движутся со скоростью более медленного.
Фонарь нужно переносить, перебрасывать его нельзя.
Изначально все пять человек и фонарь находятся на одном берегу.
Какое минимальное время потребуется всем для переправы?
Укажите последовательность переходов и возвратов."""
MODES = {"direct": "Прямой ответ", "step": "Решай пошагово", "meta": "Промпт от модели", "experts": "Группа экспертов"}
EXPERTS = """Реши задачу как группа из трёх экспертов. Представь отдельное решение от каждого.
Математик: формализуй ограничения, сравни стратегии и обоснуй минимальность, а не только допустимость.
Планировщик: составь исполнимое расписание; отслеживай людей на обоих берегах и местоположение фонаря, считай время каждого хода.
Скептик: самостоятельно предложи решение, затем проверь арифметику, возвраты и скрытые допущения; попытайся найти более короткий вариант и честно укажи пробелы доказательства.
Для каждого эксперта укажи заголовок с ролью, полную последовательность переходов и возвратов, итоговое время и краткое обоснование своего подхода. Не заменяй отдельные решения общим консенсусом."""


def create_app(openai_client=None, results_dir=None):
    load_dotenv(ROOT / ".env")
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 64000
    folder = Path(results_dir) if results_dir is not None else ROOT / "results"
    lock = Lock()

    def save(result, failed=False):
        destination = folder / "failed" if failed else folder
        destination.mkdir(parents=True, exist_ok=True)
        path = destination / (result["id"] + ".json")
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    @app.get("/")
    def index():
        return render_template("index.html", task=TASK, model=MODEL)

    @app.get("/api/reference")
    def reference():
        return jsonify(optimum())

    @app.get("/api/latest")
    def latest():
        found = {}
        with lock:
            for path in sorted(folder.glob("*.json"), reverse=True):
                result = json.loads(path.read_text(encoding="utf-8"))
                found.setdefault(result["mode"], result)
        return jsonify(found)

    @app.post("/api/run/<mode>")
    def run(mode):
        if mode not in MODES:
            return jsonify(error="Неизвестный способ."), 404
        if not lock.acquire(blocking=False):
            return jsonify(error="Запрос уже выполняется. Дождитесь завершения."), 409
        calls = []
        started = perf_counter()

        def failure(message, status):
            if calls:
                stamp = datetime.now(timezone.utc)
                save({"id": stamp.strftime("%Y%m%dT%H%M%S%f") + "-" + uuid4().hex[:8],
                      "created_at": stamp.isoformat(), "mode": mode, "model": MODEL,
                      "status": "failed", "error": message, "calls": calls,
                      "seconds": round(perf_counter() - started, 3),
                      "tokens": sum(c["usage"]["total_tokens"] for c in calls) if all(c["usage"] for c in calls) else None}, failed=True)
            return jsonify(error=message), status

        try:
            client = openai_client
            if client is None:
                if not os.getenv("OPENAI_API_KEY"):
                    return jsonify(error="Добавьте OPENAI_API_KEY в локальный .env задания или окружение сервера."), 503
                client = OpenAI(timeout=300, max_retries=0)
            def ask(prompt, instructions=None):
                params = {"model": MODEL, "reasoning": {"effort": "high"}, "input": prompt, "store": False}
                params["instructions"] = PLAIN_TEXT + ("\n\n" + instructions if instructions else "")
                started = perf_counter()
                response = client.responses.create(**params)
                if response.status != "completed" or not response.output_text.strip():
                    raise ValueError("Модель вернула незавершённый или пустой ответ.")
                calls.append({"request": params, "answer": response.output_text,
                              "response_id": response.id, "response_model": response.model,
                              "seconds": round(perf_counter() - started, 3), "usage": response.usage.model_dump() if response.usage else None})
                return response.output_text

            started = perf_counter()
            if mode == "direct":
                answer = ask(TASK)
            elif mode == "step":
                answer = ask(TASK + "\n\nРешай пошагово.")
            elif mode == "meta":
                generated = ask("Составь промпт для решения приведённой задачи. Не решай задачу и не включай предполагаемый ответ. Верни только готовый промпт.\n\n" + TASK)
                answer = ask(generated + "\n\nИсходное условие (сохрани все ограничения):\n" + TASK)
            else:
                answer = ask(TASK, EXPERTS)
            stamp = datetime.now(timezone.utc)
            result = {"id": stamp.strftime("%Y%m%dT%H%M%S%f") + "-" + uuid4().hex[:8],
                      "created_at": stamp.isoformat(), "mode": mode, "model": MODEL,
                      "answer": answer, "calls": calls, "seconds": round(perf_counter() - started, 3),
                      "tokens": sum(c["usage"]["total_tokens"] for c in calls) if all(c["usage"] for c in calls) else None,
                      "reviews": []}
            save(result)
            return jsonify(result)
        except AuthenticationError:
            return failure("API-ключ отклонён провайдером.", 401)
        except RateLimitError:
            return failure("Лимит API исчерпан. Повторите позже.", 429)
        except APIError:
            return failure("Ошибка подключения или ответа OpenAI API. Результат не засчитан.", 502)
        except ValueError as error:
            return failure(str(error), 502)
        finally:
            lock.release()

    @app.post("/api/check/<result_id>")
    def check(result_id):
        if not re.fullmatch(r"\d{8}T\d{12}-[a-f0-9]{8}", result_id):
            return jsonify(error="Неизвестный результат."), 404
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or not isinstance(payload.get("reviews"), list):
            return jsonify(error="Нужен список проверок."), 400
        reviews = payload["reviews"]
        if not 1 <= len(reviews) <= 3:
            return jsonify(error="Нужно от одной до трёх проверок."), 400
        clean = []
        for review in reviews:
            if (not isinstance(review, dict) or not isinstance(review.get("moves"), list)
                    or type(review.get("claimed")) is not int
                    or not isinstance(review.get("label"), str) or len(review["label"]) > 100
                    or not isinstance(review.get("note", ""), str) or len(review.get("note", "")) > 4000):
                return jsonify(error="Проверьте маршрут, роль и целое число минут."), 400
            clean.append({"label": review["label"], "moves": review["moves"], "claimed": review["claimed"],
                          "note": review.get("note", ""), "check": check_route(review["moves"], review["claimed"])})
        with lock:
            path = folder / (result_id + ".json")
            if not path.exists():
                return jsonify(error="Результат не найден."), 404
            result = json.loads(path.read_text(encoding="utf-8"))
            expected = ["Математик", "Планировщик", "Скептик"] if result["mode"] == "experts" else ["Ответ"]
            if [r["label"] for r in clean] != expected:
                return jsonify(error="Проверьте все решения: " + ", ".join(expected)), 400
            result["reviews"] = clean
            save(result)
        return jsonify(result)

    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=5003, debug=False)

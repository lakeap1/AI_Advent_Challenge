"""One prompt, three model tiers, measured sequentially through Responses API."""
import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from statistics import median
from threading import Lock
from time import perf_counter
from uuid import uuid4

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request, stream_with_context
from openai import APIError, AuthenticationError, BadRequestError, OpenAI, RateLimitError

ROOT = Path(__file__).resolve().parent
MODELS = (
    {"id": "gpt-5.6-luna", "name": "Luna", "tier": "Экономичная", "input": "0.20", "cached": "0.02", "output": "1.20"},
    {"id": "gpt-5.6-terra", "name": "Terra", "tier": "Сбалансированная", "input": "2.00", "cached": "0.20", "output": "12.00"},
    {"id": "gpt-5.6-sol", "name": "Sol", "tier": "Сильная", "input": "4.00", "cached": "0.40", "output": "20.00"},
)
PRICING = {"checked_at": "2026-09-07", "currency": "USD", "service_tier": "default",
           "source": "https://developers.openai.com/api/docs/pricing", "models": MODELS}
INSTRUCTIONS = (
    "Отвечай по-русски обычным текстом с абзацами, без Markdown: без заголовков с #, "
    "выделения звёздочками, Markdown-таблиц, обратных кавычек и ограждений блоков кода. "
    "Не оборачивай весь ответ в блок кода. Сохраняй символы, нужные для кода и формул. "
    "Если пользователь явно требует другой формат, используй его. Выполняй запрос пользователя."
)
EXAMPLE_PROMPT = """Выбери функции для релиза с максимальной суммарной ценностью. Бюджет — 14 часов. Каждую функцию можно взять один раз, частично брать нельзя. Затраты и ценности складываются; зависимости тоже входят в бюджет.

A — авторизация: 3 ч, ценность 5.
B — поиск: 4 ч, ценность 8; требует A.
C — фильтры: 2 ч, ценность 4; требует B.
D — экспорт: 3 ч, ценность 7; требует A.
E — отчёты: 5 ч, ценность 10; требует A.
F — уведомления: 2 ч, ценность 4; требует A.
G — офлайн-режим: 4 ч, ценность 9; несовместим с H.
H — интеграция: 3 ч, ценность 8; требует D.

Укажи выбранные буквы, суммарные часы и ценность. Проверь зависимости и несовместимость. Кратко обоснуй, почему более ценного допустимого набора нет. До 180 слов."""


def cost_usd(usage, model):
    """Reasoning is already included in output_tokens; never add it twice."""
    if usage is None:
        return None
    incoming, outgoing = usage.get("input_tokens"), usage.get("output_tokens")
    details = usage.get("input_tokens_details") or {}
    cached = details.get("cached_tokens")
    if any(type(v) is not int or v < 0 for v in (incoming, outgoing, cached)) or cached > incoming:
        return None
    # Explicit cache mode without breakpoints disables reads and writes in our requests.
    # Preserve unexpected cache writes, but do not report a misleading known price.
    if any(value for key, value in details.items() if "writ" in key or "creat" in key):
        return None
    rates = {key: Decimal(model[key]) for key in ("input", "cached", "output")}
    amount = ((incoming - cached) * rates["input"] + cached * rates["cached"]
              + outgoing * rates["output"]) / Decimal(1_000_000)
    return float(amount)


def summarize(results):
    summary = []
    for model in MODELS:
        rows = [r for r in results if r["requested_model"] == model["id"]]
        good = [r for r in rows if r["status"] == "completed"]
        tokens = [r["usage"]["total_tokens"] for r in good if r.get("usage")
                  and isinstance(r["usage"].get("total_tokens"), int)]
        costs = [r["cost_usd"] for r in rows if r.get("cost_usd") is not None]
        summary.append({"model": model["id"], "completed": len(good), "attempts": len(rows),
                        "median_seconds": round(median(r["seconds"] for r in good), 3) if good else None,
                        "median_tokens": median(tokens) if len(tokens) == len(good) and good else None,
                        "total_cost_usd": round(sum(costs), 8) if len(costs) == len(rows) and rows else None})
    return summary


def generate(client, model, common, attempt):
    result = {"requested_model": model["id"], "attempt": attempt, "status": "error",
              "answer": "", "usage": None, "cost_usd": None}
    start = perf_counter()
    try:
        reply = client.responses.create(model=model["id"], **common)
        elapsed = perf_counter() - start
        usage = reply.usage.model_dump() if reply.usage is not None else None
        result.update(answer=reply.output_text, status=reply.status, model=reply.model,
                      response_id=reply.id, usage=usage, service_tier=reply.service_tier,
                      incomplete_details=reply.incomplete_details.model_dump() if reply.incomplete_details else None)
        if reply.status != "completed":
            result["error"] = "Ответ не завершён. Он исключён из медиан."
        elif not reply.output_text.strip():
            result.update(status="error", error="Модель вернула пустой ответ.")
        if reply.service_tier == "default" and reply.model == model["id"]:
            result["cost_usd"] = cost_usd(usage, model)
        if result["cost_usd"] is None:
            result["cost_warning"] = "Стоимость неизвестна: проверьте usage, модель и тариф в журнале запуска."
    except AuthenticationError:
        result["error"] = "API-ключ отклонён провайдером."
        elapsed = perf_counter() - start
    except RateLimitError:
        result["error"] = "Достигнут лимит API. Повторите позже."
        elapsed = perf_counter() - start
    except BadRequestError:
        result["error"] = "API отклонил модель или параметры. Настройки не подменялись."
        elapsed = perf_counter() - start
    except APIError:
        result["error"] = "Не удалось получить ответ OpenAI API."
        elapsed = perf_counter() - start
    result["seconds"] = round(elapsed, 3)
    return result


def create_app(openai_client=None, results_dir=None):
    load_dotenv(ROOT / ".env")
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 32_000
    folder = Path(results_dir) if results_dir is not None else ROOT / "docs" / "runs"
    lock = Lock()

    @app.get("/")
    def index():
        return render_template("index.html", models=MODELS, prompt=EXAMPLE_PROMPT, pricing=PRICING)

    @app.get("/api/latest")
    def latest():
        try:
            paths = sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            for path in paths:
                try:
                    run = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(run, dict) and run.get("complete") and run.get("summary"):
                        return jsonify(run)
                except (OSError, ValueError):
                    continue
        except OSError:
            pass
        return jsonify(error="Завершённых сохранённых сравнений пока нет."), 404

    @app.post("/api/compare")
    def compare():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify(error="Ожидается JSON-объект."), 400
        prompt, repeats = payload.get("prompt"), payload.get("repeats", 3)
        if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 4000:
            return jsonify(error="Введите запрос от 1 до 4000 символов."), 400
        if type(repeats) is not int or repeats not in (1, 3):
            return jsonify(error="Допустимы 1 или 3 прогона."), 400
        if openai_client is None and not os.getenv("OPENAI_API_KEY"):
            return jsonify(error="Добавьте OPENAI_API_KEY в локальный .env."), 503
        if not lock.acquire(blocking=False):
            return jsonify(error="Сравнение уже выполняется. Дождитесь завершения."), 409
        common = {"input": prompt.strip(), "instructions": INSTRUCTIONS, "reasoning": {"effort": "medium"},
                  "max_output_tokens": 6000, "store": False, "service_tier": "default",
                  "extra_body": {"prompt_cache_options": {"mode": "explicit"}}}
        run = {"id": uuid4().hex, "created_at": datetime.now(timezone.utc).isoformat(),
               "request": common, "repeats": repeats, "pricing": PRICING, "results": [], "complete": False}

        def save():
            try:
                folder.mkdir(parents=True, exist_ok=True)
                target = folder / f"{run['id']}.json"
                temporary = target.with_suffix(".tmp")
                temporary.write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
                temporary.replace(target)
            except OSError:
                run["save_warning"] = "Не удалось сохранить запуск на диске."

        def event(kind, **data):
            return json.dumps({"type": kind, **data}, ensure_ascii=False) + "\n"

        @stream_with_context
        def stream():
            owned = None
            try:
                client = openai_client
                if client is None:
                    owned = client = OpenAI(timeout=90, max_retries=0)
                yield event("start", id=run["id"], total=repeats * 3)
                for attempt in range(repeats):
                    order = MODELS[attempt:] + MODELS[:attempt]
                    for model in order:
                        yield event("progress", model=model["id"], attempt=attempt + 1)
                        result = generate(client, model, common, attempt + 1)
                        run["results"].append(result)
                        save()
                        yield event("result", result=result)
                run["complete"] = True
                run["summary"] = summarize(run["results"])
                save()
                yield event("done", run=run)
            finally:
                save()
                if owned is not None:
                    owned.close()
                lock.release()

        return Response(stream(), mimetype="application/x-ndjson", headers={"Cache-Control": "no-store"})

    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=5005, debug=False)

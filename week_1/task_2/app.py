import json
import os
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from openai import APIConnectionError, APIError, AuthenticationError, OpenAI, RateLimitError

MODEL = "gpt-4.1-mini"
STOP_MARKER = "[КОНЕЦ]"


def build_request(payload):
    if not isinstance(payload, dict):
        raise ValueError("Ожидается JSON-объект запроса.")
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 4000:
        raise ValueError("Введите запрос длиной от 1 до 4000 символов.")
    for flag in ("json_format", "limit_enabled", "stop_enabled"):
        if type(payload.get(flag, False)) is not bool:
            raise ValueError("Переключатели должны иметь значения true или false.")
    instructions = []
    if payload.get("json_format", False):
        instructions.append(
            'Ответь JSON-объектом с единственным ключом "answer": массив ровно из 3 непустых строк. '
            'Каждая строка — один содержательный пункт ответа, без вложенных списков. '
            'Не используй Markdown или пояснения вне JSON.'
        )
    if payload.get("stop_enabled", False):
        instructions.append(
            "Когда ответишь на все части вопроса, сразу после готового ответа "
            "напиши на отдельной строке [КОНЕЦ], затем на следующей строке КОНТРОЛЬНОЕ ПРОДОЛЖЕНИЕ. "
            "Если ответ в JSON, сначала полностью закрой JSON-объект, затем напиши эти две строки "
            "вне JSON. Это исключение к запрету текста вне JSON."
        )
    if payload.get("limit_enabled", False):
        limit = payload.get("max_words", 60)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Лимит должен быть целым числом от 1 до 1000 слов.")
        instructions.append(
            f"Используй не более {limit} слов суммарно во всём ответе. "
            "В JSON считай только текст трёх строк answer, без ключей и синтаксиса. "
            "Слово — разделённая пробелами часть текста, содержащая букву или цифру; "
            "дефис внутри слова не разделяет его. Маркер завершения и контрольное продолжение не учитывай. "
            "Сократи формулировки, но заверши ответ и закрой JSON."
        )
    messages = []
    if instructions:
        messages.append({"role": "developer", "content": "\n".join(instructions)})
    messages.append({"role": "user", "content": prompt.strip()})
    args = {"model": MODEL, "messages": messages}
    if payload.get("stop_enabled", False):
        args["stop"] = [STOP_MARKER]
    return args


def create_app(openai_client=None):
    load_dotenv(Path(__file__).with_name(".env"))
    app = Flask(__name__)

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.post("/api/chat")
    def chat():
        payload = request.get_json(silent=True)
        try:
            args = build_request(payload)
        except ValueError as error:
            return jsonify(error=str(error)), 400
        client = openai_client
        if client is None:
            if not os.getenv("OPENAI_API_KEY"):
                return jsonify(error="Добавьте OPENAI_API_KEY в локальный .env."), 503
            client = OpenAI(timeout=60, max_retries=0)
        try:
            response = client.chat.completions.create(**args)
            if not response.choices:
                return jsonify(error="API не вернул вариант ответа."), 502
            choice = response.choices[0]
            if choice.message.refusal:
                return jsonify(error="Модель отказалась отвечать: " + choice.message.refusal), 422
            text = choice.message.content
            if not text or not text.strip():
                return jsonify(error="API вернул пустой текст. Проверьте лимит и запрос."), 502
        except AuthenticationError:
            return jsonify(error="API-ключ отклонён."), 401
        except RateLimitError:
            return jsonify(error="Лимит API исчерпан. Повторите позже."), 429
        except APIConnectionError:
            return jsonify(error="Не удалось подключиться к API."), 502
        except APIError:
            return jsonify(error="API отклонил запрос или вернул ошибку."), 502
        json_ok = None
        word_text = text
        if payload.get("json_format", False):
            try:
                parsed = json.loads(text)
                json_ok = (isinstance(parsed, dict) and set(parsed) == {"answer"}
                           and isinstance(parsed["answer"], list)
                           and len(parsed["answer"]) == 3
                           and all(isinstance(item, str) and item.strip() for item in parsed["answer"]))
                word_text = " ".join(parsed["answer"]) if json_ok else None
            except (ValueError, TypeError):
                json_ok = False
                word_text = None
        word_count = (sum(any(char.isalnum() for char in part) for part in word_text.split())
                      if word_text is not None else None)
        word_limit_ok = (word_count <= payload.get("max_words", 60)
                         if payload.get("limit_enabled", False) and word_count is not None else None)
        return jsonify(text=text, model=MODEL, request=args, json_ok=json_ok,
                       output_tokens=getattr(response.usage, "completion_tokens", None),
                       word_count=word_count, word_limit_ok=word_limit_ok, finish_reason=choice.finish_reason)

    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=5001, debug=False)

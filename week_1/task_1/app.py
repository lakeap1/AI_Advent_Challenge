import os

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from openai import APIConnectionError, APIError, AuthenticationError, OpenAI, RateLimitError


MODEL = "gpt-5.6-luna"
MAX_PROMPT_LENGTH = 4000


def create_app(openai_client=None):
    load_dotenv()
    app = Flask(__name__)

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.post("/api/generate")
    def generate():
        payload = request.get_json(silent=True) or {}
        prompt = payload.get("prompt")

        if not isinstance(prompt, str) or not prompt.strip():
            return jsonify(error="Введите текст запроса."), 400

        prompt = prompt.strip()
        if len(prompt) > MAX_PROMPT_LENGTH:
            return jsonify(error="Запрос не должен превышать 4000 символов."), 400

        client = openai_client
        if client is None:
            api_key = os.getenv("OPENAI_API_KEY")
            if not api_key:
                return (
                    jsonify(
                        error=(
                            "API-ключ не настроен. "
                            "Добавьте OPENAI_API_KEY в файл .env."
                        )
                    ),
                    503,
                )
            client = OpenAI(api_key=api_key)

        try:
            response = client.responses.create(
                model=MODEL,
                reasoning={"effort": "none"},
                instructions=(
                    "Отвечай обычным текстом без Markdown: без заголовков с #, "
                    "выделения звёздочками, обратных кавычек и ограждений блоков кода. "
                    "Используй абзацы. Сохраняй необходимые символы в коде и формулах."
                ),
                input=prompt,
            )
        except AuthenticationError:
            return jsonify(error="API-ключ отклонён провайдером."), 401
        except RateLimitError:
            return jsonify(error="Лимит API исчерпан. Повторите запрос позже."), 429
        except APIConnectionError:
            return jsonify(error="Не удалось подключиться к OpenAI API."), 502
        except APIError:
            return jsonify(error="OpenAI API временно недоступен."), 502

        answer = response.output_text.strip()
        if not answer:
            return jsonify(error="Модель вернула пустой ответ."), 502

        return jsonify(answer=answer)

    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=5000, debug=False)

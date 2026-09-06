from types import SimpleNamespace

import pytest

from app import create_app


class FakeResponses:
    def __init__(self, answer="Ответ Luna"):
        self.answer = answer
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(output_text=self.answer)


class FakeOpenAI:
    def __init__(self, answer="Ответ Luna"):
        self.responses = FakeResponses(answer)


@pytest.fixture
def fake_openai():
    return FakeOpenAI()


@pytest.fixture
def client(fake_openai):
    app = create_app(openai_client=fake_openai)
    app.config.update(TESTING=True)
    return app.test_client()


def test_generate_calls_luna_with_cheapest_reasoning(client, fake_openai):
    response = client.post("/api/generate", json={"prompt": "Скажи привет"})

    assert response.status_code == 200
    assert response.get_json() == {"answer": "Ответ Luna"}
    assert fake_openai.responses.calls == [
        {
            "model": "gpt-5.6-luna",
            "reasoning": {"effort": "none"},
            "instructions": (
                "Отвечай обычным текстом без Markdown: без заголовков с #, "
                "выделения звёздочками, обратных кавычек и ограждений блоков кода. "
                "Используй абзацы. Сохраняй необходимые символы в коде и формулах."
            ),
            "input": "Скажи привет",
        }
    ]


@pytest.mark.parametrize(
    "payload", [None, {}, {"prompt": ""}, {"prompt": "   "}, {"prompt": 7}]
)
def test_generate_rejects_empty_or_invalid_prompt(client, fake_openai, payload):
    response = client.post("/api/generate", json=payload)

    assert response.status_code == 400
    assert response.get_json() == {"error": "Введите текст запроса."}
    assert fake_openai.responses.calls == []


def test_generate_rejects_prompt_longer_than_4000(client, fake_openai):
    response = client.post("/api/generate", json={"prompt": "а" * 4001})

    assert response.status_code == 400
    assert response.get_json() == {
        "error": "Запрос не должен превышать 4000 символов."
    }
    assert fake_openai.responses.calls == []


def test_generate_reports_missing_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr("app.load_dotenv", lambda: None)
    app = create_app()
    app.config.update(TESTING=True)

    response = app.test_client().post("/api/generate", json={"prompt": "Привет"})

    assert response.status_code == 503
    assert response.get_json() == {
        "error": "API-ключ не настроен. Добавьте OPENAI_API_KEY в файл .env."
    }


def test_index_contains_prompt_interface(client):
    response = client.get("/")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'id="prompt-form"' in html
    assert 'id="prompt"' in html
    assert 'id="status"' in html
    assert 'id="answer"' in html

from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from openai import BadRequestError

from app import create_app


def response(text="Ответ", status="completed"):
    return SimpleNamespace(output_text=text, status=status, model="gpt-5.6-luna",
                           id="resp_test", usage=SimpleNamespace(input_tokens=20, output_tokens=10, total_tokens=30))


def client_for(tmp_path, side_effect=None):
    api = Mock()
    api.responses.create.side_effect = side_effect
    api.responses.create.return_value = response()
    app = create_app(api, results_dir=tmp_path)
    app.config["TESTING"] = True
    return app.test_client(), api


def test_same_prompt_and_settings_except_temperature(tmp_path):
    client, api = client_for(tmp_path)
    result = client.post("/api/compare", json={"prompt": "Один запрос"})
    assert result.status_code == 200
    data = result.get_json()
    assert [r["temperature"] for r in data["results"]] == [0, 0.7, 1.2]
    calls = [c.kwargs.copy() for c in api.responses.create.call_args_list]
    assert sorted(c.pop("temperature") for c in calls) == [0, 0.7, 1.2]
    assert calls[0] == calls[1] == calls[2]
    assert calls[0]["input"] == "Один запрос"
    assert "без Markdown" in calls[0]["instructions"]
    assert "top_p" not in calls[0]
    assert len(list(tmp_path.glob("*.json"))) == 1


@pytest.mark.parametrize("payload", [None, [], "text", {}, {"prompt": 1}, {"prompt": " "}, {"prompt": "x" * 4001}])
def test_invalid_input_does_not_spend_tokens(tmp_path, payload):
    client, api = client_for(tmp_path)
    assert client.post("/api/compare", json=payload).status_code == 400
    api.responses.create.assert_not_called()


def test_one_failure_preserves_other_answers(tmp_path):
    def generate(**args):
        if args["temperature"] == 0.7:
            raise BadRequestError("unsupported", response=httpx.Response(400, request=httpx.Request("POST", "https://api.openai.com/v1/responses")), body=None)
        return response()
    client, api = client_for(tmp_path, generate)
    data = client.post("/api/compare", json={"prompt": "test"}).get_json()
    assert data["results"][0]["answer"] == "Ответ"
    assert data["results"][1]["status"] == "error"
    assert data["results"][2]["answer"] == "Ответ"
    assert api.responses.create.call_count == 3


@pytest.mark.parametrize("reply", [response(""), response("Часть", "incomplete")])
def test_empty_or_truncated_answers_are_not_success(tmp_path, reply):
    client, api = client_for(tmp_path)
    api.responses.create.return_value = reply
    results = client.post("/api/compare", json={"prompt": "test"}).get_json()["results"]
    assert all(r["status"] != "completed" for r in results)


def test_answer_is_not_rewritten(tmp_path):
    client, api = client_for(tmp_path)
    api.responses.create.return_value = response("  # Текст <script>alert(1)</script>\n")
    data = client.post("/api/compare", json={"prompt": "test"}).get_json()
    assert data["results"][0]["answer"] == "  # Текст <script>alert(1)</script>\n"

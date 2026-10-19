"""Loopback-only browser fixture. Real HTTP/SQLite/service, zero external model calls.

Run from task_1: .venv/Scripts/python.exe -B tests/web_fixture.py
Credentials are disposable fixture constants, never production credentials.
"""
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from graphics.config import Settings
from graphics.provider import GLMProvider
from graphics.service import ChatService
from graphics.store import Store
from graphics.web import create_app
from tests.test_service import FakeMCP, FakeRetriever


def transport(payload):
    stage = json.loads(payload["messages"][-1]["content"])["stage"]
    value = {
        "preparation": {"query": "шейдер", "working_state": {"topic": "shader"},
                        "needs_clarification": False, "clarification": "", "answer_kind": "explanation"},
        "filter": {"keep": ["S1"], "reason": "fixture"},
        "generator": {"answer": "Шейдер определяет расчёт вида поверхности, а материал задаёт его параметры.",
                      "source_ids": ["S1"]},
        "validator": {"accepted": True, "reason": "fixture", "used_source_ids": ["S1"]},
        "summary": {"summary": "Обсуждали материал и шейдер."},
    }[stage]
    return {"model": "glm-5.3-flash", "choices": [{"message": {"content": json.dumps(value)}}],
            "usage": {"prompt_tokens": 80, "completion_tokens": 20, "total_tokens": 100,
                      "prompt_tokens_details": {"cached_tokens": 0}}}


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="grani-browser-") as temporary:
        root = Path(__file__).resolve().parents[1]
        settings = Settings(root, Path(temporary), api_key="offline-fixture", secret_key="offline-session",
                            secure_cookies=False)
        store = Store(settings.db_path)
        store.create_user("browser", "browser-fixture-password")
        second = store.create_user("browser-b", "browser-fixture-password-b")
        store.update_profile(second, {"role": "PROFILE_B"})
        store.remember(second, "private-B", "MEMORY_B")
        store.create_chat(second, "Разговор B")
        service = ChatService(settings, store, GLMProvider(settings, store, transport), FakeRetriever(), FakeMCP())
        create_app(settings, store, service).run(host="127.0.0.1", port=5049, threaded=True, use_reloader=False)

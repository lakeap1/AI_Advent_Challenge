import re
from pathlib import Path

from app import create_app


TASK_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = TASK_ROOT.parents[1]


def test_index_exposes_one_chat_and_three_controls():
    app = create_app(openai_client=object())
    app.config.update(TESTING=True)

    response = app.test_client().get("/")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    for fragment in (
        'id="chat-form"',
        'id="prompt"',
        'id="send-button"',
        'id="status"',
        'id="history"',
        'id="json-format"',
        'id="limit-enabled"',
        'id="stop-enabled"',
        "Без ограничений",
        "Stop sequence",
    ):
        assert fragment in html


def test_task_readme_documents_a_runnable_assignment():
    content = (TASK_ROOT / "README.md").read_text(encoding="utf-8")

    for heading in (
        "## Цель",
        "## Требования и допущения",
        "## Подход",
        "## Запуск",
        "## Проверка",
        "## Файлы проекта",
        "## Ограничения",
    ):
        assert heading in content

    assert "Условие ещё не получено" not in content
    assert "OPENAI_API_KEY" in content
    assert "gpt-4.1-mini" in content
    assert "max_words" in content
    assert not re.search(r"\]\((?:\./)?(?:demo|docs)/", content)


def test_root_readme_marks_second_assignment_as_implemented():
    content = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")

    assert "Задание 1.2 — управление форматом ответа" in content
    assert "](week_1/task_2/README.md)" in content
    assert (REPOSITORY_ROOT / "week_1/task_2/README.md").is_file()


def test_environment_template_contains_no_key():
    assert (TASK_ROOT / ".env.example").read_text(encoding="utf-8") == (
        "OPENAI_API_KEY=\n"
    )

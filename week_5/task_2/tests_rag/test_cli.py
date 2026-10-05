"""CLI configuration must work in a fresh shell with a task-local .env."""
import os

import pytest

from rag import cli


@pytest.mark.parametrize("command", ["ask", "compare", "evaluate"])
@pytest.mark.parametrize("existing_key", [None, "test-shell-key"])
def test_cli_loads_task_dotenv_before_service_without_overriding_shell(
    tmp_path, monkeypatch, capsys, command, existing_key
):
    task = tmp_path / "task"
    module = task / "rag" / "cli.py"
    module.parent.mkdir(parents=True)
    (task / ".env").write_text("OPENAI_API_KEY=test-task-dotenv-key\n", encoding="utf-8")
    monkeypatch.setattr(cli, "__file__", str(module))
    monkeypatch.chdir(tmp_path)  # The file belongs to the task, not the caller's cwd.
    # Register restoration even when the original shell had no key.
    monkeypatch.setenv("OPENAI_API_KEY", "temporary-test-key")
    monkeypatch.delenv("OPENAI_API_KEY")
    if existing_key is not None:
        monkeypatch.setenv("OPENAI_API_KEY", existing_key)
    seen = {}

    class Service:
        def __init__(self, root, directory):
            seen.update(key=os.environ.get("OPENAI_API_KEY"), root=root, directory=directory)

        def ask(self, question, mode, session):
            return {"status": "ok"}

        def compare(self, question, session):
            return {"status": "ok"}

        def close(self):
            seen["closed"] = True

    monkeypatch.setattr(cli, "RagService", Service)
    monkeypatch.setattr(cli, "evaluate", lambda service, output: {"status": "complete"})
    arguments = {
        "ask": ["ask", "test question", "--mode", "plain"],
        "compare": ["compare", "test question"],
        "evaluate": ["evaluate", "--output", str(tmp_path / "report.json")],
    }
    assert cli.main(arguments[command]) == 0
    assert seen["key"] == (existing_key or "test-task-dotenv-key")
    assert seen["root"] == task and seen["directory"] == task / "data"
    assert seen["closed"] is True
    assert "test-task-dotenv-key" not in capsys.readouterr().out

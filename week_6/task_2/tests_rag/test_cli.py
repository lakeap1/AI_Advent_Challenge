"""CLI configuration must work in a fresh shell with a task-local .env."""
import json
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


@pytest.mark.parametrize("status, expected_exit", [("ok", 0), ("no_context", 0), ("error", 1), ("rejected", 1)])
def test_cli_exit_distinguishes_normal_abstention_from_failure(tmp_path, monkeypatch, capsys, status, expected_exit):
    monkeypatch.setattr(cli, "__file__", str(tmp_path / "task" / "rag" / "cli.py"))
    closed = []

    class Service:
        def __init__(self, root, directory):
            pass

        def ask(self, question, mode, session):
            return {"status": status, "text": "bounded result"}

        def close(self):
            closed.append(True)

    monkeypatch.setattr(cli, "RagService", Service)
    actual_exit = cli.main(["ask", "question", "--mode", "filter"])
    assert json.loads(capsys.readouterr().out)["status"] == status
    assert closed == [True]
    assert actual_exit == expected_exit

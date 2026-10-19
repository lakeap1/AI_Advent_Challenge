"""Real stdio MCP contract and preflight refusal checks."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from graphics.mcp_client import MCPClient


class MCPTests(unittest.TestCase):
    def test_real_stdio_returns_branch_and_commit_from_ephemeral_checkout(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            subprocess.run(["git", "init", "-q", "-b", "mcp-fixture", str(repo)], check=True, capture_output=True)
            (repo / "README.md").write_text("fixture", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-qm", "fixture"], check=True, capture_output=True)
            result = MCPClient(repo).project_context()
            self.assertEqual(result["branch"], "mcp-fixture")
            self.assertEqual(len(result["commit"]), 40)
            self.assertEqual(result["tool"], "git_current_branch")
            self.assertEqual(result["protocol"], "mcp-stdio")

    def test_invalid_name_or_args_are_denied_before_subprocess(self):
        client = MCPClient(Path(__file__).resolve().parents[3])
        with patch.object(client, "_call_stdio", side_effect=AssertionError("spawned")):
            with self.assertRaises(ValueError):
                client.call("run_shell", {})
            with self.assertRaises(ValueError):
                client.call("git_current_branch", {"path": "."})

    def test_missing_checkout_fails_without_guessed_branch(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(Exception) as error:
                MCPClient(Path(temporary)).project_context()
            self.assertNotIn("main", str(error.exception).casefold())

    def test_detached_checkout_fails_without_guessed_branch(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            subprocess.run(["git", "init", "-q", "-b", "fixture", str(repo)], check=True, capture_output=True)
            (repo / "README.md").write_text("fixture", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-qm", "fixture"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "checkout", "-q", "--detach", "HEAD"], check=True, capture_output=True)
            with self.assertRaises(Exception):
                MCPClient(repo).project_context()

    def test_nested_directory_cannot_impersonate_parent_checkout(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            subprocess.run(["git", "init", "-q", "-b", "parent-branch", str(repo)], check=True, capture_output=True)
            (repo / "README.md").write_text("fixture", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-qm", "fixture"], check=True, capture_output=True)
            nested = repo / "wrong-configured-root"
            nested.mkdir()
            with self.assertRaises(Exception):
                MCPClient(nested).project_context()


if __name__ == "__main__":
    unittest.main()

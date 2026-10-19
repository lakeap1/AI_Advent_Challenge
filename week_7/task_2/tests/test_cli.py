import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from pr_review.cli import main


class CLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.env = os.environ.copy()
        self.env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                         "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
                         "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid"})
        self.git("init", "-q")
        self.git("config", "core.autocrlf", "false")
        self.write("week_7/task_1/corpus/glossary.md", "one\n")
        self.commit("base")
        self.base = self.git("rev-parse", "HEAD").stdout.strip()
        self.write("week_7/task_1/corpus/glossary.md", "one\ntwo\n")
        self.commit("head")

    def git(self, *args):
        result = subprocess.run(["git", *args], cwd=self.repo, env=self.env,
                                capture_output=True, text=True, check=True)
        return result

    def write(self, path, text):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-qm", message)

    def cli(self, *args):
        return subprocess.run([sys.executable, "-B", "-m", "pr_review", *map(str, args)],
                              cwd=Path(__file__).resolve().parents[1], env=self.env,
                              capture_output=True, text=True)

    def test_prepare_and_validate_empty_report_are_explicitly_unaccepted(self):
        out = self.root / "prepared.json"
        result = self.cli("prepare", "--repo", self.repo, "--base", self.base,
                          "--head", "HEAD", "--output", out)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "")
        prepared = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(prepared["status"], "prepared")
        self.assertEqual(prepared["review_status"], "integration_required")
        self.assertFalse(prepared["review_accepted"])
        self.assertEqual(prepared["llm_calls"], 0)
        self.assertIsNone(prepared["usage"])
        self.assertIsNone(prepared["cost_usd"])
        self.assertEqual(prepared["accounting_status"], "no_request")
        self.assertTrue(prepared["impact"]["rag_checks_required"])
        findings = self.root / "findings.json"
        findings.write_text(json.dumps({"schema_version": 1,
                                        "base_revision": prepared["base_revision"],
                                        "head_revision": prepared["head_revision"],
                                        "findings": []}), encoding="utf-8")
        validated = self.cli("validate", "--preparation", out, "--findings", findings)
        self.assertEqual(validated.returncode, 0, validated.stdout + validated.stderr)
        self.assertFalse(json.loads(validated.stdout)["review_accepted"])

    def test_prepare_invalid_ref_returns_json_error(self):
        result = self.cli("prepare", "--repo", self.repo, "--base", "--bad", "--head", "HEAD")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("error", json.loads(result.stdout))

    def test_same_head_related_context_validates_empty_report(self):
        prepared_run = self.cli("prepare", "--repo", self.repo, "--base", "HEAD",
                                "--head", "HEAD", "--related",
                                "week_7/task_1/corpus/glossary.md")
        self.assertEqual(prepared_run.returncode, 0, prepared_run.stdout + prepared_run.stderr)
        prepared = json.loads(prepared_run.stdout)
        ids = [source["source_id"] for source in prepared["sources"]]
        self.assertEqual(len(ids), 1)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(prepared["changed_files"], [])
        self.assertFalse(prepared["review_accepted"])
        prep_path = self.root / "same-head.json"
        prep_path.write_text(prepared_run.stdout, encoding="utf-8")
        report_path = self.root / "empty.json"
        report_path.write_text(json.dumps({"schema_version": 1,
                                           "base_revision": prepared["base_revision"],
                                           "head_revision": prepared["head_revision"],
                                           "findings": []}), encoding="utf-8")
        checked = self.cli("validate", "--preparation", prep_path, "--findings", report_path)
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
        self.assertTrue(json.loads(checked.stdout)["valid"])
        self.assertFalse(json.loads(checked.stdout)["review_accepted"])

    def test_output_refuses_overwrite(self):
        out = self.root / "user.txt"
        out.write_text("do not touch", encoding="utf-8")
        result = self.cli("prepare", "--repo", self.repo, "--base", self.base,
                          "--head", "HEAD", "--output", out)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(out.read_text(encoding="utf-8"), "do not touch")
        self.assertIn("error", json.loads(result.stdout))

    def test_invalid_report_and_oversized_input_fail_with_json(self):
        prep = self.root / "prep.json"
        result = self.cli("prepare", "--repo", self.repo, "--base", self.base,
                          "--head", "HEAD", "--output", prep)
        self.assertEqual(result.returncode, 0, result.stdout)
        findings = self.root / "findings.json"
        findings.write_text('{"findings":[]}', encoding="utf-8")
        bad = self.cli("validate", "--preparation", prep, "--findings", findings)
        self.assertNotEqual(bad.returncode, 0)
        self.assertFalse(json.loads(bad.stdout)["valid"])
        findings.write_bytes(b"x" * (8 * 1024 * 1024 + 1))
        huge = self.cli("validate", "--preparation", prep, "--findings", findings)
        self.assertNotEqual(huge.returncode, 0)
        self.assertIn("error", json.loads(huge.stdout))

    def test_russian_help_is_utf8_in_plain_windows_pipe(self):
        result = subprocess.run([sys.executable, "-B", "-m", "pr_review", "--help"],
                                cwd=Path(__file__).resolve().parents[1], env=self.env,
                                capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Подготовить Git-контекст", result.stdout.decode("utf-8"))

    def test_real_git_source_supports_positive_citation(self):
        prepared_path = self.root / "prepared.json"
        prepared_run = self.cli("prepare", "--repo", self.repo, "--base", self.base,
                                "--head", "HEAD", "--output", prepared_path)
        self.assertEqual(prepared_run.returncode, 0, prepared_run.stdout + prepared_run.stderr)
        prepared = json.loads(prepared_path.read_text(encoding="utf-8"))
        source = next(source for source in prepared["sources"] if source["side"] == "head")
        report = {"schema_version": 1, "base_revision": prepared["base_revision"],
                  "head_revision": prepared["head_revision"],
                  "snapshot_sha256": prepared["snapshot_sha256"],
                  "findings": [{"id": "F1", "severity": "low", "kind": "hypothesis",
                                "title": "Glossary item may need context", "problem": "Term has no definition",
                                "conditions": "When users search this term",
                                "consequence": "The answer may lack context",
                                "recommendation": "Check the glossary entry",
                                "location": {"revision": source["revision"], "path": source["path"],
                                             "start_line": 2, "end_line": 2},
                                "evidence": [{"source_id": source["source_id"], "quote": "two"}]}]}
        report_path = self.root / "findings.json"
        report_path.write_text(json.dumps(report), encoding="utf-8")
        validated = self.cli("validate", "--preparation", prepared_path,
                             "--findings", report_path)
        self.assertEqual(validated.returncode, 0, validated.stdout + validated.stderr)
        self.assertTrue(json.loads(validated.stdout)["valid"])
        self.assertFalse(json.loads(validated.stdout)["review_accepted"])

    def test_cyrillic_prepare_validates_and_oversized_json_creates_no_file(self):
        self.write("week_7/task_1/corpus/glossary.md", "Ж" * 900 + "\n")
        self.commit("cyrillic")
        prepared_path = self.root / "cyrillic.json"
        prepared = self.cli("prepare", "--repo", self.repo, "--base", self.base,
                            "--head", "HEAD", "--output", prepared_path)
        self.assertEqual(prepared.returncode, 0, prepared.stdout + prepared.stderr)
        payload = json.loads(prepared_path.read_text(encoding="utf-8"))
        self.assertLessEqual(prepared_path.stat().st_size, 8 * 1024 * 1024)
        report_path = self.root / "empty.json"
        report_path.write_text(json.dumps({"schema_version": 1,
                                           "base_revision": payload["base_revision"],
                                           "head_revision": payload["head_revision"],
                                           "findings": []}), encoding="utf-8")
        validation = self.cli("validate", "--preparation", prepared_path,
                              "--findings", report_path)
        self.assertEqual(validation.returncode, 0, validation.stdout + validation.stderr)
        blocked_path = self.root / "blocked.json"
        stdout = io.StringIO()
        with patch("pr_review.cli._MAX_INPUT_BYTES", 4096), contextlib.redirect_stdout(stdout):
            code = main(["prepare", "--repo", str(self.repo), "--base", self.base,
                         "--head", "HEAD", "--output", str(blocked_path)])
        self.assertNotEqual(code, 0)
        self.assertFalse(blocked_path.exists())
        self.assertEqual(json.loads(stdout.getvalue())["error"], "json_output_too_large")
        stdout = io.StringIO()
        with patch("pr_review.cli._MAX_INPUT_BYTES", 4096), contextlib.redirect_stdout(stdout):
            code = main(["prepare", "--repo", str(self.repo), "--base", self.base,
                         "--head", "HEAD"])
        self.assertNotEqual(code, 0)
        self.assertEqual(stdout.getvalue().count("\n"), 1)
        self.assertEqual(json.loads(stdout.getvalue())["error"], "json_output_too_large")

    def test_deep_json_input_returns_machine_readable_error(self):
        prep_path = self.root / "prep.json"
        created = self.cli("prepare", "--repo", self.repo, "--base", self.base,
                           "--head", "HEAD", "--output", prep_path)
        self.assertEqual(created.returncode, 0, created.stdout + created.stderr)
        nested_path = self.root / "nested.json"
        nested_path.write_text('{"findings":' + '[' * 100000 + '0' + ']' * 100000 + '}',
                               encoding="utf-8")
        result = self.cli("validate", "--preparation", prep_path,
                          "--findings", nested_path)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["error"], "json_nesting_too_deep")


if __name__ == "__main__":
    unittest.main()

"""Exercise the installed shell entry point without starting the review model."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


TASK_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = TASK_ROOT / "deploy" / "review-worker.sh"
TEMP_ROOT = TASK_ROOT / "demo" / ".test-tmp"


def _shell_tools():
    if os.name != "nt":
        return shutil.which("bash"), None
    git = shutil.which("git")
    if not git:
        return None, None
    git_root = Path(git).resolve().parent.parent
    bash = git_root / "bin" / "bash.exe"
    cygpath = git_root / "usr" / "bin" / "cygpath.exe"
    if bash.is_file() and cygpath.is_file():
        return str(bash), str(cygpath)
    return None, None


class DeployBudgetTests(unittest.TestCase):
    def setUp(self):
        self.bash, self.cygpath = _shell_tools()
        if not self.bash:
            self.skipTest("POSIX shell or Git Bash is unavailable")
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.shared_env = self.home / ".config" / "ai-advent-day31" / "service.env"
        self.review_env = self.home / ".config" / "ai-advent-day32" / "review.env"
        self.shared_env.parent.mkdir(parents=True)
        self.review_env.parent.mkdir(parents=True)
        self.shared_env.write_text("GRAPHICS_BUDGET_USD=1\n", encoding="utf-8")
        self._write_review_env()
        (self.home / "ai-advent-day32" / "runtime" / "week_7" / "task_2").mkdir(parents=True)
        fake_python = self.home / "ai-advent-day31" / ".venv" / "bin" / "python"
        fake_python.parent.mkdir(parents=True)
        fake_python.write_text(
            '#!/bin/sh\n'
            'printf "%s\\n" "$GRAPHICS_BUDGET_USD" > "$HOME/selected-budget"\n'
            'printf "%s\\n" "$@" > "$HOME/selected-args"\n'
            'printf "%s\\n" "$PR_REVIEW_DATA_DIR" > "$HOME/selected-data-dir"\n'
            'printf "%s\\n" "$PR_REVIEW_SERVICE_ROOT" > "$HOME/selected-service-root"\n',
            encoding="utf-8",
        )
        self._chmod(fake_python, "700")

    def _unix_path(self, path):
        if not self.cygpath:
            return str(path)
        return subprocess.check_output([self.cygpath, "-u", str(path)], text=True).strip()

    def _chmod(self, path, mode):
        subprocess.run([self.bash, "-c", 'chmod "$1" "$2"', "sh", mode,
                        self._unix_path(path)], check=True, capture_output=True, text=True)

    def _write_review_env(self):
        self.review_env.write_text("GRAPHICS_BUDGET_USD=0.15\n", encoding="utf-8")
        self._chmod(self.review_env, "600")

    def _run_launcher(self):
        env = os.environ.copy()
        env["HOME"] = self._unix_path(self.home)
        env["SSH_ORIGINAL_COMMAND"] = "GRAPHICS_BUDGET_USD=99"
        return subprocess.run([self.bash, self._unix_path(LAUNCHER)], env=env,
                              capture_output=True, text=True, timeout=15)

    def test_review_budget_overrides_shared_budget_and_preserves_worker_entry_point(self):
        result = self._run_launcher()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.home / "selected-budget").read_text().strip(), "0.15")
        self.assertEqual((self.home / "selected-args").read_text().splitlines(),
                         ["-B", "-m", "pr_review.worker"])
        self.assertEqual((self.home / "selected-data-dir").read_text().strip(),
                         f"{self._unix_path(self.home)}/ai-advent-day32/data")
        self.assertEqual((self.home / "selected-service-root").read_text().strip(),
                         f"{self._unix_path(self.home)}/ai-advent-day32/runtime/week_7/task_1")

    def test_missing_review_env_stops_before_worker(self):
        self.review_env.unlink()
        result = self._run_launcher()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.home / "selected-budget").exists())

    @unittest.skipIf(os.name == "nt", "Git Bash derives file mode from umask on NTFS")
    def test_insecure_review_env_stops_before_worker(self):
        self._chmod(self.review_env, "644")
        result = self._run_launcher()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.home / "selected-budget").exists())

    def test_review_env_without_own_budget_cannot_inherit_shared_budget(self):
        self.review_env.write_text("# separate review budget is required\n", encoding="utf-8")
        self._chmod(self.review_env, "600")
        result = self._run_launcher()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.home / "selected-budget").exists())


if __name__ == "__main__":
    unittest.main()

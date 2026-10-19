"""Trusted workflow metadata must not authorize a fork or stale payload."""
import copy
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from pr_review.ci_driver import _remote, main, select_request, ssh_command


def event():
    return {"workflow_run": {"event": "pull_request", "conclusion": "success",
            "path": ".github/workflows/day32-review-request.yml", "head_sha": "b" * 40,
            "head_repository": {"full_name": "owner/repo"},
            "repository": {"full_name": "owner/repo"}, "pull_requests": [{"number": 12}]}}


def pr():
    return {"number": 12, "state": "open", "draft": False,
            "base": {"sha": "a" * 40, "repo": {"full_name": "owner/repo"}},
            "head": {"sha": "b" * 40, "repo": {"full_name": "owner/repo"}}}


class CIDriverTests(unittest.TestCase):
    def test_current_same_repository_event_pins_revisions(self):
        result = select_request(event(), "owner/repo", lambda number: pr())
        self.assertEqual(result, {"number": 12, "base": "a" * 40, "head": "b" * 40})

    def test_fork_skips_before_any_authenticated_lookup(self):
        payload = event()
        payload["workflow_run"]["head_repository"]["full_name"] = "stranger/fork"
        self.assertEqual(select_request(payload, "owner/repo", lambda number: self.fail("lookup"))["skip"], "fork")

    def test_stale_head_and_closed_pr_never_reach_model(self):
        for field, value, expected in (("state", "closed", "closed_or_draft"),
                                       ("head", {"sha": "c" * 40, "repo": {"full_name": "owner/repo"}}, "stale_head")):
            current = pr()
            current[field] = value
            self.assertEqual(select_request(event(), "owner/repo", lambda number: current)["skip"], expected)

    def test_untrusted_numbers_and_wrong_workflow_rejected(self):
        for value in ("12; touch evil", True, -1):
            payload = event()
            payload["workflow_run"]["pull_requests"][0]["number"] = value
            with self.assertRaises(ValueError):
                select_request(payload, "owner/repo", lambda number: self.fail("lookup"))
        payload = event()
        payload["workflow_run"]["path"] = "evil.yml"
        with self.assertRaises(ValueError):
            select_request(payload, "owner/repo", lambda number: self.fail("lookup"))

    def test_ssh_has_fixed_remote_command_and_no_host_key_bypass(self):
        command = ssh_command("review@capsule.example", "/tmp/key", "/tmp/hosts")
        self.assertIn("StrictHostKeyChecking=yes", command)
        self.assertEqual(command[-1], "review")
        for host in ("-oProxyCommand=evil", "user@host; evil", "host\nother"):
            with self.assertRaises(ValueError):
                ssh_command(host, "/tmp/key", "/tmp/hosts")

    def test_worker_status_or_run_identity_mismatch_is_rejected(self):
        payload = {"schema_version": 1, "run_key": "run-one", "snapshot": {
            "base_revision": "a" * 40, "head_revision": "b" * 40}}
        original = {"status": "accepted", "review_accepted": True, "run_key": "run-one",
                    "base_revision": "a" * 40, "head_revision": "b" * 40}
        for changes in ({"status": "error"}, {"run_key": "another-run"},
                        {"base_revision": "c" * 40}, {"review_accepted": False}):
            with self.subTest(changes=changes):
                completed = subprocess.CompletedProcess([], 0, json.dumps({**original, **changes}).encode(), b"")
                with patch("pr_review.ci_driver.subprocess.run", return_value=completed):
                    with self.assertRaises(ValueError):
                        _remote(payload, "review@capsule.example", "fixture-key", "fixture-host-key")

    def test_base_change_or_draft_after_model_is_stale(self):
        for changes in ({"base": {"sha": "c" * 40, "repo": {"full_name": "owner/repo"}}},
                        {"draft": True}):
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                event_file = root / "event.json"
                event_file.write_text(json.dumps(event()), encoding="utf-8")
                snapshot = {"schema_version": 1, "base_revision": "a" * 40,
                            "head_revision": "b" * 40, "merge_base": "a" * 40,
                            "changed_files": [], "sources": [], "coverage": {"complete": True}, "limits": {}}
                env = {"GITHUB_REPOSITORY": "owner/repo", "GH_TOKEN": "fixture-token",
                       "GITHUB_EVENT_PATH": str(event_file), "PR_REVIEW_SSH_HOST": "review@capsule.example",
                       "PR_REVIEW_SSH_KEY": "fixture-key", "PR_REVIEW_KNOWN_HOSTS": "fixture-host-key"}
                def remote(payload, *args):
                    return {"status": "accepted", "review_accepted": True, "run_key": payload["run_key"],
                            "base_revision": "a" * 40, "head_revision": "b" * 40, "findings": []}
                with patch.dict(os.environ, env), patch("pr_review.ci_driver._fetch_pr", side_effect=[pr(), {**pr(), **changes}]), \
                        patch("pr_review.ci_driver.prepare_snapshot", return_value=snapshot), \
                        patch("pr_review.ci_driver._remote", side_effect=remote), redirect_stdout(io.StringIO()):
                    result = main(["--repo", str(root), "--output-dir", str(root / "output")])
                report = json.loads((root / "output/review.json").read_text(encoding="utf-8"))
                self.assertEqual(result, 2)
                self.assertEqual(report["status"], "stale")
                self.assertFalse(report["review_accepted"])


if __name__ == "__main__":
    unittest.main()

"""Trusted workflow metadata must not authorize a fork or stale payload."""
import copy
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from pr_review.ci_driver import _fetch_pr, _remote, main, select_request, ssh_command


def event():
    return {"workflow_run": {"event": "pull_request", "conclusion": "success",
            "path": ".github/workflows/day32-review-request.yml", "head_sha": "b" * 40,
            "head_repository": {"full_name": "owner/repo"},
            "repository": {"full_name": "owner/repo"}, "pull_requests": [{"number": 12}]}}


def pr():
    return {"number": 12, "state": "open", "draft": False,
            "base": {"sha": "a" * 40, "ref": "master", "repo": {"full_name": "owner/repo"}},
            "head": {"sha": "b" * 40, "repo": {"full_name": "owner/repo"}}}


def github_response(value):
    return io.BytesIO(json.dumps(value).encode("utf-8"))


def branch(sha, name="master", object_type="commit"):
    return {"ref": "refs/heads/" + name, "object": {"type": object_type, "sha": sha}}


class CIDriverTests(unittest.TestCase):
    def test_current_branch_ref_pins_snapshot_and_run_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            event_file = root / "event.json"
            event_file.write_text(json.dumps(event()), encoding="utf-8")
            snapshot = {"schema_version": 1, "base_revision": "c" * 40,
                        "head_revision": "b" * 40, "merge_base": "c" * 40,
                        "changed_files": [], "sources": [], "coverage": {"complete": True}, "limits": {}}
            calls = []
            replies = iter((pr(), branch("c" * 40), pr(), branch("c" * 40)))
            def fake_urlopen(request, timeout):
                calls.append(request.full_url)
                return github_response(next(replies))
            def remote(payload, *args):
                self.assertEqual(payload["snapshot"]["base_revision"], "c" * 40)
                expected = hashlib.sha256((f"day32-v1:owner/repo:12:{'c' * 40}:{'b' * 40}").encode("ascii")).hexdigest()
                self.assertEqual(payload["run_key"], expected)
                return {"status": "accepted", "review_accepted": True, "run_key": expected,
                        "base_revision": "c" * 40, "head_revision": "b" * 40, "findings": []}
            env = {"GITHUB_REPOSITORY": "owner/repo", "GH_TOKEN": "fixture-token",
                   "GITHUB_EVENT_PATH": str(event_file), "PR_REVIEW_SSH_HOST": "review@capsule.example",
                   "PR_REVIEW_SSH_KEY": "fixture-key", "PR_REVIEW_KNOWN_HOSTS": "fixture-host-key"}
            with patch.dict(os.environ, env), patch("pr_review.ci_driver.urlopen", side_effect=fake_urlopen), \
                    patch("pr_review.ci_driver.prepare_snapshot", return_value=snapshot) as prepare, \
                    patch("pr_review.ci_driver._remote", side_effect=remote), redirect_stdout(io.StringIO()):
                code = main(["--repo", str(root), "--output-dir", str(root / "out")])
            self.assertEqual(code, 0)
            self.assertEqual(prepare.call_args.args[1:3], ("c" * 40, "b" * 40))
            self.assertEqual(len(calls), 4)
            self.assertEqual(json.loads((root / "out/review.json").read_text(encoding="utf-8"))["status"], "accepted")

    def test_branch_moves_after_model_withholds_findings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            event_file = root / "event.json"
            event_file.write_text(json.dumps(event()), encoding="utf-8")
            snapshot = {"schema_version": 1, "base_revision": "c" * 40,
                        "head_revision": "b" * 40, "merge_base": "c" * 40,
                        "changed_files": [], "sources": [], "coverage": {"complete": True}, "limits": {}}
            replies = iter((pr(), branch("c" * 40), pr(), branch("d" * 40)))
            calls = []
            def fake_urlopen(request, timeout):
                calls.append(request.full_url)
                return github_response(next(replies))
            def remote(payload, *args):
                return {"status": "accepted", "review_accepted": True, "run_key": payload["run_key"],
                        "base_revision": "c" * 40, "head_revision": "b" * 40,
                        "findings": [{"message": "must withhold"}]}
            env = {"GITHUB_REPOSITORY": "owner/repo", "GH_TOKEN": "fixture-token",
                   "GITHUB_EVENT_PATH": str(event_file), "PR_REVIEW_SSH_HOST": "review@capsule.example",
                   "PR_REVIEW_SSH_KEY": "fixture-key", "PR_REVIEW_KNOWN_HOSTS": "fixture-host-key"}
            with patch.dict(os.environ, env), patch("pr_review.ci_driver.urlopen", side_effect=fake_urlopen), \
                    patch("pr_review.ci_driver.prepare_snapshot", return_value=snapshot), \
                    patch("pr_review.ci_driver._remote", side_effect=remote) as worker, redirect_stdout(io.StringIO()):
                code = main(["--repo", str(root), "--output-dir", str(root / "out")])
            report = json.loads((root / "out/review.json").read_text(encoding="utf-8"))
            self.assertEqual(code, 2)
            self.assertEqual(report["status"], "stale")
            self.assertEqual(report["findings"], [])
            self.assertFalse(report["review_accepted"])
            self.assertEqual(worker.call_count, 1)
            self.assertEqual(len(calls), 4)

    def test_branch_lookup_failures_never_use_cached_base(self):
        invalid = [None, {}, branch("c" * 40, "other"), branch("c" * 40, object_type="tag"),
                   branch("not-an-oid")]
        for reference in invalid:
            with self.subTest(reference=reference):
                calls = []
                def fake_urlopen(request, timeout):
                    calls.append(request.full_url)
                    if len(calls) == 1:
                        return github_response(pr())
                    return github_response(reference)
                with patch("pr_review.ci_driver.urlopen", side_effect=fake_urlopen):
                    with self.assertRaises(ValueError):
                        _fetch_pr("owner/repo", "fixture-token", 12)
                self.assertEqual(len(calls), 2)
        with patch("pr_review.ci_driver.urlopen", side_effect=[github_response(pr()), OSError("lookup failed")]):
            with self.assertRaises(OSError):
                _fetch_pr("owner/repo", "fixture-token", 12)

    def test_foreign_base_and_invalid_ref_stop_before_second_request(self):
        for bad in ({"repo": {"full_name": "other/repo"}}, {"ref": "../evil"},
                    {"ref": "master?next=https://evil.test"}, {"ref": "master\nX-Header: evil"},
                    {"ref": None}, {"ref": 7}, {"ref": "x" * 300}):
            with self.subTest(bad=bad):
                current = pr()
                current["base"].update(bad)
                calls = []
                def fake_urlopen(request, timeout):
                    calls.append(request.full_url)
                    return github_response(current)
                with patch("pr_review.ci_driver.urlopen", side_effect=fake_urlopen):
                    with self.assertRaises(ValueError):
                        _fetch_pr("owner/repo", "fixture-token", 12)
                self.assertEqual(len(calls), 1)

    def test_failed_branch_lookup_stops_before_snapshot_or_remote(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            event_file = root / "event.json"
            event_file.write_text(json.dumps(event()), encoding="utf-8")
            env = {"GITHUB_REPOSITORY": "owner/repo", "GH_TOKEN": "fixture-token",
                   "GITHUB_EVENT_PATH": str(event_file), "PR_REVIEW_SSH_HOST": "review@capsule.example",
                   "PR_REVIEW_SSH_KEY": "fixture-key", "PR_REVIEW_KNOWN_HOSTS": "fixture-host-key"}
            with patch.dict(os.environ, env), \
                    patch("pr_review.ci_driver.urlopen", side_effect=[github_response(pr()), OSError("lookup failed")]), \
                    patch("pr_review.ci_driver.prepare_snapshot") as prepare, \
                    patch("pr_review.ci_driver._remote") as worker, redirect_stdout(io.StringIO()):
                code = main(["--repo", str(root), "--output-dir", str(root / "out")])
            report = json.loads((root / "out/review.json").read_text(encoding="utf-8"))
            self.assertEqual(code, 2)
            self.assertEqual(report["status"], "error")
            self.assertFalse(report["review_accepted"])
            prepare.assert_not_called()
            worker.assert_not_called()

    def test_slash_branch_is_encoded_and_ref_identity_checked(self):
        current = pr()
        current["base"]["ref"] = "fix/current-base"
        calls = []
        def fake_urlopen(request, timeout):
            calls.append(request.full_url)
            return github_response(current if len(calls) == 1 else branch("c" * 40, "fix/current-base"))
        with patch("pr_review.ci_driver.urlopen", side_effect=fake_urlopen):
            resolved = _fetch_pr("owner/repo", "fixture-token", 12)
        self.assertEqual(resolved["base"]["sha"], "c" * 40)
        self.assertEqual(current["base"]["sha"], "a" * 40)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1].split("/git/ref/heads/")[1], "fix%2Fcurrent-base")

    def test_fork_or_closed_pr_metadata_does_not_lookup_branch(self):
        for change in ({"head": {"sha": "b" * 40, "repo": {"full_name": "stranger/fork"}}},
                       {"state": "closed"}, {"draft": True}):
            with self.subTest(change=change):
                current = pr()
                current.update(change)
                calls = []
                def fake_urlopen(request, timeout):
                    calls.append(request.full_url)
                    return github_response(current)
                with patch("pr_review.ci_driver.urlopen", side_effect=fake_urlopen):
                    try:
                        resolved = _fetch_pr("owner/repo", "fixture-token", 12)
                    except ValueError:
                        resolved = None
                self.assertEqual(len(calls), 1)
                self.assertEqual(resolved, current)
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

"""CLI, machine worker and browser report boundaries without live calls."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from pr_review.cli import main
from pr_review.report import render_report
from pr_review.worker import execute_request


class DeliveryTests(unittest.TestCase):
    def test_report_escapes_source_text_and_has_no_executable_links(self):
        report = {"status": "accepted", "review_accepted": True, "run_key": "demo",
                  "findings": [{"title": '<script>alert("bad")</script>', "severity": "high",
                                "problem": "Проблема", "location": {"path": '<img src=x onerror=alert(1)>',
                                                                           "start_line": 2}}],
                  "ledger": {"complete": False, "cost_estimated_usd": None,
                             "known_cost_estimated_usd": 0.012}}
        html = render_report(report)
        self.assertNotIn("<script>", html)
        self.assertNotIn("<img ", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("0.012", html)
        self.assertIn("неизвест", html.lower())

    def test_accepted_report_displays_incomplete_instance_and_operator_ack(self):
        report = {"status": "accepted", "review_accepted": True, "findings": [],
                  "ledger": {"complete": True, "cost_estimated_usd": 0.01,
                             "known_cost_estimated_usd": 0.01, "calls": [],
                             "instance_accounting": {
                                 "complete": False, "cost_estimated_usd": None,
                                 "known_cost_estimated_usd": 0.01210035,
                                 "budget_usd": 0.15, "unknown_call_ids": ["lost-call-1"],
                                 "operator_resolutions": [{"call_id": "lost-call-1",
                                                           "resolution": "ack_local_embedding_usage_loss",
                                                           "resolved_at_utc": "2026-10-19T07:00:00+00:00"}]}}}
        html = render_report(report)
        self.assertIn("0.01210035", html)
        self.assertIn("0.15", html)
        self.assertIn("lost-call-1", html)
        self.assertIn("ack_local_embedding_usage_loss", html)
        self.assertIn("Учёт всего контура", html)
        self.assertIn("неполный", html)

    def test_rejected_report_never_renders_draft_findings(self):
        html = render_report({"status": "rejected", "review_accepted": False,
                              "findings": [{"title": "REJECTED_DRAFT_SENTINEL"}]})
        self.assertNotIn("REJECTED_DRAFT_SENTINEL", html)
        self.assertIn("отклон", html.lower())

    def test_worker_rejects_configuration_in_untrusted_payload(self):
        calls = []
        with self.assertRaises(ValueError):
            execute_request({"schema_version": 1, "run_key": "demo", "snapshot": {},
                             "service_root": "/tmp/evil"}, lambda *args: calls.append(args))
        self.assertEqual(calls, [])

    def test_worker_rejects_operator_resolution_fields(self):
        calls = []
        with self.assertRaises(ValueError):
            execute_request({"schema_version": 1, "run_key": "demo", "snapshot": {},
                             "operator_resolution": {"call_id": "lost-call-1",
                                                     "authorization_ref": "FORGED-REF"}},
                            lambda *args: calls.append(args))
        self.assertEqual(calls, [])

    def test_worker_passes_data_and_identity_without_interpreting_commands(self):
        calls = []
        def run(snapshot, run_key):
            calls.append((snapshot, run_key))
            return {"status": "accepted", "review_accepted": True}
        snapshot = {"sources": [{"text": "$(touch /tmp/evil)"}]}
        result = execute_request({"schema_version": 1, "run_key": "demo-123", "snapshot": snapshot}, run)
        self.assertTrue(result["review_accepted"])
        self.assertEqual(calls, [(snapshot, "demo-123")])

    def test_review_requires_explicit_live_flag_before_loading_runtime(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["review", "--preparation", "missing.json", "--data-dir", "unused", "--run-key", "demo"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue())["error"], "live_confirmation_required")

    def test_render_cli_writes_new_html_only(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.json"
            output = Path(directory) / "report.html"
            report.write_text(json.dumps({"status": "incomplete", "review_accepted": False}), encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                code = main(["render", "--report", str(report), "--output", str(output)])
            self.assertEqual(code, 0)
            self.assertIn("<!doctype html>", output.read_text(encoding="utf-8"))
            original = output.read_bytes()
            with redirect_stdout(io.StringIO()):
                code = main(["render", "--report", str(report), "--output", str(output)])
            self.assertEqual(code, 2)
            self.assertEqual(output.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()

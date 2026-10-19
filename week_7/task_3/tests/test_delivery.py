"""Delivery boundary: actual archive bytes and opt-in command behavior."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch


TASK = Path(__file__).resolve().parents[1]
ROOT = TASK.parents[1]
SCRIPTS = TASK / "scripts"


def load_owned_script(name):
    spec = importlib.util.spec_from_file_location("task3_delivery_" + name, SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DeliveryTests(unittest.TestCase):
    def test_archive_has_required_integrated_sources_and_verified_bytes(self):
        package = load_owned_script("package_delivery").package

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "delivery.tar.gz"
            package(ROOT, target)
            with tarfile.open(target, "r:gz") as archive:
                names = archive.getnames()
                manifest = json.load(archive.extractfile("delivery-manifest.json"))
                self.assertIn("week_7/task_1/graphics/hybrid.py", names)
                self.assertIn("week_7/task_1/graphics/validation.py", names)
                self.assertIn("week_7/task_1/graphics/support_integration.py", names)
                self.assertIn("week_7/task_1/docs/usage.md", names)
                self.assertIn("week_7/task_1/corpus/support-faq.md", names)
                self.assertIn("week_7/task_3/support/mcp_server.py", names)
                self.assertEqual(set(names) - {"delivery-manifest.json"}, set(manifest["files"]))
                for name, facts in manifest["files"].items():
                    payload = archive.extractfile(name).read()
                    self.assertEqual(facts, {"sha256": hashlib.sha256(payload).hexdigest(),
                                             "size": len(payload)})

    def test_archive_excludes_private_and_hidden_data(self):
        package = load_owned_script("package_delivery").package

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "delivery.tar.gz"
            package(ROOT, target)
            with tarfile.open(target, "r:gz") as archive:
                names = archive.getnames()
            for name in names:
                self.assertNotIn("demo", name.split("/"))
                self.assertNotIn("data", name.split("/"))
                self.assertNotIn(".git", name.split("/"))
                self.assertNotIn(".recording-tmp", name.split("/"))
                if name != "week_7/task_1/.env.example":
                    self.assertNotIn(".env", name)

    def test_archive_refuses_existing_target(self):
        package = load_owned_script("package_delivery").package

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "delivery.tar.gz"
            target.write_bytes(b"previous")
            with self.assertRaises(FileExistsError):
                package(ROOT, target)
            self.assertEqual(target.read_bytes(), b"previous")

    def test_archive_refuses_symlink_source_when_os_allows_creation(self):
        package = load_owned_script("package_delivery").package

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "week_7/task_1/graphics").mkdir(parents=True)
            (root / "week_7/task_3/support").mkdir(parents=True)
            try:
                (root / "week_7/task_1/graphics/service.py").symlink_to(TASK / "README.md")
            except OSError:
                self.skipTest("Windows symlink privilege unavailable")
            with self.assertRaises((ValueError, FileNotFoundError)):
                package(root, root / "out.tar.gz")

    def test_live_check_default_never_reads_credentials_or_creates_state(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "state.json"
            process = subprocess.run(
                [sys.executable, "-B", str(SCRIPTS / "live_check.py"),
                 "--phase", "prepare", "--base-url", "https://127.0.0.1:1",
                 "--access-file", str(Path(directory) / "missing.json"),
                 "--state-file", str(target)], capture_output=True, text=True,
                timeout=10, env={key: value for key, value in os.environ.items()
                                 if not key.startswith("GRAPHICS_") and key != "ZAI_API_KEY"})
            self.assertNotEqual(process.returncode, 0)
            self.assertIn("--run-live", process.stderr)
            self.assertFalse(target.exists())

    def test_prepare_retains_request_intent_when_first_mutation_fails(self):
        prepare = load_owned_script("live_check").prepare

        class InterruptedSite:
            origin = "https://example.test"

            def login(self, _actor):
                pass

            def required(self, _method, _path, _body):
                return {"id": "chat", "branch_id": "branch"}

            def ask(self, _branch, _question, request_id=None):
                raise RuntimeError("unknown commit outcome")

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            with self.assertRaisesRegex(RuntimeError, "unknown commit outcome"):
                prepare(InterruptedSite(), {"primary": {}}, state)
            saved = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(saved["branch_id"], "branch")
            self.assertFalse(saved["complete"])
            self.assertTrue(saved["request_ids"])

    def test_prepare_stops_after_unexpected_generated_control(self):
        prepare = load_owned_script("live_check").prepare

        class WrongSite:
            origin = "https://example.test"

            def __init__(self):
                self.questions = []

            def login(self, _actor):
                pass

            def required(self, _method, _path, _body):
                return {"id": "chat", "branch_id": "branch"}

            def ask(self, _branch, question, request_id=None):
                self.questions.append(question)
                return {"status": "accepted", "answer": "unexpected generated answer",
                        "usage": {"total_tokens": 12}}

        site = WrongSite()
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            with self.assertRaisesRegex(RuntimeError, "support_control"):
                prepare(site, {"primary": {}}, state)
            saved = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(len(site.questions), 1)
            self.assertEqual(saved["observed"][0]["status"], "accepted")
            self.assertFalse(saved["complete"])

    def _verification_fixture(self, directory, *, interrupt_at=None, wrong_control=False,
                              other_result=None, other_error=None):
        live = load_owned_script("live_check")
        state = Path(directory) / "state.json"
        receipt = Path(directory) / "receipt.json"
        ticket = "a" * 32
        state.write_text(json.dumps({"complete": True, "origin": "https://example.test",
                                     "branch_id": "branch", "ticket_id": ticket}), encoding="utf-8")

        class Primary:
            origin = "https://example.test"

            def __init__(self):
                self.paid_calls = 0
                self.login_calls = 0
                self.control_calls = 0

            def login(self, _actor):
                self.login_calls += 1

            def required(self, method, path, body=None):
                if path == "/api/ledger":
                    return {"complete": True, "cost_estimated_usd": 0.0,
                            "calls": [], "total_tokens": 0}
                if method == "GET" and path == "/api/branches/branch":
                    return {"id": "branch"}
                raise AssertionError((method, path))

            def ask(self, branch, question, request_id=None):
                if question.startswith("/support Как") or question.startswith("Почему"):
                    self.paid_calls += 1
                    if self.paid_calls == interrupt_at:
                        raise RuntimeError("paid request interrupted")
                    return {"status": "accepted", "answer": "observed synthetic explanation",
                            "sources": [], "usage": {"total_tokens": 7}}
                self.control_calls += 1
                if wrong_control and self.control_calls == 1:
                    return {"status": "accepted", "answer": "wrong control",
                            "usage": {"total_tokens": 3}}
                if question == "/support /me":
                    message = "QA; day33-example: использовать пример матового шара"
                elif question == "/support /status":
                    message = "storage: ready"
                elif question.endswith("/ticket get " + ticket):
                    message = f"Обращение {ticket}: вопрос касается панели источников сервиса, статус open"
                else:
                    message = "closed_by_user"
                return {"status": "support_control", "message": message,
                        "support_confirmation": {"receipt": {"operation": "close"}}}

        class Other:
            requests = []
            intent_snapshots = []

            def __init__(self, _origin):
                pass

            def login(self, _actor):
                pass

            def call(self, method, path):
                return 403, {"error": "denied"}

            def required(self, method, path, body=None):
                if path == "/api/chats":
                    return {"branch_id": "other"}
                if method != "POST" or path != "/api/branches/other/ask":
                    raise AssertionError((method, path))
                self.requests.append(dict(body))
                self.intent_snapshots.append(json.loads(receipt.read_text(encoding="utf-8")).get("other_ticket"))
                if other_error is not None:
                    raise other_error
                return other_result if other_result is not None else {
                    "status": "input_rejected", "message": "not_found"}

        return live, Primary(), Other, state, receipt

    def test_existing_verify_receipt_stops_before_any_request(self):
        with tempfile.TemporaryDirectory() as directory:
            live, site, other, state, receipt = self._verification_fixture(directory)
            receipt.write_text("previous", encoding="utf-8")
            with patch.object(live, "Site", other):
                with self.assertRaises(FileExistsError):
                    live.verify(site, {"primary": {}, "other": {}}, state, receipt)
            self.assertEqual(site.login_calls, 0)
            self.assertEqual(site.paid_calls, 0)
            self.assertEqual(receipt.read_text(encoding="utf-8"), "previous")

    def test_verify_persists_first_and_second_paid_interruption(self):
        for interrupted in (1, 2):
            with self.subTest(interrupted=interrupted), tempfile.TemporaryDirectory() as directory:
                live, site, other, state, receipt = self._verification_fixture(
                    directory, interrupt_at=interrupted)
                with patch.object(live, "Site", other):
                    with self.assertRaisesRegex(RuntimeError, "paid request interrupted"):
                        live.verify(site, {"primary": {}, "other": {}}, state, receipt)
                saved = json.loads(receipt.read_text(encoding="utf-8"))
                attempts = saved["paid_attempts"]
                self.assertEqual(len(attempts), interrupted)
                self.assertTrue(all(item["request_id"] for item in attempts))
                self.assertEqual(attempts[-1]["status"], "unknown")
                if interrupted == 2:
                    self.assertEqual(attempts[0]["status"], "accepted")
                    self.assertEqual(attempts[0]["usage"], {"total_tokens": 7})
                self.assertEqual(site.paid_calls, interrupted)

    def test_verify_stops_after_unexpected_generated_control(self):
        with tempfile.TemporaryDirectory() as directory:
            live, site, other, state, receipt = self._verification_fixture(
                directory, wrong_control=True)
            with patch.object(live, "Site", other):
                with self.assertRaisesRegex(RuntimeError, "support_control"):
                    live.verify(site, {"primary": {}, "other": {}}, state, receipt)
            saved = json.loads(receipt.read_text(encoding="utf-8"))
            self.assertEqual(saved["controls"][0]["status"], "accepted")
            self.assertEqual(site.control_calls, 1)
            self.assertEqual(site.paid_calls, 0)


    def test_other_actor_accepted_probe_retains_intent_answer_and_usage_then_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            live, site, other, state, receipt = self._verification_fixture(
                directory, other_result={"status": "accepted", "answer": "unexpected isolated answer",
                                         "usage": {"total_tokens": 19}})
            with patch.object(live, "Site", other):
                with self.assertRaisesRegex(RuntimeError, "Other actor ticket read was accepted"):
                    live.verify(site, {"primary": {}, "other": {}}, state, receipt)
                saved = json.loads(receipt.read_text(encoding="utf-8"))
                self.assertIn("other_ticket", saved)
                entry = saved["other_ticket"]
                self.assertEqual(entry["actor"], "other")
                self.assertEqual(entry["branch_id"], "other")
                self.assertEqual(entry["question"], "/support /ticket get " + "a" * 32)
                self.assertTrue(entry["request_id"])
                self.assertEqual(entry["request_id"], other.requests[0]["request_id"])
                self.assertEqual(other.intent_snapshots[0]["status"], "intent")
                self.assertEqual(other.intent_snapshots[0]["request_id"], entry["request_id"])
                self.assertEqual(entry["status"], "accepted")
                self.assertEqual(entry["answer"], "unexpected isolated answer")
                self.assertEqual(entry["usage"], {"total_tokens": 19})
                self.assertEqual(site.paid_calls, 0)
                self.assertEqual(saved["paid_attempts"], [])
                previous = receipt.read_bytes()
                with self.assertRaises(FileExistsError):
                    live.verify(site, {"primary": {}, "other": {}}, state, receipt)
                self.assertEqual(receipt.read_bytes(), previous)
                self.assertEqual(len(other.requests), 1)

    def test_other_actor_probe_timeout_retains_unknown_intent_then_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            live, site, other, state, receipt = self._verification_fixture(
                directory, other_error=TimeoutError("probe outcome unknown"))
            with patch.object(live, "Site", other):
                with self.assertRaises(TimeoutError):
                    live.verify(site, {"primary": {}, "other": {}}, state, receipt)
                saved = json.loads(receipt.read_text(encoding="utf-8"))
                self.assertIn("other_ticket", saved)
                entry = saved["other_ticket"]
                self.assertEqual(entry["actor"], "other")
                self.assertEqual(entry["branch_id"], "other")
                self.assertEqual(entry["question"], "/support /ticket get " + "a" * 32)
                self.assertTrue(entry["request_id"])
                self.assertEqual(entry["request_id"], other.requests[0]["request_id"])
                self.assertEqual(other.intent_snapshots[0]["status"], "intent")
                self.assertEqual(other.intent_snapshots[0]["request_id"], entry["request_id"])
                self.assertEqual(entry["status"], "unknown")
                self.assertEqual(entry["error_type"], "TimeoutError")
                self.assertEqual(site.paid_calls, 0)
                self.assertEqual(saved["paid_attempts"], [])
                previous = receipt.read_bytes()
                with self.assertRaises(FileExistsError):
                    live.verify(site, {"primary": {}, "other": {}}, state, receipt)
                self.assertEqual(receipt.read_bytes(), previous)
                self.assertEqual(len(other.requests), 1)


if __name__ == "__main__":
    unittest.main()

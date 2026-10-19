"""Flask integration retains day31 authentication and serves private files."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

if "graphics" not in sys.modules:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "task_1"))

from graphics.config import Settings
from graphics.service import ChatService
from graphics.store import Store
from graphics.web import RateLimiter
from learning_files.repository import FileRepository
from learning_files.web import create_app


class FileWebTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        app_root = Path(__file__).resolve().parents[2] / "task_1"
        self.settings = Settings(app_root, root / "data", api_key="fixture",
                                 secret_key="test-secret-key", secure_cookies=False)
        self.store = Store(self.settings.db_path)
        self.user = self.store.create_user("owner", "long-fixture-password")
        self.branch = self.store.create_chat(self.user)["branch_id"]
        self.other = self.store.create_user("other", "long-fixture-password")
        self.repo = FileRepository(root / "data" / "learning-files")
        self.app = create_app(self.settings, self.store,
                              ChatService(self.settings, self.store), self.repo)
        self.client = self.app.test_client()
        self.csrf = self.client.get("/api/csrf").json["csrf_token"]

    def post(self, path, payload=None, *, csrf=True):
        return self.client.post(path, json=payload or {}, headers={
            "Origin": "http://localhost", "X-CSRF-Token": self.csrf if csrf else "bad"})

    def login(self, username="owner"):
        response = self.post("/api/login", {"username": username,
                                            "password": "long-fixture-password"})
        self.assertEqual(response.status_code, 200)
        self.csrf = response.json["csrf_token"]

    def test_existing_home_and_self_hosted_assets(self):
        html = self.client.get("/").data.decode()
        self.assertIn("/learning-files/files.js", html)
        self.assertIn("/learning-files/files.css", html)
        self.assertIn("/static/app.js", html)
        asset = self.client.get("/learning-files/files.js")
        self.assertEqual(asset.status_code, 200)
        asset.close()

    def test_anonymous_csrf_and_server_owned_workspace(self):
        self.assertEqual(self.client.get("/api/learning-files/inventory").status_code, 401)
        self.login()
        self.assertEqual(self.post("/api/learning-files/examples", csrf=False).status_code, 403)
        seeded = self.post("/api/learning-files/examples")
        self.assertEqual(seeded.status_code, 200)
        self.assertGreaterEqual(len(seeded.json["added"]), 6)
        again = self.post("/api/learning-files/examples")
        self.assertEqual(again.json["added"], [])
        inventory = self.client.get("/api/learning-files/inventory").json["files"]
        self.assertEqual(len(inventory), 6)
        self.assertEqual(self.client.get("/api/learning-files/file?path=../x").status_code, 400)
        self.assertEqual(self.client.get("/api/learning-files/file?path=sources/materials.md").status_code, 200)
        self.assertEqual(self.post("/api/learning-files/prepare", {"branch_id": "foreign",
            "goal": "Сделай памятку", "request_id": "r1"}).status_code, 404)

    def test_file_content_remains_data_and_other_owner_cannot_read(self):
        self.repo.seed(self.user, {"notes/x.md": "<script>alert(1)</script>"})
        self.login()
        response = self.client.get("/api/learning-files/file?path=notes/x.md")
        self.assertEqual(response.json["content"], "<script>alert(1)</script>")
        self.post("/api/logout")
        self.csrf = self.client.get("/api/csrf").json["csrf_token"]
        self.login("other")
        self.assertEqual(self.client.get("/api/learning-files/file?path=notes/x.md").status_code, 404)

    def test_apply_requires_owned_run_and_matching_branch(self):
        self.repo.seed(self.user, {"sources/materials.md": "Материал и шейдер.\n"})
        original = self.repo.workspace(self.user).read("sources/materials.md")
        run = self.repo.start(self.user, self.branch, "apply-test", "Сделай памятку")["run"]
        proposal = self.repo.build(self.user, [{"path": "outputs/guide.md", "content": "Памятка\n"}],
                                   [original])
        self.repo.accept(self.user, run["id"], proposal, "Подготовлена памятка")
        self.login()
        url = f"/api/learning-files/runs/{run['id']}"
        self.assertEqual(self.post(url + "/apply", {"branch_id": "wrong"}).status_code, 404)
        self.assertEqual(self.post(url + "/apply", {"branch_id": self.branch, "changes": []}).status_code, 400)
        self.assertNotIn("outputs/guide.md", [x["path"] for x in self.repo.workspace(self.user).inventory()])
        applied = self.post(url + "/apply", {"branch_id": self.branch})
        self.assertEqual(applied.json["status"], "applied")
        self.post("/api/logout")
        self.csrf = self.client.get("/api/csrf").json["csrf_token"]
        self.login("other")
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.post(url + "/apply", {"branch_id": self.branch}).status_code, 404)

    def test_paid_ip_uses_forwarded_address_only_from_loopback_proxy(self):
        self.login()
        payload = {"branch_id": self.branch, "goal": "Подготовь памятку", "request_id": "ip-1"}
        with patch.object(RateLimiter, "take", return_value=True) as take, \
             patch("learning_files.web.FileAgent.prepare", return_value={"status": "error"}):
            response = self.client.post("/api/learning-files/prepare", json=payload,
                                        headers={"Origin": "http://localhost", "X-CSRF-Token": self.csrf,
                                                 "X-Real-IP": "203.0.113.8"})
            self.assertEqual(response.status_code, 200)
            self.assertIn("paid-ip:203.0.113.8", [call.args[0] for call in take.call_args_list])
            take.reset_mock()
            payload["request_id"] = "ip-2"
            response = self.client.post("/api/learning-files/prepare", json=payload,
                                        headers={"Origin": "http://localhost", "X-CSRF-Token": self.csrf,
                                                 "X-Real-IP": "203.0.113.8"},
                                        environ_overrides={"REMOTE_ADDR": "198.51.100.9"})
            self.assertEqual(response.status_code, 200)
            self.assertIn("paid-ip:198.51.100.9", [call.args[0] for call in take.call_args_list])

    def test_recovery_finds_same_owned_request_and_latest_without_model_call(self):
        self.repo.seed(self.user, {"sources/materials.md": "Материал и шейдер.\n"})
        first = self.repo.start(self.user, self.branch, "lost-response", "Подготовь памятку")["run"]
        snapshot = self.repo.workspace(self.user).read("sources/materials.md")
        proposal = self.repo.build(self.user, [{"path": "outputs/guide.md", "content": "Памятка\n"}],
                                   [snapshot])
        completed = self.repo.accept(self.user, first["id"], proposal, "Готово")
        second = self.repo.start(self.user, self.branch, "second", "Другая цель")["run"]
        self.login()
        base = "/api/learning-files/recovery?branch_id=" + self.branch
        self.assertEqual(self.client.get(base).json["run"]["id"], second["id"])
        with patch("learning_files.web.FileAgent.prepare", side_effect=AssertionError("new model run")):
            recovered = self.client.get(base + "&request_id=lost-response").json["run"]
        self.assertEqual(recovered["id"], completed["id"])
        self.assertEqual(recovered["changes"][0]["diff"], completed["changes"][0]["diff"])
        self.assertIsNone(self.client.get(base + "&request_id=unknown").json["run"])
        self.assertEqual(self.client.get(base + "&request_id=second&request_id=second").status_code, 400)
        self.post("/api/logout")
        self.csrf = self.client.get("/api/csrf").json["csrf_token"]
        self.login("other")
        self.assertEqual(self.client.get(base).status_code, 404)
        other_branch = self.store.create_chat(self.other)["branch_id"]
        self.assertIsNone(self.client.get("/api/learning-files/recovery?branch_id=" + other_branch).json["run"])
        self.assertIsNone(self.client.get("/api/learning-files/recovery?branch_id=" + other_branch + "&request_id=lost-response").json["run"])


if __name__ == "__main__":
    unittest.main()

"""Saved explanations use the real Store, Workspace and Flask authentication."""

import sys
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
for package in (ROOT / "task_1", ROOT / "task_4"):
    if str(package) not in sys.path:
        sys.path.insert(0, str(package))

from graphics.config import Settings
from graphics.store import Store
from learning_files.repository import FileRepository
from learning_product.web import create_app
from learning_product.materials import save_material


class MaterialsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.settings = Settings(ROOT / "task_1", root / "data", api_key="fixture",
                                 secret_key="test-secret-key", secure_cookies=False)
        self.store = Store(self.settings.db_path)
        self.owner = self.store.create_user("owner", "long-fixture-password")
        self.other = self.store.create_user("other", "long-fixture-password")
        self.branch = self.store.create_chat(self.owner)["branch_id"]
        self.repository = FileRepository(root / "data" / "learning-files")
        self.app = create_app(self.settings, self.store, repository=self.repository)
        self.client = self.app.test_client()
        self.csrf = self.client.get("/api/csrf").json["csrf_token"]

    def login(self, name="owner"):
        response = self.client.post("/api/login", json={"username": name,
            "password": "long-fixture-password"}, headers={
                "Origin": "http://localhost", "X-CSRF-Token": self.csrf})
        self.assertEqual(response.status_code, 200)
        self.csrf = response.json["csrf_token"]

    def post(self, branch, data, *, origin="http://localhost", csrf=None):
        return self.client.post(f"/api/branches/{branch}/materials", json=data,
            headers={"Origin": origin, "X-CSRF-Token": self.csrf if csrf is None else csrf})

    def accepted(self, question="Чем отличаются материал и шейдер?", answer="Материал задаёт параметры.\nШейдер рассчитывает вид.", sources=None):
        sources = sources if sources is not None else [{"title": "Unity Manual", "url": "https://docs.unity3d.com/", "section": "Materials"}]
        turn = self.store.begin_turn(self.owner, self.branch, question)
        self.store.append_message(self.owner, self.branch, "user", question, turn_id=turn["id"])
        for state in ("input_check", "context", "sources", "draft", "program_check", "semantic_check"):
            self.store.move_turn(turn["id"], state)
        return self.store.publish_answer(self.owner, self.branch, turn["id"], answer, sources)

    def test_owner_saves_and_downloads_exact_utf8_material(self):
        message = self.accepted()
        self.login()
        saved = self.post(self.branch, {"message_id": message["id"]})
        self.assertEqual(saved.status_code, 201)
        path = saved.json["path"]
        self.assertTrue(path.startswith("outputs/answer-"))
        stored = self.repository.workspace(self.owner).read(path)["content"].encode("utf-8")
        self.assertIn("Чем отличаются материал и шейдер?".encode(), stored)
        self.assertIn("Материал задаёт параметры.\nШейдер рассчитывает вид.".encode(), stored)
        self.assertIn("Unity Manual".encode(), stored)
        self.assertIn(b"https://docs.unity3d.com/", stored)
        response = self.client.get(saved.json["download_url"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, stored)
        self.assertEqual(response.mimetype, "text/plain")
        self.assertIn("attachment", response.headers["Content-Disposition"])
        self.assertIn("private", response.headers["Cache-Control"])
        self.assertIn("no-store", response.headers["Cache-Control"])

    def test_repeat_keeps_existing_bytes_and_modified_target_conflicts(self):
        message = self.accepted()
        self.login()
        first = self.post(self.branch, {"message_id": message["id"]})
        self.assertEqual(first.status_code, 201)
        target = self.repository.workspace(self.owner)._checked_path(first.json["path"])
        before = target.stat().st_mtime_ns
        repeated = self.post(self.branch, {"message_id": message["id"]})
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(repeated.json["path"], first.json["path"])
        self.assertFalse(repeated.json["created"])
        self.assertEqual(target.stat().st_mtime_ns, before)
        target.write_text("Changed by owner", encoding="utf-8")
        rejected = self.post(self.branch, {"message_id": message["id"]})
        self.assertEqual(rejected.status_code, 409)
        self.assertEqual(target.read_text(encoding="utf-8"), "Changed by owner")

    def test_invalid_message_owner_and_rejected_turn_create_no_files(self):
        message = self.accepted()
        user_message = self.store.get_branch(self.owner, self.branch)["messages"][0]
        other_branch = self.store.create_chat(self.other)["branch_id"]
        self.login()
        for branch, identifier in ((self.branch, user_message["id"]),
                                   (other_branch, message["id"]),
                                   (self.branch, "nonexistent")):
            self.assertEqual(self.post(branch, {"message_id": identifier}).status_code, 404)
        with patch.object(self.store, "get_turn", return_value={"status": "rejected",
                "branch_id": self.branch, "question": "Чем отличаются материал и шейдер?"}):
            self.assertEqual(self.post(self.branch, {"message_id": message["id"]}).status_code, 404)
        self.assertEqual(self.repository.workspace(self.owner).inventory(), [])
        self.client.post("/api/logout", json={}, headers={"Origin": "http://localhost",
            "X-CSRF-Token": self.csrf})
        self.csrf = self.client.get("/api/csrf").json["csrf_token"]
        self.login("other")
        self.assertEqual(self.post(self.branch, {"message_id": message["id"]}).status_code, 404)
        self.assertEqual(self.repository.workspace(self.other).inventory(), [])

    def test_csrf_origin_query_and_body_are_rejected_before_write(self):
        message = self.accepted()
        endpoint = f"/api/branches/{self.branch}/materials"
        self.assertEqual(self.post(self.branch, {"message_id": message["id"]}).status_code, 401)
        self.login()
        self.assertEqual(self.post(self.branch, {"message_id": message["id"]}, csrf="bad").status_code, 403)
        self.assertEqual(self.post(self.branch, {"message_id": message["id"]}, origin="https://evil.test").status_code, 403)
        for data in ({}, {"message_id": message["id"], "content": "forged"},
                     {"message_id": message["id"], "owner": self.other},
                     {"message_id": 5}, ["message_id"]):
            self.assertEqual(self.post(self.branch, data).status_code, 400)
        self.assertEqual(self.client.post(endpoint + "?path=outputs/x.md",
            json={"message_id": message["id"]}, headers={"Origin": "http://localhost",
            "X-CSRF-Token": self.csrf}).status_code, 400)
        self.assertEqual(self.repository.workspace(self.owner).inventory(), [])

    def test_size_and_count_limits_do_not_create_a_partial_file(self):
        message = self.accepted(answer="я" * 8200)
        self.login()
        self.assertEqual(self.post(self.branch, {"message_id": message["id"]}).status_code, 413)
        self.assertEqual(self.repository.workspace(self.owner).inventory(), [])
        for number in range(24):
            self.repository.seed(self.owner, {f"notes/n{number}.md": "filled"})
        self.assertEqual(len(self.repository.workspace(self.owner).inventory()), 24)
        short = self.accepted(question="Теперь коротко", answer="Коротко")
        self.assertEqual(self.post(self.branch, {"message_id": short["id"]}).status_code, 409)
        self.assertEqual(len(self.repository.workspace(self.owner).inventory()), 24)

    def test_download_rejects_unsafe_paths_and_other_owner(self):
        message = self.accepted()
        self.login()
        saved = self.post(self.branch, {"message_id": message["id"]}).json
        path = saved["path"]
        for query in ("", "?path=../secret", "?path=outputs/../x.md",
                      "?path=outputs/x.txt", "?path=outputs/x.md&path=outputs/x.md",
                      "?path=outputs/x.md&owner=1"):
            self.assertIn(self.client.get("/api/materials/download" + query).status_code, (400, 404))
        self.client.post("/api/logout", json={}, headers={"Origin": "http://localhost",
            "X-CSRF-Token": self.csrf})
        self.csrf = self.client.get("/api/csrf").json["csrf_token"]
        self.assertEqual(self.client.get(saved["download_url"]).status_code, 401)
        self.login("other")
        self.assertEqual(self.client.get(saved["download_url"]).status_code, 404)

    def test_forked_accepted_reply_can_be_saved_with_original_provenance(self):
        message = self.accepted(question="Почему карта нормалей не меняет силуэт?",
                                answer="Она влияет на освещение, не на геометрию.")
        fork = self.store.fork_branch(self.owner, self.branch, message["id"])
        inherited = fork["messages"][-1]
        self.assertIsNone(inherited["turn_id"])
        self.login()
        saved = self.post(fork["id"], {"message_id": inherited["id"]})
        self.assertEqual(saved.status_code, 201)
        content = self.repository.workspace(self.owner).read(saved.json["path"])["content"]
        self.assertIn("Почему карта нормалей не меняет силуэт?", content)
        self.assertIn("Она влияет на освещение, не на геометрию.", content)

    def test_parallel_repeat_creates_one_file_without_partial_result(self):
        message = self.accepted()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: save_material(self.store, self.repository,
                self.owner, self.branch, message["id"]), range(2)))
        self.assertEqual(sorted(item["created"] for item in results), [False, True])
        self.assertEqual(results[0]["sha256"], results[1]["sha256"])
        self.assertEqual(len(self.repository.workspace(self.owner).inventory()), 1)

    def test_assets_load_after_existing_chat_and_files_scripts(self):
        html = self.client.get("/").data.decode("utf-8")
        self.assertLess(html.index("/static/app.js"), html.index("/learning-files/files.js"))
        self.assertLess(html.index("/learning-files/files.js"), html.index("/learning-product/materials.js"))
        for asset in ("materials.js", "materials.css"):
            response = self.client.get("/learning-product/" + asset)
            self.assertEqual(response.status_code, 200)
            response.close()

    def test_branch_marks_only_accepted_explanations_saveable(self):
        answer = self.accepted()
        control = self.store.begin_support_turn(self.owner, self.branch,
            "/support /status", "control-request")
        self.store.append_message(self.owner, self.branch, "user", "/support /status",
                                  turn_id=control["id"])
        self.store.move_turn(control["id"], "input_check")
        self.store.publish_support_control(self.owner, self.branch, control["id"],
                                           "Проверка выполнена")
        fork = self.store.fork_branch(self.owner, self.branch, answer["id"])
        inherited = fork["messages"][-1]
        self.login()

        response = self.client.get(f"/api/branches/{self.branch}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["id"], self.branch)
        self.assertIn("support_confirmations", response.json)
        self.assertIn("support_outcomes", response.json)
        by_id = {item["id"]: item for item in response.json["messages"]}
        self.assertTrue(by_id[answer["id"]].get("can_save_material", False))
        self.assertEqual(by_id[answer["id"]]["sources"], answer["sources"])
        self.assertEqual(by_id[answer["id"]]["turn_id"], answer["turn_id"])
        self.assertFalse(next(item for item in response.json["messages"]
                              if item["turn_id"] == control["id"] and item["role"] == "assistant")["can_save_material"])
        self.assertTrue(all(not item["can_save_material"] for item in response.json["messages"]
                            if item["role"] == "user"))

        inherited_response = self.client.get(f"/api/branches/{fork['id']}")
        self.assertEqual(inherited_response.status_code, 200)
        self.assertTrue(next(item for item in inherited_response.json["messages"]
                             if item["id"] == inherited["id"])["can_save_material"])
        self.client.post("/api/logout", json={}, headers={"Origin": "http://localhost",
            "X-CSRF-Token": self.csrf})
        self.csrf = self.client.get("/api/csrf").json["csrf_token"]
        self.login("other")
        self.assertEqual(self.client.get(f"/api/branches/{self.branch}").status_code, 404)

    def test_save_does_not_call_provider_and_keeps_source_fields(self):
        sources = [{"title": "A", "url": "https://a.test", "section": "One",
                    "version": "6.3", "pipeline": "URP", "text": "quoted fragment",
                    "chunk_id": "secret-internal-id", "score": 0.9, "sha256": "internal-hash"}]
        message = self.accepted(sources=sources)
        self.login()
        with patch("graphics.provider.GLMProvider.call", side_effect=AssertionError("provider called")):
            saved = self.post(self.branch, {"message_id": message["id"]})
        self.assertEqual(saved.status_code, 201)
        content = self.repository.workspace(self.owner).read(saved.json["path"])["content"]
        self.assertIn("A", content)
        self.assertIn("https://a.test", content)
        self.assertIn("One", content)
        self.assertIn("6.3", content)
        self.assertIn("URP", content)
        self.assertNotIn("secret-internal-id", content)
        self.assertNotIn("internal-hash", content)
        self.assertNotIn("quoted fragment", content)
        self.assertNotIn('"score"', content)

    def test_download_rejects_hardlink_when_supported(self):
        message = self.accepted()
        self.login()
        saved = self.post(self.branch, {"message_id": message["id"]}).json
        target = self.repository.workspace(self.owner)._checked_path(saved["path"])
        link = target.with_name("link.md")
        try:
            os.link(target, link)
        except (OSError, NotImplementedError):
            self.skipTest("hardlinks unavailable on this platform")
        self.assertEqual(self.client.get(saved["download_url"]).status_code, 404)
        link.unlink()

    def test_download_rejects_symlink_when_supported(self):
        message = self.accepted()
        self.login()
        saved = self.post(self.branch, {"message_id": message["id"]}).json
        target = self.repository.workspace(self.owner)._checked_path(saved["path"])
        target.unlink()
        try:
            os.symlink(Path(__file__), target)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable on this platform")
        self.assertEqual(self.client.get(saved["download_url"]).status_code, 404)
        self.assertEqual(self.post(self.branch, {"message_id": message["id"]}).status_code, 404)


if __name__ == "__main__":
    unittest.main()

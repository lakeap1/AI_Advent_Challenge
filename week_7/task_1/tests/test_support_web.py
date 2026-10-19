"""Public allowlists and authenticated support receipts."""
from pathlib import Path
import tempfile
import unittest

from graphics.config import Settings
from graphics.service import ChatService
from graphics.store import Store
from graphics.web import create_app, _public_result


ORIGIN = "https://grani.test"


class SupportWebTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        app_root = Path(__file__).resolve().parents[1]
        self.settings = Settings(app_root, root / "data", secret_key="support-test-secret",
                                 secure_cookies=False)
        self.store = Store(self.settings.db_path)
        self.store.create_user("alice", "alice-password-123")
        self.store.create_user("bob", "bob-password-123")
        self.app = create_app(self.settings, self.store, ChatService(self.settings, self.store))
        self.client = self.app.test_client()

    def _login(self, name):
        csrf = self.client.get("/api/csrf", base_url=ORIGIN).json["csrf_token"]
        response = self.client.post("/api/login", json={"username": name, "password": f"{name}-password-123"},
                                    headers={"Origin": ORIGIN, "X-CSRF-Token": csrf}, base_url=ORIGIN)
        self.assertEqual(response.status_code, 200)
        return response.json["csrf_token"]

    def _post(self, path, payload, csrf):
        return self.client.post(path, json=payload, headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
                                base_url=ORIGIN)

    def test_public_result_allowlists_only_safe_confirmation(self):
        public = _public_result({"status": "error", "message": "Проверка отклонена",
                                 "draft": "PRIVATE_DRAFT", "tool_result": "PRIVATE_TOOL",
                                 "support_confirmation": {"message": "Подтверждено", "receipt": {
                                     "operation_id": "a" * 32, "operation": "create", "ticket_id": "b" * 32,
                                     "request_key": "secret-key", "raw": "PRIVATE_RAW"}}})
        text = str(public)
        for private in ("PRIVATE_DRAFT", "PRIVATE_TOOL", "PRIVATE_RAW", "secret-key"):
            self.assertNotIn(private, text)
        self.assertEqual(public["support_confirmation"]["receipt"]["ticket_id"], "b" * 32)

    def test_control_receipt_survives_reload_and_other_account_cannot_read_it(self):
        csrf = self._login("alice")
        branch = self._post("/api/chats", {}, csrf).json["branch_id"]
        path = f"/api/branches/{branch}/ask"
        result = self._post(path, {"question": "/support /profile role=QA", "request_id": "same-id"}, csrf)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json["status"], "support_control")
        self.assertEqual(result.json["support_confirmation"]["receipt"]["operation"], "support_profile_update")
        first = self.client.get(f"/api/branches/{branch}", base_url=ORIGIN).json
        self.assertEqual(len(first["support_confirmations"]), 1)
        replay = self._post(path, {"question": "/support /profile role=QA", "request_id": "same-id"}, csrf)
        self.assertEqual(replay.json["support_confirmation"], result.json["support_confirmation"])
        self.assertEqual(len(self.client.get(f"/api/branches/{branch}", base_url=ORIGIN).json["messages"]), 2)
        self._post("/api/logout", {}, csrf)
        self._login("bob")
        foreign = self.client.get(f"/api/branches/{branch}", base_url=ORIGIN)
        self.assertEqual(foreign.status_code, 404)
        self.assertNotIn("QA", foreign.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()

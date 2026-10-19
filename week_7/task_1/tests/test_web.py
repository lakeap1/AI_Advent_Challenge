"""HTTP boundary tests with real SQLite state and an injected answer boundary."""
from pathlib import Path
from dataclasses import replace
import tempfile
import unittest

from graphics.config import Settings
from graphics.store import Store
from graphics.web import create_app


ORIGIN = "https://grani.test"


class AnswerBoundary:
    def __init__(self, store):
        self.store = store
        self.calls = 0
        self.compress_calls = 0
        self.result = {"status": "rejected", "message": "Ответ не прошёл проверку",
                       "draft": "PRIVATE_DRAFT_SENTINEL", "trace": "PRIVATE_TRACE_SENTINEL"}

    def ask(self, user_id, branch_id, text):
        self.calls += 1
        self.store.get_branch(user_id, branch_id)
        return self.result

    def compress(self, user_id, branch_id):
        self.compress_calls += 1
        self.store.get_branch(user_id, branch_id)
        return {"status": "compressed", "message": "Сводка обновлена"}


class WebTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.settings = Settings(Path(__file__).resolve().parents[1], root / "data", secret_key="fixture-session-secret",
                                 secure_cookies=False)
        self.store = Store(self.settings.db_path)
        self.owner = self.store.create_user("owner", "owner-password-123")
        self.other = self.store.create_user("other", "other-password-123")
        self.other_chat = self.store.create_chat(self.other)
        self.service = AnswerBoundary(self.store)
        self.app = create_app(self.settings, self.store, self.service)
        self.client = self.app.test_client()

    def request(self, method, path, data=None, *, csrf=None, origin=ORIGIN):
        headers = {"Origin": origin}
        if csrf:
            headers["X-CSRF-Token"] = csrf
        return self.client.open(path, method=method, json=data, headers=headers,
                                base_url=ORIGIN)

    def token(self):
        response = self.client.get("/api/csrf", base_url=ORIGIN)
        self.assertEqual(response.status_code, 200)
        return response.json["csrf_token"]

    def login(self, username="owner", password="owner-password-123"):
        response = self.request("POST", "/api/login",
                                {"username": username, "password": password}, csrf=self.token())
        self.assertEqual(response.status_code, 200)
        return response.json["csrf_token"]

    def test_private_api_requires_login_and_login_needs_origin_and_csrf(self):
        self.assertEqual(self.client.get("/api/bootstrap", base_url=ORIGIN).status_code, 401)
        self.assertEqual(self.request("POST", "/api/login", {"username": "owner",
                         "password": "owner-password-123"}).status_code, 403)
        token = self.token()
        self.assertEqual(self.request("POST", "/api/login", {"username": "owner",
                         "password": "owner-password-123"}, csrf=token,
                         origin="https://foreign.test").status_code, 403)
        self.assertEqual(self.request("POST", "/api/login", {"username": "owner",
                         "password": "owner-password-123"}, csrf=token).status_code, 200)

    def test_all_mutations_need_csrf_and_origin_after_login(self):
        csrf = self.login()
        self.assertEqual(self.request("POST", "/api/chats", {"title": "Тест"}).status_code, 403)
        self.assertEqual(self.request("POST", "/api/chats", {"title": "Тест"}, csrf=csrf,
                         origin="https://foreign.test").status_code, 403)
        self.assertEqual(self.request("POST", "/api/chats", {"title": "Тест"}, csrf=csrf).status_code, 201)

    def test_cross_owner_branch_message_chat_and_ledger_are_denied(self):
        csrf = self.login()
        branch = self.other_chat["branch_id"]
        message = self.store.append_message(self.other, branch, "user", "private")
        paths = [
            ("GET", f"/api/branches/{branch}", None),
            ("PATCH", f"/api/branches/{branch}", {"title": "x"}),
            ("POST", f"/api/branches/{branch}/ask", {"question": "x"}),
            ("POST", f"/api/branches/{branch}/compress", {}),
            ("POST", f"/api/branches/{branch}/fork", {"message_id": message["id"]}),
            ("GET", f"/api/ledger?chat_id={self.other_chat['id']}", None),
        ]
        for method, path, data in paths:
            with self.subTest(path=path):
                response = self.request(method, path, data, csrf=csrf)
                self.assertEqual(response.status_code, 404)
                self.assertNotIn("private", response.get_data(as_text=True))
        own_branch = self.request("POST", "/api/chats", {}, csrf=csrf).json["branch_id"]
        self.assertEqual(self.request("POST", f"/api/branches/{own_branch}/fork",
                         {"message_id": message["id"]}, csrf=csrf).status_code, 404)
        self.assertEqual(self.service.calls, 0)

    def test_rejected_draft_is_hidden_and_untrusted_text_stays_data(self):
        csrf = self.login()
        chat = self.request("POST", "/api/chats", {}, csrf=csrf).json
        response = self.request("POST", f"/api/branches/{chat['branch_id']}/ask",
                                {"question": "<img src=x onerror=alert(1)>"}, csrf=csrf)
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertNotIn("PRIVATE_DRAFT_SENTINEL", body)
        self.assertNotIn("PRIVATE_TRACE_SENTINEL", body)
        self.assertEqual(response.json["status"], "rejected")
        page = self.client.get("/", base_url=ORIGIN)
        self.assertEqual(page.status_code, 200)
        self.assertNotIn("<img", page.get_data(as_text=True))

    def test_login_rate_limit_survives_app_recreation(self):
        csrf = self.token()
        for _ in range(10):
            response = self.request("POST", "/api/login", {"username": "owner",
                                    "password": "wrong-password"}, csrf=csrf)
            self.assertEqual(response.status_code, 401)
        restarted = create_app(self.settings, Store(self.settings.db_path), AnswerBoundary(self.store))
        fresh = restarted.test_client()
        token = fresh.get("/api/csrf", base_url=ORIGIN).json["csrf_token"]
        blocked = fresh.post("/api/login", json={"username": "owner", "password": "owner-password-123"},
                             headers={"Origin": ORIGIN, "X-CSRF-Token": token}, base_url=ORIGIN)
        self.assertEqual(blocked.status_code, 429)

    def test_empty_secret_fails_closed_and_sources_are_allowlisted(self):
        settings = Settings(self.settings.app_root, self.settings.data_dir, secret_key="")
        with self.assertRaises(ValueError):
            create_app(settings, self.store, self.service)
        self.assertEqual(self.client.get("/reference/readme", base_url=ORIGIN).status_code, 401)
        self.login()
        self.assertEqual(self.client.get("/reference/../../graphics/store.py", base_url=ORIGIN).status_code, 404)
        self.assertEqual(self.client.get("/reference/not-allowed", base_url=ORIGIN).status_code, 404)

    def test_forked_branches_remain_navigable_after_reload(self):
        csrf = self.login()
        chat = self.request("POST", "/api/chats", {}, csrf=csrf).json
        message = self.store.append_message(self.owner, chat["branch_id"], "user", "anchor")
        fork = self.request("POST", f"/api/branches/{chat['branch_id']}/fork",
                            {"message_id": message["id"]}, csrf=csrf).json
        restarted = create_app(self.settings, Store(self.settings.db_path), AnswerBoundary(self.store))
        fresh = restarted.test_client()
        fresh.set_cookie("session", self.client.get_cookie("session", domain="grani.test").value,
                         domain="grani.test")
        response = fresh.get(f"/api/chats/{chat['id']}/branches", base_url=ORIGIN)
        self.assertEqual(response.status_code, 200)
        self.assertEqual({b["id"] for b in response.json["branches"]},
                         {chat["branch_id"], fork["id"]})

    def test_branch_response_excludes_internal_snapshots_and_reference_is_text(self):
        csrf = self.login()
        chat = self.request("POST", "/api/chats", {}, csrf=csrf).json
        self.store.append_message(self.owner, chat["branch_id"], "user", "hello")
        branch = self.client.get(f"/api/branches/{chat['branch_id']}", base_url=ORIGIN)
        self.assertEqual(branch.status_code, 200)
        self.assertNotIn("snapshot", branch.get_data(as_text=True))
        for source in ("readme", "usage", "architecture", "api", "glossary"):
            with self.subTest(source=source):
                response = self.client.get(f"/reference/{source}", base_url=ORIGIN)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.content_type.startswith("text/plain"))

    def test_question_size_matches_composer_and_cookie_flags(self):
        csrf = self.login()
        chat = self.request("POST", "/api/chats", {}, csrf=csrf).json
        response = self.request("POST", f"/api/branches/{chat['branch_id']}/ask",
                                {"question": "a" * 3000}, csrf=csrf)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.service.calls, 1)
        production = Settings(self.settings.app_root, self.settings.data_dir,
                              secret_key="fixture-session-secret", secure_cookies=True)
        app = create_app(production, self.store, self.service)
        cookie = app.test_client().get("/api/csrf", base_url=ORIGIN).headers["Set-Cookie"]
        for attribute in ("Secure", "HttpOnly", "SameSite=Strict"):
            self.assertIn(attribute, cookie)

    def test_ledger_exposes_accounting_without_internal_metadata(self):
        csrf = self.login()
        chat = self.request("POST", "/api/chats", {}, csrf=csrf).json
        turn = self.store.begin_turn(self.owner, chat["branch_id"], "question")["id"]
        self.store.move_turn(turn, "input_check")
        self.store.move_turn(turn, "context")
        call = self.store.start_call(self.owner, chat["branch_id"], turn,
                                     "preparation", "fixture", {"trace": "PRIVATE_TRACE_SENTINEL",
                                                                   "request_sha256": "PRIVATE_HASH_SENTINEL",
                                                                   "tariff_source": "Fixture tariff",
                                                                   "tariff_checked_on": "2026-10-19"})
        self.store.finish_call(call, None, 0.0, status="not_sent",
                               metadata={"api_request_sent": False})
        response = self.client.get(f"/api/ledger?chat_id={chat['id']}", base_url=ORIGIN)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("PRIVATE_TRACE_SENTINEL", response.get_data(as_text=True))
        self.assertNotIn("PRIVATE_HASH_SENTINEL", response.get_data(as_text=True))
        self.assertNotIn("metadata", response.get_data(as_text=True))
        self.assertEqual(response.json["calls"][0]["stage"], "preparation")
        self.assertEqual(response.json["calls"][0].get("tariff_source"), "Fixture tariff")
        self.assertEqual(response.json["calls"][0].get("tariff_checked_on"), "2026-10-19")

    def test_trusted_loopback_proxy_rate_limit_uses_real_client_ip(self):
        csrf = self.token()
        def login_from(ip, password):
            return self.client.post("/api/login", json={"username": "owner", "password": password},
                                    headers={"Origin": ORIGIN, "X-CSRF-Token": csrf,
                                             "X-Real-IP": ip}, base_url=ORIGIN)
        for _ in range(10):
            self.assertEqual(login_from("203.0.113.8", "wrong").status_code, 401)
        self.assertEqual(login_from("203.0.113.8", "owner-password-123").status_code, 429)
        self.assertEqual(login_from("203.0.113.9", "owner-password-123").status_code, 200)

    def test_compression_has_persistent_paid_action_rate_limit(self):
        csrf = self.login()
        chat = self.request("POST", "/api/chats", {}, csrf=csrf).json
        path = f"/api/branches/{chat['branch_id']}/compress"
        for _ in range(20):
            self.assertEqual(self.request("POST", path, {}, csrf=csrf).status_code, 200)
        self.assertEqual(self.request("POST", path, {}, csrf=csrf).status_code, 429)
        self.assertEqual(self.service.compress_calls, 20)

    def test_logout_revokes_replay_of_previous_signed_cookie(self):
        csrf = self.login()
        stolen = self.client.get_cookie("session", domain="grani.test").value
        self.assertEqual(self.request("POST", "/api/logout", {}, csrf=csrf).status_code, 200)
        replay = self.app.test_client()
        replay.set_cookie("session", stolen, domain="grani.test")
        self.assertEqual(replay.get("/api/bootstrap", base_url=ORIGIN).status_code, 401)

    def test_explicit_budget_refusal_is_actionable_and_keeps_http_data(self):
        csrf = self.login()
        chat = self.request("POST", "/api/chats", {}, csrf=csrf).json
        branch = chat["branch_id"]
        refused = self.request("POST", "/api/memories", {"key": "подача", "value": "Я" * 2000}, csrf=csrf)
        self.assertEqual(refused.status_code, 400)
        self.assertIn("4000", refused.json["error"])
        self.assertIn("Сократите", refused.json["error"])
        self.assertEqual(self.store.list_memories(self.owner), [])
        for index in range(3):
            self.assertEqual(self.request("POST", "/api/memories", {"key": f"k{index}", "value": "x" * 950}, csrf=csrf).status_code, 201)
        before = self.store.list_memories(self.owner)
        self.assertEqual(self.request("POST", "/api/memories", {"key": "k0", "value": "Я" * 1000}, csrf=csrf).status_code, 400)
        self.assertEqual(self.store.list_memories(self.owner), before)
        self.assertEqual(self.request("PATCH", f"/api/branches/{branch}",
                                     {"conditions": {"unity": "Я" * 600}}, csrf=csrf).status_code, 400)
        self.assertEqual(self.store.get_branch(self.owner, branch)["conditions"], {})
        self.assertEqual(self.service.calls, 0)

    def test_injected_store_uses_custom_settings_explicit_budget(self):
        self.settings = replace(self.settings, explicit_budget=300)
        self.app = create_app(self.settings, self.store, self.service)
        self.client = self.app.test_client()
        csrf = self.login()
        self.assertEqual(self.request("POST", "/api/memories", {"key": "short", "value": "x" * 100}, csrf=csrf).status_code, 201)
        refused = self.request("PATCH", "/api/profile", {"goal": "x" * 100}, csrf=csrf)
        self.assertEqual(refused.status_code, 400)
        self.assertIn("300", refused.json["error"])
        self.assertEqual(self.store.get_profile(self.owner), {})

    def test_authenticated_reads_and_mutations_do_not_refresh_old_session_cookie(self):
        anonymous = self.client.get('/api/csrf', base_url=ORIGIN)
        self.assertIn('Set-Cookie', anonymous.headers)
        login = self.request('POST', '/api/login', {'username': 'owner', 'password': 'owner-password-123'},
                             csrf=anonymous.json['csrf_token'])
        self.assertEqual(login.status_code, 200)
        self.assertIn('Set-Cookie', login.headers)
        csrf = login.json['csrf_token']
        bootstrap = self.client.get('/api/bootstrap', base_url=ORIGIN)
        self.assertEqual(bootstrap.status_code, 200)
        self.assertFalse('Set-Cookie' in bootstrap.headers, 'authenticated bootstrap refreshed the cookie')
        for path, data in (('/api/profile', {'role': 'QA'}),
                           ('/api/memories', {'key': 'example', 'value': 'basalt'})):
            response = self.request('PATCH' if path.endswith('profile') else 'POST', path, data, csrf=csrf)
            self.assertIn(response.status_code, (200, 201))
            self.assertFalse('Set-Cookie' in response.headers, 'ordinary mutation refreshed the cookie')
        logout = self.request('POST', '/api/logout', {}, csrf=csrf)
        self.assertEqual(logout.status_code, 200)
        self.assertIn('Set-Cookie', logout.headers)
        anonymous_again = self.client.get('/api/csrf', base_url=ORIGIN)
        self.assertIn('Set-Cookie', anonymous_again.headers)
        self.assertTrue(anonymous_again.json['csrf_token'])


if __name__ == "__main__":
    unittest.main()

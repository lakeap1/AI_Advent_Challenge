"""Private HTTP boundary. Browser input never selects a file, tool, or model."""

import hmac
import hashlib
import ipaddress
import os
from pathlib import Path
import secrets
import sqlite3
import time
from contextlib import closing
from datetime import timedelta

from flask import Flask, jsonify, render_template, request, session, Response

from .config import Settings
from .context import ExplicitBudgetError
from .service import ChatService
from .store import Store


_SOURCE_FILES = {
    "readme": "README.md", "usage": "docs/usage.md",
    "architecture": "docs/architecture.md", "api": "docs/api.md",
    "glossary": "corpus/glossary.md",
}
_PROFILE_FIELDS = {"role", "level", "goal", "depth"}
_BRANCH_FIELDS = {"title", "conditions"}


class RateLimiter:
    """Small, process independent fixed windows in a dedicated SQLite file."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db:
            with db:
                db.execute("CREATE TABLE IF NOT EXISTS limits (key TEXT NOT NULL, window INTEGER NOT NULL, "
                           "count INTEGER NOT NULL, PRIMARY KEY(key,window))")
                db.execute("CREATE TABLE IF NOT EXISTS sessions (id_hash TEXT PRIMARY KEY, "
                           "user_id INTEGER NOT NULL, expires_at INTEGER NOT NULL)")

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA busy_timeout=10000")
        return db

    def take(self, key, *, limit, seconds):
        now = int(time.time())
        window = now // seconds
        with closing(self.connect()) as db:
            with db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("DELETE FROM limits WHERE window<? AND key=?", (window - 2, key))
                row = db.execute("SELECT count FROM limits WHERE key=? AND window=?", (key, window)).fetchone()
                if row and row[0] >= limit:
                    return False
                db.execute("INSERT INTO limits(key,window,count) VALUES(?,?,1) "
                           "ON CONFLICT(key,window) DO UPDATE SET count=count+1", (key, window))
        return True

    @staticmethod
    def _session_hash(token):
        return hashlib.sha256(token.encode("ascii")).hexdigest()

    def new_session(self, user_id):
        token = secrets.token_urlsafe(32)
        expires_at = int(time.time()) + 12 * 3600
        with closing(self.connect()) as db:
            with db:
                db.execute("DELETE FROM sessions WHERE expires_at<=?", (int(time.time()),))
                db.execute("INSERT INTO sessions(id_hash,user_id,expires_at) VALUES(?,?,?)",
                           (self._session_hash(token), user_id, expires_at))
        return token

    def session_user(self, token):
        if not isinstance(token, str) or len(token) > 128 or not token.isascii():
            return None
        with closing(self.connect()) as db:
            row = db.execute("SELECT user_id FROM sessions WHERE id_hash=? AND expires_at>?",
                             (self._session_hash(token), int(time.time()))).fetchone()
        return row[0] if row else None

    def revoke_session(self, token):
        if not isinstance(token, str) or not token.isascii():
            return
        with closing(self.connect()) as db:
            with db:
                db.execute("DELETE FROM sessions WHERE id_hash=?", (self._session_hash(token),))


def _public_result(result):
    """Explicit allowlist prevents provider drafts and receipts reaching JSON."""
    status = result.get("status", "error")
    if status not in {"accepted", "compressed", "updated", "needs_clarification",
                      "input_rejected", "rejected", "error"}:
        status = "error"
    public = {"status": status}
    if status == "accepted":
        public["answer"] = result.get("answer", "")
        public["sources"] = result.get("sources", [])
    else:
        public["message"] = result.get("message") or "Не удалось завершить запрос"
    if isinstance(result.get("usage"), dict):
        public["usage"] = {k: result["usage"].get(k) for k in
                           ("complete", "known_cost_estimated_usd", "cost_estimated_usd",
                            "input_tokens", "output_tokens", "total_tokens")}
    return public


def _public_branch(branch):
    public = {key: branch[key] for key in ("id", "chat_id", "title", "summary",
              "summary_through", "conditions", "revision")}
    public["messages"] = [{key: message[key] for key in ("id", "role", "content", "sources")}
                          for message in branch["messages"]]
    return public


def _public_ledger(ledger):
    public = {key: ledger[key] for key in ("complete", "known_cost_estimated_usd",
              "cost_estimated_usd", "input_tokens", "output_tokens", "total_tokens")}
    public["calls"] = []
    for call in ledger["calls"]:
        raw_usage = call.get("usage")
        usage = None if raw_usage is None else {
            key: raw_usage.get(key) if type(raw_usage.get(key)) is int and raw_usage[key] >= 0 else None
            for key in ("input_tokens", "output_tokens", "total_tokens",
                        "cached_input_tokens", "reasoning_tokens")
        }
        metadata = call.get("metadata") or {}
        tariff_source = metadata.get("tariff_source")
        tariff_checked_on = metadata.get("tariff_checked_on")
        public["calls"].append({
            "stage": call["stage"], "model": call["model"], "status": call["status"],
            "usage": usage, "cost_estimated_usd": call["cost_estimated_usd"],
            "tariff_source": tariff_source if isinstance(tariff_source, str) and len(tariff_source) <= 200 else None,
            "tariff_checked_on": tariff_checked_on if isinstance(tariff_checked_on, str) and len(tariff_checked_on) <= 32 else None,
        })
    return public


def create_app(settings=None, store=None, service=None):
    settings = settings or Settings.from_env()
    if not settings.secret_key or len(settings.secret_key) < 12:
        raise ValueError("GRAPHICS_SECRET_KEY отсутствует или слишком короткий")
    store = store or Store(settings.db_path, explicit_budget=settings.explicit_budget)
    store.explicit_budget = settings.explicit_budget
    service = service or ChatService(settings, store)
    limiter = RateLimiter(settings.data_dir / "auth_limits.sqlite3")
    app = Flask(__name__, template_folder=str(settings.app_root / "templates"),
                static_folder=str(settings.app_root / "static"))
    app.secret_key = settings.secret_key
    app.config.update(MAX_CONTENT_LENGTH=48 * 1024, SESSION_COOKIE_SECURE=settings.secure_cookies,
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict",
                      SESSION_REFRESH_EACH_REQUEST=False,
                      PERMANENT_SESSION_LIFETIME=timedelta(hours=12))

    def csrf_token():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        return session["csrf"]

    def current_user():
        return limiter.session_user(session.get("sid"))

    def problem(code, message):
        return jsonify({"error": message}), code

    def body():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ValueError("Ожидается объект JSON")
        return data

    def fields(data, allowed):
        if not set(data) <= allowed:
            raise ValueError("Недопустимое поле")
        for value in data.values():
            if isinstance(value, str) and len(value) > 8000:
                raise ValueError("Слишком длинное значение")
        return data

    def owned_branch(branch_id):
        return store.get_branch(current_user(), branch_id)

    def client_ip():
        peer = request.remote_addr or "unknown"
        if peer in {"127.0.0.1", "::1"}:
            forwarded = request.headers.get("X-Real-IP", "")
            try:
                return str(ipaddress.ip_address(forwarded))
            except ValueError:
                pass
        return peer

    def paid_action_allowed():
        return (limiter.take(f"paid-user:{current_user()}", limit=20, seconds=60)
                and limiter.take(f"paid-ip:{client_ip()}", limit=60, seconds=60))

    @app.before_request
    def guard():
        if request.path.startswith("/api/") and request.path not in {"/api/csrf", "/api/login"}:
            if current_user() is None:
                return problem(401, "Требуется вход")
        if request.path.startswith("/reference/") and current_user() is None:
            return problem(401, "Требуется вход")
        if request.method in {"POST", "PATCH", "PUT", "DELETE"}:
            supplied = request.headers.get("Origin", "")
            expected = os.environ.get("GRAPHICS_PUBLIC_ORIGIN", "").rstrip("/")
            if not expected:
                scheme = request.scheme
                if request.remote_addr in {"127.0.0.1", "::1"}:
                    forwarded = request.headers.get("X-Forwarded-Proto", "")
                    if forwarded in {"https", "http"}:
                        scheme = forwarded
                expected = f"{scheme}://{request.host}"
            if supplied != expected or not hmac.compare_digest(
                    request.headers.get("X-CSRF-Token", ""), csrf_token()):
                return problem(403, "Запрос отклонён")
            if request.path.startswith("/api/") and not request.is_json:
                return problem(415, "Ожидается JSON")

    @app.after_request
    def headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; font-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        return response

    @app.errorhandler(PermissionError)
    def denied(_error):
        return problem(404, "Не найдено")

    @app.errorhandler(ValueError)
    def bad_input(_error):
        return problem(400, "Некорректный запрос")

    @app.errorhandler(ExplicitBudgetError)
    def explicit_budget_error(error):
        return problem(400, str(error))

    @app.errorhandler(RuntimeError)
    def busy(_error):
        return problem(409, "Операция пока недоступна")

    @app.errorhandler(413)
    def too_large(_error):
        return problem(413, "Запрос слишком большой")

    @app.get("/healthz")
    def health():
        return jsonify({"ok": True})

    @app.get("/")
    def index():
        csrf_token()
        return render_template("index.html")

    @app.get("/api/csrf")
    def csrf():
        return jsonify({"csrf_token": csrf_token()})

    @app.post("/api/login")
    def login():
        data = fields(body(), {"username", "password"})
        username, password = data.get("username"), data.get("password")
        if (not isinstance(username, str) or not isinstance(password, str)
                or len(username) > 80 or len(password) > 256):
            raise ValueError("Некорректные данные входа")
        ip = client_ip()
        if not limiter.take(f"login:{ip}", limit=10, seconds=900):
            return problem(429, "Слишком много попыток. Повторите позже")
        user = store.authenticate(username.strip(), password)
        if user is None:
            return problem(401, "Неверное имя или пароль")
        session.clear()
        session["sid"] = limiter.new_session(user)
        session.permanent = True
        return jsonify({"csrf_token": csrf_token()})

    @app.post("/api/logout")
    def logout():
        limiter.revoke_session(session.get("sid"))
        session.clear()
        return jsonify({"ok": True})

    @app.get("/api/bootstrap")
    def bootstrap():
        user = current_user()
        return jsonify({"csrf_token": csrf_token(), "profile": store.get_profile(user),
                        "memories": store.list_memories(user), "chats": store.list_chats(user)})

    @app.get("/api/chats")
    def chats():
        return jsonify({"chats": store.list_chats(current_user())})

    @app.post("/api/chats")
    def new_chat():
        data = fields(body(), {"title"})
        if "title" in data and (not isinstance(data["title"], str) or len(data["title"]) > 120):
            raise ValueError("Некорректное название")
        return jsonify(store.create_chat(current_user(), data.get("title", "Новый разговор"))), 201

    @app.get("/api/chats/<chat_id>/branches")
    def chat_branches(chat_id):
        with store._connect() as db:
            if db.execute("SELECT 1 FROM chats WHERE id=? AND user_id=?",
                          (chat_id, current_user())).fetchone() is None:
                raise PermissionError("Доступ запрещён")
            rows = db.execute("SELECT id,title,revision FROM branches WHERE chat_id=? AND user_id=? "
                              "ORDER BY rowid", (chat_id, current_user())).fetchall()
        return jsonify({"branches": [dict(row) for row in rows]})

    @app.get("/api/branches/<branch_id>")
    def branch(branch_id):
        return jsonify(_public_branch(owned_branch(branch_id)))

    @app.patch("/api/branches/<branch_id>")
    def edit_branch(branch_id):
        owned_branch(branch_id)
        data = fields(body(), _BRANCH_FIELDS)
        if "title" in data and (not isinstance(data["title"], str) or len(data["title"]) > 120):
            raise ValueError("Некорректное название")
        if "conditions" in data:
            conditions = data["conditions"]
            if not isinstance(conditions, dict) or len(conditions) > 30 or any(
                not isinstance(k, str) or not isinstance(v, str) or len(k) > 80 or len(v) > 1000
                for k, v in conditions.items()
            ):
                raise ValueError("Некорректные условия")
        return jsonify(_public_branch(store.update_branch(current_user(), branch_id, data)))

    @app.post("/api/branches/<branch_id>/fork")
    def fork(branch_id):
        owned_branch(branch_id)
        data = fields(body(), {"message_id"})
        if not isinstance(data.get("message_id"), str):
            raise ValueError("Нужна реплика")
        return jsonify(_public_branch(store.fork_branch(current_user(), branch_id,
                                                      data["message_id"]))), 201

    @app.post("/api/branches/<branch_id>/ask")
    def ask(branch_id):
        owned_branch(branch_id)
        data = fields(body(), {"question"})
        question = data.get("question")
        if not isinstance(question, str) or not question.strip() or len(question) > 8000:
            raise ValueError("Вопрос пустой или слишком длинный")
        if not paid_action_allowed():
            return problem(429, "Слишком много запросов. Повторите позже")
        return jsonify(_public_result(service.ask(current_user(), branch_id, question)))

    @app.post("/api/branches/<branch_id>/compress")
    def compress(branch_id):
        owned_branch(branch_id)
        if body():
            raise ValueError("Параметры не поддерживаются")
        if not paid_action_allowed():
            return problem(429, "Слишком много запросов. Повторите позже")
        return jsonify(_public_result(service.compress(current_user(), branch_id)))

    @app.patch("/api/profile")
    def profile():
        data = fields(body(), _PROFILE_FIELDS)
        if any(not isinstance(v, str) or len(v) > 500 for v in data.values()):
            raise ValueError("Некорректное значение профиля")
        return jsonify({"profile": store.update_profile(current_user(), data)})

    @app.post("/api/memories")
    def remember():
        data = fields(body(), {"key", "value"})
        if (not isinstance(data.get("key"), str) or not isinstance(data.get("value"), str)
                or len(data["key"]) > 80 or len(data["value"]) > 2000):
            raise ValueError("Некорректная память")
        return jsonify(store.remember(current_user(), data["key"], data["value"])), 201

    @app.delete("/api/memories/<memory_id>")
    def forget(memory_id):
        store.forget(current_user(), memory_id)
        return jsonify({"ok": True})

    @app.get("/api/ledger")
    def ledger():
        chat_id = request.args.get("chat_id")
        return jsonify(_public_ledger(store.ledger(current_user(), chat_id)))

    @app.get("/reference/<source_id>")
    def reference(source_id):
        if source_id not in _SOURCE_FILES:
            return problem(404, "Не найдено")
        path = settings.app_root / _SOURCE_FILES[source_id]
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            return problem(404, "Не найдено")
        return Response(content, content_type="text/plain; charset=utf-8")

    return app

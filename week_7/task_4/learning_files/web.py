"""Private learning-file routes attached to the existing educational site."""

from pathlib import Path
import ipaddress

from flask import jsonify, request, send_from_directory, session

from graphics.config import Settings
from graphics.service import ChatService
from graphics.store import Store
from graphics.web import RateLimiter, create_app as create_chat_app

from .agent import FileAgent
from .repository import FileRepository


_STATIC = Path(__file__).resolve().parent / "static"
_EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
_SEED = {
    "materials.md": "sources/materials.md",
    "shader-pipeline.md": "sources/shader-pipeline.md",
    "textures.md": "sources/textures.md",
    "normal-maps.md": "sources/normal-maps.md",
    "normal-theory.md": "notes/normal-theory.md",
    "normal-unity.md": "notes/normal-unity.md",
}
_CSS = '<link rel="stylesheet" href="/learning-files/files.css">'
_JS = '<script src="/learning-files/files.js" defer></script>'


def create_app(settings=None, store=None, service=None, repository=None):
    settings = settings or Settings.from_env(base_dir=Path(__file__).resolve().parents[2] / "task_1")
    store = store or Store(settings.db_path, explicit_budget=settings.explicit_budget)
    service = service or ChatService(settings, store)
    repository = repository or FileRepository(settings.data_dir / "learning-files")
    agent = FileAgent(settings, store, repository, service)
    limiter = RateLimiter(settings.data_dir / "auth_limits.sqlite3")
    app = create_chat_app(settings, store, service)

    def user():
        value = limiter.session_user(session.get("sid"))
        if value is None:
            raise PermissionError("session missing")
        return value

    def body(required):
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or set(data) != required:
            raise ValueError("invalid body")
        return data

    def run_for_owner(run_id):
        try:
            return repository.finalize(user(), run_id, store)
        except (PermissionError, ValueError):
            raise PermissionError("run unavailable") from None

    def client_ip():
        peer = request.remote_addr or "unknown"
        if peer in {"127.0.0.1", "::1"}:
            forwarded = request.headers.get("X-Real-IP", "")
            try:
                return str(ipaddress.ip_address(forwarded))
            except ValueError:
                pass
        return peer

    @app.after_request
    def attach_assets(response):
        if request.path == "/" and response.status_code == 200 and response.mimetype == "text/html":
            html = response.get_data(as_text=True)
            if "</head>" in html:
                response.set_data(html.replace("</head>", _CSS + "\n  " + _JS + "\n</head>", 1))
        return response

    @app.get("/learning-files/files.js")
    def files_js():
        return send_from_directory(_STATIC, "files.js", mimetype="text/javascript")

    @app.get("/learning-files/files.css")
    def files_css():
        return send_from_directory(_STATIC, "files.css", mimetype="text/css")

    @app.get("/api/learning-files/inventory")
    def inventory():
        if request.args:
            raise ValueError("invalid query")
        return jsonify({"files": repository.workspace(user()).inventory()})

    @app.get("/api/learning-files/file")
    def file_content():
        if set(request.args) != {"path"} or len(request.args.getlist("path")) != 1:
            raise ValueError("invalid query")
        try:
            return jsonify(repository.workspace(user()).read(request.args["path"]))
        except FileNotFoundError:
            raise PermissionError("document unavailable") from None

    @app.get("/api/learning-files/runs/<run_id>")
    def run_detail(run_id):
        if request.args:
            raise ValueError("invalid query")
        return jsonify(run_for_owner(run_id))

    @app.get("/api/learning-files/recovery")
    def recovery():
        if set(request.args) not in ({"branch_id"}, {"branch_id", "request_id"}) or any(
            len(request.args.getlist(key)) != 1 for key in request.args
        ):
            raise ValueError("invalid query")
        owner, branch = user(), request.args["branch_id"]
        if not isinstance(branch, str) or not branch or len(branch) > 128:
            raise ValueError("invalid branch")
        store.get_branch(owner, branch)
        request_id = request.args.get("request_id")
        if request_id is not None and (not request_id or len(request_id) > 128):
            raise ValueError("invalid request")
        run = (repository.find_request(owner, request_id) if request_id is not None
               else repository.latest(owner, branch))
        if run is not None and run["branch_id"] != branch:
            run = None
        return jsonify({"run": repository.finalize(owner, run["id"], store) if run else None})

    @app.post("/api/learning-files/examples")
    def seed_examples():
        body(set())
        files = {target: (_EXAMPLES / name).read_text(encoding="utf-8")
                 for name, target in _SEED.items()}
        return jsonify({"added": repository.seed(user(), files),
                        "exercise_notice": "Примеры подготовлены для упражнения; в заметках normal map есть намеренное противоречие."})

    @app.post("/api/learning-files/prepare")
    def prepare():
        data = body({"branch_id", "goal", "request_id"})
        branch, goal, request_id = data["branch_id"], data["goal"], data["request_id"]
        if (not isinstance(branch, str) or not branch or len(branch) > 128
                or not isinstance(goal, str) or not goal.strip() or len(goal) > 4000
                or not isinstance(request_id, str) or not request_id or len(request_id) > 128):
            raise ValueError("invalid goal")
        owner = user()
        store.get_branch(owner, branch)
        existing = repository.find_request(owner, request_id)
        if existing is not None:
            if existing["branch_id"] != branch or existing["goal"] != goal:
                raise ValueError("request reused")
            return jsonify(repository.finalize(owner, existing["id"], store))
        if not limiter.take(f"paid-user:{owner}", limit=20, seconds=60):
            return jsonify({"error": "Слишком много запросов. Повторите позже"}), 429
        if not limiter.take(f"paid-ip:{client_ip()}", limit=60, seconds=60):
            return jsonify({"error": "Слишком много запросов. Повторите позже"}), 429
        return jsonify(agent.prepare(owner, branch, goal, request_id))

    @app.post("/api/learning-files/runs/<run_id>/apply")
    def apply(run_id):
        data = body({"branch_id"})
        if not isinstance(data["branch_id"], str):
            raise ValueError("invalid branch")
        owner = user()
        run = run_for_owner(run_id)
        if run["branch_id"] != data["branch_id"]:
            raise PermissionError("wrong branch")
        store.get_branch(owner, run["branch_id"])
        return jsonify(repository.apply(owner, run_id))

    return app

"""Compose the learning site with the existing private files app."""

from pathlib import Path

from flask import jsonify, request, send_from_directory, session, Response

from graphics.web import RateLimiter
from learning_files.web import create_app as create_files_app

from .materials import MaterialConflict, MaterialLimit, can_save_material, save_material


_STATIC = Path(__file__).resolve().parent / "static"
_JS = '<script src="/learning-product/materials.js" defer></script>'
_CSS = '<link rel="stylesheet" href="/learning-product/materials.css">'


def create_app(settings=None, store=None, service=None, repository=None):
    if settings is None:
        from graphics.config import Settings
        settings = Settings.from_env(base_dir=Path(__file__).resolve().parents[2] / "task_1")
    if store is None:
        from graphics.store import Store
        store = Store(settings.db_path, explicit_budget=settings.explicit_budget)
    if repository is None:
        from learning_files.repository import FileRepository
        repository = FileRepository(settings.data_dir / "learning-files")
    app = create_files_app(settings, store, service, repository)
    limiter = RateLimiter(settings.data_dir / "auth_limits.sqlite3")

    def owner():
        value = limiter.session_user(session.get("sid"))
        if value is None:
            raise PermissionError("session missing")
        return value

    # Day31 owns this route. Decorate its public projection in the composed
    # app, preserving every existing field and the original ownership check.
    branch_rule = next(rule for rule in app.url_map.iter_rules()
                       if rule.rule == "/api/branches/<branch_id>" and "GET" in rule.methods)
    original_branch = app.view_functions[branch_rule.endpoint]

    def branch_with_materials(branch_id):
        response = original_branch(branch_id)
        public = response.get_json()
        actor = owner()
        for index, message in enumerate(public["messages"]):
            message["can_save_material"] = can_save_material(store, actor, public, index)
        response.set_data(app.json.dumps(public))
        return response

    app.view_functions[branch_rule.endpoint] = branch_with_materials

    @app.errorhandler(MaterialConflict)
    def conflict(error):
        return jsonify({"error": str(error)}), 409

    @app.errorhandler(MaterialLimit)
    def limit(error):
        return jsonify({"error": str(error)}), 409

    @app.post("/api/branches/<branch_id>/materials")
    def save(branch_id):
        if request.args:
            raise ValueError("invalid query")
        data = request.get_json(silent=True)
        if (not isinstance(data, dict) or set(data) != {"message_id"}
                or not isinstance(data["message_id"], str) or not data["message_id"]
                or len(data["message_id"]) > 128):
            raise ValueError("invalid body")
        try:
            saved = save_material(store, repository, owner(), branch_id, data["message_id"])
        except ValueError as error:
            if str(error) == "document too large":
                return jsonify({"error": "Ответ слишком длинный для файла (максимум 16384 байта UTF-8)"}), 413
            raise
        saved["download_url"] = "/api/materials/download?path=" + saved["path"]
        return jsonify(saved), 201 if saved["created"] else 200

    @app.get("/api/materials/download")
    def download():
        if set(request.args) != {"path"} or len(request.args.getlist("path")) != 1:
            raise ValueError("invalid query")
        path = request.args["path"]
        try:
            snapshot = repository.workspace(owner()).read(path)
        except FileNotFoundError:
            raise PermissionError("document unavailable") from None
        name = path.rsplit("/", 1)[1]
        response = Response(snapshot["content"].encode("utf-8"), mimetype="text/plain")
        response.headers["Content-Disposition"] = f'attachment; filename="{name}"'
        response.headers["Cache-Control"] = "private, no-store"
        return response

    @app.get("/learning-product/materials.js")
    def materials_js():
        return send_from_directory(_STATIC, "materials.js", mimetype="text/javascript")

    @app.get("/learning-product/materials.css")
    def materials_css():
        return send_from_directory(_STATIC, "materials.css", mimetype="text/css")

    def compose(response):
        if request.path == "/" and response.status_code == 200 and response.mimetype == "text/html":
            html = response.get_data(as_text=True)
            response.set_data(html.replace("</head>", _CSS + "\n" + _JS + "\n</head>", 1))
        if request.path == "/api/materials/download":
            response.headers["Cache-Control"] = "private, no-store"
        return response

    # Flask executes after_request callbacks in reverse registration order.
    # Run after day4's asset injection and day31's cache headers.
    app.after_request_funcs.setdefault(None, []).insert(0, compose)
    return app

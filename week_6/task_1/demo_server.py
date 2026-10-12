"""Loopback-only diagnostic page for three fixed local model requests."""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from local_llm import LocalAgent, ROOT, load_scenarios, runtime_status


def make_server(agent: LocalAgent, port: int = 5026) -> HTTPServer:
    scenarios = load_scenarios()
    by_id = {item["id"]: item for item in scenarios}

    class Handler(BaseHTTPRequestHandler):
        def _json(self, value: object, code: int = 200) -> None:
            body = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path in ("/", "/index.html"):
                body = (ROOT / "static" / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/api/scenarios":
                self._json(scenarios)
            elif self.path == "/api/status":
                self._json(runtime_status(agent))
            else:
                self._json({"error": "Неизвестный путь"}, 404)

        def do_POST(self) -> None:
            if self.path != "/api/run":
                self._json({"error": "Неизвестный путь"}, 404)
                return
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                self._json({"error": "Требуется application/json"}, 415)
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size < 1 or size > 4096:
                    raise ValueError("Недопустимый размер запроса")
                payload = json.loads(self.rfile.read(size))
                if not isinstance(payload, dict) or set(payload) != {"scenario_id"}:
                    raise ValueError("Разрешён только scenario_id")
                scenario = by_id.get(payload["scenario_id"])
                if scenario is None:
                    raise ValueError("Неизвестный сценарий")
            except (ValueError, TypeError, KeyError):
                self._json({"error": "Неверный scenario_id"}, 400)
                return
            result = agent.run(scenario["prompt"], scenario_id=scenario["id"], save_text=True)
            public_receipt = {key: value for key, value in result["receipt"].items() if key not in {"prompt", "response_text"}}
            self._json({"status": result["status"], "answer": result["answer"], "prompt": scenario["prompt"], "receipt": public_receipt})

        def log_message(self, format: str, *args: object) -> None:
            return

    return HTTPServer(("127.0.0.1", port), Handler)


def main() -> None:
    parser = argparse.ArgumentParser(description="Локальная диагностическая страница Day 26")
    parser.add_argument("--port", type=int, default=5026)
    args = parser.parse_args()
    server = make_server(LocalAgent(), args.port)
    print(f"Откройте http://127.0.0.1:{server.server_port} — только локальный доступ", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

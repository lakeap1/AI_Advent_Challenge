"""Probe safety regression against a real local HTTP socket."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from threading import Thread
from unittest.mock import patch
import urllib.request

import pytest

from week_6.task_5.scripts import probe_service


def test_probe_ignores_http_proxy_and_reaches_loopback(monkeypatch):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, self.headers.get("Authorization")))
            payload = b'{"status":"ready"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
        monkeypatch.delenv("no_proxy", raising=False)
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.setattr(urllib.request, "proxy_bypass", lambda _host: False)
        response = probe_service.call(f"http://127.0.0.1:{server.server_port}",
                                      "secret", "GET", "/api/private/runtime")
        assert response["http_status"] == 200
        assert seen == [("/api/private/runtime", "Bearer secret")]
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_probe_stops_after_first_failed_inference_and_keeps_key_out_of_report(tmp_path):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, status, body):
            payload = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            calls.append(("GET", self.path))
            if self.headers.get("Authorization") != "Bearer " + "s" * 48:
                self._reply(401, {"code": "unauthorized"})
                return
            if self.path == "/api/private/state":
                self._reply(200, {"blocked": False, "ledger": {"complete": True,
                    "unknown_usage": False}, "counters": {"inference_calls": 0},
                    "receipts": []})
            elif self.path == "/api/private/runtime":
                self._reply(200, {"status": "ready", "model": "qwen3.5:4b"})
            else:
                self._reply(404, {})

        def do_POST(self):
            calls.append(("POST", self.path))
            self._reply(503, {"status": "error", "code": "unavailable"})

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        secret = "s" * 48
        token_file = tmp_path / "token"
        token_file.write_text(secret, encoding="ascii")
        report_file = tmp_path / "report.json"
        argv = ["probe_service.py", "--base-url", f"http://127.0.0.1:{server.server_port}",
                "--token-file", str(token_file), "--output", str(report_file)]
        with patch("sys.argv", argv):
            assert probe_service.main() == 1
        report_text = report_file.read_text(encoding="utf-8")
        report = json.loads(report_text)
        assert report["status"] == "fail"
        assert calls.count(("POST", "/api/private/ask")) == 1
        assert secret not in report_text
        assert report["events"][-1]["label"] == "state_after_failure"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


@pytest.mark.parametrize("extra_receipt", [False, True])
def test_resume_reuses_six_receipts_and_sends_no_inference(tmp_path, extra_receipt):
    requests = []
    receipts = [{"id": index + 1, "status": "ok", "output_policy": "accepted",
                 "usage_status": "known", "usage": {"input_tokens": 10,
                 "output_tokens": 5, "total_tokens": 15}, "cost_usd": "0"}
                for index in range(6)]

    def state(counter, *, saved=False):
        current_receipts = receipts if saved or not extra_receipt else receipts + [
            {"id": 7, "status": "ok", "usage_status": "known", "cost_usd": "0"}]
        return {"blocked": False, "ledger": {"complete": True, "unknown_usage": False,
                "total_cost_usd": "0", "requests": len(current_receipts)},
                "counters": {"inference_calls": counter},
                "receipts": current_receipts, "messages": []}

    class Handler(BaseHTTPRequestHandler):
        invalid_count = 0

        def reply(self, status, body, retry_after=None):
            payload = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            if retry_after:
                self.send_header("Retry-After", retry_after)
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            requests.append(("GET", self.path, None))
            if self.headers.get("Authorization") != "Bearer " + "s" * 48:
                self.reply(401, {"code": "unauthorized"})
            elif self.path == "/api/private/state":
                self.reply(200, state(0))  # gateway counters reset on restart
            elif self.path == "/api/private/runtime":
                self.reply(200, {"status": "ready", "model": "qwen3.5:4b",
                                 "processor": "CPU", "placement": "configured num_gpu=0"})
            else:
                self.reply(404, {})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(("POST", self.path, body))
            if body["prompt"] == "Ж" * 1800:
                self.reply(400, {"receipt": {"usage_status": "not_requested"}})
            elif body["prompt"] == "":
                Handler.invalid_count += 1
                if Handler.invalid_count <= 9:  # context rejection used the first of ten attempts
                    self.reply(400, {"receipt": {"usage_status": "not_requested"}})
                else:
                    self.reply(429, {"code": "rate_limited"}, "60")
            else:
                self.reply(500, {"error": "unexpected inference request"})

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        events = []
        for index, (label, prompt, receipt) in enumerate(zip(
                [f"sequential_{i}" for i in range(1, 4)] +
                [f"concurrent_{i}" for i in range(1, 4)], probe_service.PROMPTS,
                receipts)):
            events.append({"label": label, "http_status": 200,
                           "request": {"prompt": prompt},
                           "body": {"status": "ok", "answer": "Ответ.",
                                    "receipt": receipt}})
        events.append({"label": "after_answers", "http_status": 200,
                       "body": state(6, saved=True)})
        prior = {"status": "fail", "mode": "probe", "base_url": base_url,
                 "initial_state": {**state(0, saved=True), "receipts": []},
                 "runtime": {"status": "ready", "model": "qwen3.5:4b"},
                 "events": events, "error": "Old CPU assertion"}
        prior_file = tmp_path / "prior.json"
        prior_file.write_text(json.dumps(prior), encoding="utf-8")
        token_file = tmp_path / "token"
        token_file.write_text("s" * 48, encoding="ascii")
        output = tmp_path / "resumed.json"
        argv = ["probe_service.py", "--base-url", base_url,
                "--token-file", str(token_file), "--resume-checks", str(prior_file),
                "--output", str(output)]
        with patch("sys.argv", argv):
            assert probe_service.main() == (1 if extra_receipt else 0)
        result = json.loads(output.read_text(encoding="utf-8"))
        assert result["status"] == ("fail" if extra_receipt else "pass")
        if not extra_receipt:
            assert result["summary"]["inference_calls_added"] == 6
        assert all(item[2] in ({"prompt": "Ж" * 1800}, {"prompt": ""})
                   for item in requests if item[0] == "POST")
        assert len([item for item in requests if item[0] == "POST"]) == (0 if extra_receipt else 11)
        assert prior_file.read_text(encoding="utf-8") == json.dumps(prior)
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()

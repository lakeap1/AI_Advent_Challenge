"""Checks for bounded benchmark accounting and process-local network isolation."""

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import benchmark, check_local

ROOT = Path(__file__).resolve().parents[1]


def _question():
    return {"id": "q1", "prompt": "Smoothness?", "expected_file": "unity6_urp_lit.md",
            "keyword_groups": [["smoothness"], ["alpha", "альфа"]]}


def _result(status="ok", cloud_cost=0.001):
    return {"status": status, "question": "Smoothness?", "error": None,
            "retrieval": {"sources": [{"file": "unity6_urp_lit.md"}],
                          "context_sha256": "context", "request_sha256": "request"},
            "results": [{"provider": "local", "status": "ok", "answer": "Smoothness alpha",
                         "request_sha256": "request", "elapsed_seconds": 1.2,
                         "receipt": {"id": 10, "cost_usd": 0, "usage": {"input_tokens": 20,
                                     "output_tokens": 5, "total_tokens": 25}}},
                        {"provider": "cloud", "status": "ok" if status == "ok" else "rejected",
                         "answer": "Smoothness" if status == "ok" else None,
                         "request_sha256": "request", "elapsed_seconds": 0.7,
                         "receipt": {"id": 11, "cost_usd": cloud_cost,
                                     "usage": {"input_tokens": 30, "output_tokens": 6,
                                               "total_tokens": 36}}}],
            "ledger": {"known_usd": cloud_cost, "total_usd": cloud_cost,
                       "incomplete": False}}


def test_report_keeps_failures_and_distinguishes_proxy_from_grading():
    good = {"question_id": "q1", "repeat": 1, "elapsed_seconds": 3.2,
            "query_embedding_seconds": 0.2, "result": _result(), "receipt_ids": [10, 11]}
    bad = {"question_id": "q1", "repeat": 2, "elapsed_seconds": 2.0,
           "query_embedding_seconds": 0.2, "result": _result("rejected"), "receipt_ids": [12, 13]}
    run = {"protocol": "day28-paired-v1", "base_cost_usd": 0,
           "budget_usd": 0.05, "cases": [good, bad], "status": "stopped",
           "series_cost_usd": 0.002}
    report = benchmark.analyze_run(run, [_question()])
    assert report["attempts"] == 2
    assert report["providers"]["cloud"]["successes"] == 1
    assert report["providers"]["cloud"]["failures"] == 1
    assert report["retrieval"]["expected_file_hits"] == 2
    assert report["providers"]["local"]["keyword_proxy"]["matched_groups"] == 4
    assert report["providers"]["local"]["keyword_proxy"]["semantic_grade"] is None
    assert report["request_hash_identity"]["matching_cases"] == 2
    assert report["providers"]["cloud"]["answer_hashes_by_question"]["q1"]["distinct"] == 1


def test_budget_stops_before_next_call_and_preserves_partial(tmp_path, monkeypatch):
    class Store:
        reads = 0

        def receipts(self, limit=100):
            return []

        def ledger(self):
            self.reads += 1
            cost = 0 if self.reads == 1 else 0.049
            return {"known_usd": cost, "total_usd": cost, "incomplete": False}

    class Fake:
        store = Store()
        config = type("Config", (), {"cloud": {"input_usd_per_million": 0.1,
            "cache_write_usd_per_million": 0.125, "output_usd_per_million": 0.5,
            "max_output_tokens": 1024}, "retrieval": {"context_bytes": 4200}})()

        def ask(self, prompt, mode):
            raise AssertionError("budget should block this paid request")

    target = tmp_path / "run.json"
    run = benchmark.run_benchmark(Fake(), [_question()], target, repeats=3, budget_usd=0.05)
    assert run["status"] == "stopped_budget"
    assert run["cases"] == []
    assert json.loads(target.read_text(encoding="utf-8"))["status"] == "stopped_budget"


def test_failed_paid_case_stops_and_keeps_known_cost(tmp_path):
    class Store:
        calls = 0

        def receipts(self, limit=100):
            return [{"id": 11, "stage": "generation", "elapsed_seconds": 0.7}]

        def ledger(self):
            self.calls += 1
            cost = 0 if self.calls < 3 else 0.001
            return {"known_usd": cost, "total_usd": cost, "incomplete": False}

    class Fake:
        store = Store()
        config = type("Config", (), {"cloud": {"input_usd_per_million": 0.1,
            "cache_write_usd_per_million": 0.125, "output_usd_per_million": 0.5,
            "max_output_tokens": 1024}, "retrieval": {"context_bytes": 4200}})()
        calls = 0

        def ask(self, prompt, mode):
            self.calls += 1
            assert mode == "compare"
            return _result("rejected")

    fake = Fake()
    target = tmp_path / "partial.json"
    run = benchmark.run_benchmark(fake, [_question()], target, repeats=3)
    assert fake.calls == 1
    assert run["status"] == "stopped_case_failure"
    assert run["series_cost_usd"] == 0.001
    assert json.loads(target.read_text(encoding="utf-8"))["cases"][0]["result"]["status"] == "rejected"
    report = benchmark.analyze_run(run, [_question()])
    assert report["providers"]["cloud"]["known_cost_usd"] == 0.001


def test_socket_guard_denies_external_and_restores_dns_and_connect():
    original_connect = socket.socket.connect
    original_lookup = socket.getaddrinfo
    with check_local.loopback_guard() as audit:
        with pytest.raises(check_local.ExternalNetworkBlocked):
            socket.getaddrinfo("example.com", 443)
        with pytest.raises(check_local.ExternalNetworkBlocked):
            socket.socket().connect(("93.184.216.34", 443))
        assert socket.getaddrinfo("localhost", 80)
        assert audit["blocked_external_attempts"] == 2
    assert socket.socket.connect is original_connect
    assert socket.getaddrinfo is original_lookup


def test_socket_guard_blocks_udp_and_reverse_dns_without_sending(monkeypatch):
    calls = []

    def fake_sendto(sock, *args):
        calls.append(("sendto", args[-1]))
        return len(args[0])

    def fake_reverse(host):
        calls.append(("reverse", host))
        return ("localhost", [], [host])

    monkeypatch.setattr(socket.socket, "sendto", fake_sendto)
    monkeypatch.setattr(socket, "gethostbyaddr", fake_reverse)
    udp = socket.socket(type=socket.SOCK_DGRAM)
    try:
        with check_local.loopback_guard() as audit:
            with pytest.raises(check_local.ExternalNetworkBlocked):
                udp.sendto(b"x", ("8.8.8.8", 53))
            with pytest.raises(check_local.ExternalNetworkBlocked):
                socket.gethostbyaddr("8.8.8.8")
            assert calls == []
            assert udp.sendto(b"x", ("127.0.0.1", 9)) == 1
            assert socket.gethostbyaddr("127.0.0.1")[0] == "localhost"
            assert audit["blocked_external_attempts"] == 2
        assert socket.socket.sendto is fake_sendto
        assert socket.gethostbyaddr is fake_reverse
        assert calls == [("sendto", ("127.0.0.1", 9)), ("reverse", "127.0.0.1")]
    finally:
        udp.close()


def test_probe_refuses_existing_output_before_model_call(tmp_path, monkeypatch):
    output = tmp_path / "accepted.json"
    output.write_text("original", encoding="utf-8")

    def forbidden_probe(*args, **kwargs):
        raise AssertionError("probe must not run when output exists")

    monkeypatch.setattr(check_local, "probe", forbidden_probe)
    with pytest.raises(SystemExit) as error:
        check_local.main(["--data-dir", str(tmp_path), "--output", str(output)])
    assert error.value.code == 2
    assert output.read_text(encoding="utf-8") == "original"


@pytest.mark.parametrize("script,expected", [
    ("check_local.py", "index_not_ready"),
    ("benchmark.py", "stopped_index_not_ready"),
])
def test_direct_script_on_empty_index_fails_before_inference(tmp_path, script, expected):
    output = tmp_path / (script + ".json")
    environment = os.environ.copy()
    environment.pop("OPENAI_API_KEY", None)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    command = [sys.executable, "-B", str(ROOT / "scripts" / script),
               "--data-dir", str(tmp_path / "empty"), "--output", str(output)]
    if script == "benchmark.py":
        command += ["--repeats", "1"]
    process = subprocess.run(command, cwd=ROOT, env=environment, text=True,
                             capture_output=True, timeout=30)
    assert process.returncode == 2, process.stderr
    assert "ModuleNotFoundError" not in process.stderr
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == expected

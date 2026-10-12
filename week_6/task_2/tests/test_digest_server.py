"""Digest server migration, source, due, failure, and MCP contracts."""

import asyncio
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from mcp import types

from digest_server import server
from agent.digest import DigestAgent
from digest_server.provider import ProviderError, QuestionBatch, QuestionsProvider
from digest_server.service import DigestService, current_slot, next_slot
from digest_server.store import DigestStore, ProcessLock


def run(awaitable):
    return asyncio.run(awaitable)


def question(source, qid, created, answers=0, tags=("lighting",)):
    return {"source": source, "question_id": qid, "title": f"Question {qid}",
            "url": f"https://{source}.stackexchange.com/questions/{qid}",
            "answer_count": answers, "tags": list(tags), "created_at": created,
            "excerpt": "A short body"}


class Batches:
    def __init__(self, *items):
        self.items = list(items)
        self.calls = []

    async def collect(self, start, end, source):
        self.calls.append((start, end, source))
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_omsk_fixed_slots_match_utc_cron_hours():
    midnight_utc = datetime(2026, 9, 29, tzinfo=timezone.utc).timestamp()
    assert current_slot(midnight_utc + 5 * 3600 + 59 * 60) == midnight_utc
    assert next_slot(midnight_utc + 5 * 3600 + 59 * 60) == midnight_utc + 6 * 3600
    assert current_slot(midnight_utc + 6 * 3600) == midnight_utc + 6 * 3600


def test_schedule_initial_catchup_repeat_configure_manual_and_duplicate_ticks(tmp_path):
    now = [1_000_000.0]
    provider = Batches(*(QuestionBatch([], False, None, None) for _ in range(6)))
    store = DigestStore(tmp_path / "state.sqlite3")
    service = DigestService(store, provider, lambda: now[0])
    first = run(service.configure())
    assert first["schedule"]["next_due"] == current_slot(now[0])
    assert first["schedule"]["hours"] == [0, 6, 12, 18]
    assert first["schedule"]["timezone"] == "Asia/Omsk"
    assert run(service.configure())["schedule"]["next_due"] == current_slot(now[0])
    due = run(service.run_due())
    assert due["executed"] is True
    assert due["runs"][0]["status"] == "empty"
    assert due["schedule"]["next_due"] == next_slot(now[0])
    assert run(service.run_due())["executed"] is False
    manual = run(service.collect())
    assert manual["executed"] is True
    assert manual["schedule"]["next_due"] == next_slot(now[0])
    assert len(manual["runs"]) == 2
    assert len(provider.calls) == 6
    store.close()


def test_same_question_id_on_different_sites_and_paginated_get(tmp_path):
    now = [1_000_000.0]
    provider = Batches(QuestionBatch([question("blender", 5, 999900)], False, 20, None),
                       QuestionBatch([question("computergraphics", 5, 999800, 2)], False, 19, None),
                       QuestionBatch([], False, 18, None),
                       QuestionBatch([], False, 17, None), QuestionBatch([], False, 16, None),
                       QuestionBatch([], False, 15, None))
    store = DigestStore(tmp_path / "state.sqlite3")
    service = DigestService(store, provider, lambda: now[0])
    run(service.configure())
    first = run(service.collect())["latest"]
    assert first["question_count"] == 2
    assert first["unanswered_count"] == 1
    assert first["source_counts"] == {"blender": 1, "computergraphics": 1, "graphicdesign": 0}
    assert first["top_tags"] == [{"tag": "lighting", "count": 2}]
    assert len(first["highlights"]) == 2
    assert {q["source"] for q in first["questions"]} == {"blender", "computergraphics"}
    assert store.db.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 2
    second = run(service.collect())["latest"]
    page1 = run(service.list_digests(limit=1))
    assert [d["run_id"] for d in page1["digests"]] == [first["run_id"]]
    assert page1["has_more"] is True
    page2 = run(service.list_digests(after_run_id=page1["next_cursor"], limit=1))
    assert [d["run_id"] for d in page2["digests"]] == [second["run_id"]]
    assert page2["has_more"] is False
    assert run(service.get_digest(first["run_id"])) == {"digest": first}
    with pytest.raises(ValueError, match="not found"):
        run(service.get_digest(999))
    store.close()


def test_backoff_stops_cross_site_calls_and_all_failed_retries_after_deadline(tmp_path):
    now = [1_000_000.0]
    provider = Batches(ProviderError("throttle", backoff_until=1_000_400),
                       QuestionBatch([], False, None, None),
                       QuestionBatch([], False, None, None),
                       QuestionBatch([], False, None, None))
    store = DigestStore(tmp_path / "state.sqlite3")
    service = DigestService(store, provider, lambda: now[0])
    run(service.configure())
    result = run(service.run_due())
    assert result["executed"] is True
    assert result["runs"][0]["status"] == "error"
    assert result["latest"] is None
    assert result["schedule"]["backoff_until"] == 1_000_400
    assert len(provider.calls) == 1
    assert run(service.run_due())["executed"] is False
    store.close()
    reopened = DigestStore(tmp_path / "state.sqlite3")
    now[0] = 1_000_401
    service = DigestService(reopened, provider, lambda: now[0])
    result = run(service.run_due())
    assert result["executed"] is True
    assert result["runs"][0]["status"] == "empty"
    assert result["schedule"]["next_due"] == next_slot(now[0])
    assert len(provider.calls) == 4
    reopened.close()


def test_partial_result_is_explicit_and_keeps_source_errors(tmp_path):
    now = [1_000_000.0]
    provider = Batches(QuestionBatch([question("blender", 1, 999900)], True, 10, None),
                       ProviderError("offline"), QuestionBatch([], False, 9, None))
    store = DigestStore(tmp_path / "state.sqlite3")
    service = DigestService(store, provider, lambda: now[0])
    run(service.configure())
    result = run(service.run_due())
    digest = result["latest"]
    assert digest["partial"] is True
    assert digest["source_status"] == {"blender": "partial", "computergraphics": "error", "graphicdesign": "ok"}
    assert digest["source_errors"] == {"computergraphics": "offline"}
    assert result["runs"][0]["status"] == "partial"
    store.close()


def test_transient_partial_retries_after_five_minutes_across_restart_then_completes_slot(tmp_path):
    now = [1_000_000.0]
    path = tmp_path / "state.sqlite3"
    first_provider = Batches(QuestionBatch([question("blender", 1, 999900)], False, 10, None),
                             ProviderError("offline"), QuestionBatch([], False, 9, None))
    store = DigestStore(path)
    service = DigestService(store, first_provider, lambda: now[0])
    run(service.configure())
    first = run(service.run_due())
    saved = first["latest"]
    assert first["runs"][0]["status"] == "partial"
    assert first["schedule"]["next_due"] == now[0] + 300
    store.close()

    retry_provider = Batches(*(QuestionBatch([], False, 8, None) for _ in range(3)))
    reopened = DigestStore(path)
    service = DigestService(reopened, retry_provider, lambda: now[0])
    now[0] += 299
    assert run(service.run_due())["executed"] is False
    assert retry_provider.calls == []
    now[0] += 2
    second = run(service.run_due())
    assert second["executed"] is True
    assert second["runs"][0]["status"] == "empty"
    assert second["runs"][0]["scheduled_slot"] == first["runs"][0]["scheduled_slot"]
    assert second["schedule"]["next_due"] == next_slot(now[0])
    assert run(service.get_digest(saved["run_id"])) == {"digest": saved}
    assert [d["run_id"] for d in run(service.list_digests())["digests"]] == [saved["run_id"], second["latest"]["run_id"]]
    assert run(service.run_due())["executed"] is False
    reopened.close()


def test_partial_skipped_by_shared_backoff_retries_only_after_deadline(tmp_path):
    now = [1_000_000.0]
    path = tmp_path / "state.sqlite3"
    first_provider = Batches(QuestionBatch([], False, 10, now[0] + 600))
    store = DigestStore(path)
    service = DigestService(store, first_provider, lambda: now[0])
    run(service.configure())
    first = run(service.run_due())
    assert first["latest"]["source_status"] == {
        "blender": "ok", "computergraphics": "skipped_backoff", "graphicdesign": "skipped_backoff"}
    assert first["runs"][0]["status"] == "partial"
    assert first["schedule"]["next_due"] == now[0] + 600
    assert len(first_provider.calls) == 1
    store.close()
    retry_provider = Batches(*(QuestionBatch([], False, None, None) for _ in range(3)))
    reopened = DigestStore(path)
    service = DigestService(reopened, retry_provider, lambda: now[0])
    now[0] += 599
    assert run(service.run_due())["executed"] is False
    now[0] += 2
    assert run(service.run_due())["executed"] is True
    assert len(retry_provider.calls) == 3
    reopened.close()


def test_bounded_partial_is_final_for_slot(tmp_path):
    now = [1_000_000.0]
    provider = Batches(QuestionBatch([], True, None, None),
                       QuestionBatch([], False, None, None),
                       QuestionBatch([], False, None, None))
    store = DigestStore(tmp_path / "state.sqlite3")
    service = DigestService(store, provider, lambda: now[0])
    run(service.configure())
    first = run(service.run_due())
    assert first["runs"][0]["status"] == "partial"
    assert first["schedule"]["next_due"] == next_slot(now[0])
    now[0] += 301
    assert run(service.run_due())["executed"] is False
    assert len(provider.calls) == 3
    store.close()


def test_deployed_partial_with_source_error_is_reclassified_and_retry_due_repaired(tmp_path):
    now = [1_000_000.0]
    path = tmp_path / "deployed.sqlite3"
    provider = Batches(QuestionBatch([], False, None, None), ProviderError("offline"),
                       QuestionBatch([], False, None, None))
    store = DigestStore(path)
    service = DigestService(store, provider, lambda: now[0])
    run(service.configure())
    first = run(service.run_due())
    assert first["runs"][0]["status"] == "partial"
    # Reproduce the already deployed version's persisted row and next slot.
    with store.db:
        store.db.execute("UPDATE runs SET retryable=0 WHERE id=?", (first["runs"][0]["id"],))
    store.defer(next_slot(now[0]), None)
    store.close()
    reopened = DigestStore(path)
    recovered = DigestService(reopened, Batches(), lambda: now[0])
    assert run(recovered.state())["runs"][0]["retryable"] == 1
    assert run(recovered.state())["schedule"]["next_due"] == now[0] + 300
    assert reopened.has_completed_slot(current_slot(now[0])) is False
    reopened.close()


def test_observed_backoff_is_durable_before_digest_commit(tmp_path, monkeypatch):
    now = [1_000_000.0]
    path = tmp_path / "state.sqlite3"
    provider = Batches(QuestionBatch([], False, None, now[0] + 600))
    store = DigestStore(path)
    service = DigestService(store, provider, lambda: now[0])
    run(service.configure())
    monkeypatch.setattr(store, "finish_success", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("crash before commit")))
    with pytest.raises(RuntimeError, match="crash before commit"):
        run(service.run_due())
    assert store.schedule()["backoff_until"] == now[0] + 600
    store.close()
    reopened = DigestStore(path)
    recovered = DigestService(reopened, Batches(), lambda: now[0])
    assert run(recovered.state())["runs"][0]["status"] == "interrupted"
    assert run(recovered.run_due())["executed"] is False
    reopened.close()


def test_backoff_storage_failure_stops_shared_method_requests(tmp_path, monkeypatch):
    now = [1_000_000.0]
    provider = Batches(QuestionBatch([], False, None, now[0] + 600),
                       QuestionBatch([], False, None, None))
    store = DigestStore(tmp_path / "state.sqlite3")
    service = DigestService(store, provider, lambda: now[0])
    run(service.configure())
    monkeypatch.setattr(store, "record_backoff", lambda _: (_ for _ in ()).throw(sqlite3.OperationalError("disk error")))
    with pytest.raises(sqlite3.OperationalError, match="disk error"):
        run(service.run_due())
    assert len(provider.calls) == 1
    store.close()


def test_backoff_survives_rejected_question_and_blocks_remaining_sites(tmp_path):
    now = [1_000_000.0]
    bad = question("blender", 1, 999900)
    bad["url"] = "https://evil.example/1"
    provider = Batches(QuestionBatch([bad], False, None, 1_000_500))
    store = DigestStore(tmp_path / "state.sqlite3")
    service = DigestService(store, provider, lambda: now[0])
    run(service.configure())
    result = run(service.run_due())
    assert result["runs"][0]["status"] == "error"
    assert result["runs"][0]["output_policy"] == "rejected"
    assert result["schedule"]["backoff_until"] == 1_000_500
    assert len(provider.calls) == 1
    store.close()


def test_recovery_marks_running_interrupted_and_one_current_window_catchup(tmp_path):
    now = [1_000_000.0]
    path = tmp_path / "state.sqlite3"
    store = DigestStore(path)
    service = DigestService(store, Batches(), lambda: now[0])
    run(service.configure(sources=["blender"]))
    store.start_run(now[0], now[0] - 86400, now[0], current_slot(now[0]))
    store.close()
    now[0] += 7200
    provider = Batches(QuestionBatch([], False, None, None))
    reopened = DigestStore(path)
    recovered = DigestService(reopened, provider, lambda: now[0])
    assert run(recovered.state())["runs"][0]["status"] == "interrupted"
    assert run(recovered.run_due())["executed"] is True
    assert run(recovered.run_due())["executed"] is False
    assert [r["status"] for r in run(recovered.state())["runs"]] == ["empty", "interrupted"]
    assert len(provider.calls) == 1
    reopened.close()


def test_legacy_sqlite_migrates_schedule_observations_and_digest_without_loss(tmp_path):
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript("""
        CREATE TABLE schedule(id INTEGER PRIMARY KEY, enabled INTEGER, interval_seconds INTEGER, window_hours INTEGER, tag TEXT, next_due REAL, demo INTEGER, demo_expires_at REAL, backoff_until REAL);
        INSERT INTO schedule VALUES(1,1,21600,24,'',1000001,0,NULL,NULL);
        CREATE TABLE runs(id INTEGER PRIMARY KEY, status TEXT, started_at REAL, finished_at REAL, error TEXT, window_start REAL, window_end REAL, input_policy TEXT, output_policy TEXT, partial INTEGER, quota_remaining INTEGER);
        INSERT INTO runs VALUES(1,'success',999000,999100,NULL,900000,999000,'passed','passed',0,10);
        CREATE TABLE observations(run_id INTEGER,question_id INTEGER,title TEXT,url TEXT,answer_count INTEGER,tags_json TEXT,created_at REAL,PRIMARY KEY(run_id,question_id));
        INSERT INTO observations VALUES(1,7,'Old','https://blender.stackexchange.com/questions/7',0,'[]',998000);
        CREATE TABLE digests(run_id INTEGER PRIMARY KEY,payload_json TEXT);
        """)
        legacy = {"run_id": 1, "window_start": 900000, "window_end": 999000,
                  "question_count": 1, "unanswered_count": 1,
                  "top_tags": [], "questions": [{"question_id": 7, "title": "Old",
                    "url": "https://blender.stackexchange.com/questions/7", "answer_count": 0,
                    "tags": [], "created_at": 998000}], "partial": False,
                  "text": "Old", "generated_at": 999100}
        db.execute("INSERT INTO digests VALUES(1,?)", (json.dumps(legacy),))
    store = DigestStore(path)
    service = DigestService(store, Batches(), lambda: 1_000_000.0)
    assert run(service.state())["schedule"]["next_due"] == current_slot(1_000_000.0)
    old = run(service.get_digest(1))["digest"]
    assert old["text"] == "Old"
    assert old["questions"][0]["source"] == "blender"
    assert DigestAgent(tmp_path / "client")._validate_digest(old) == old
    assert store.db.execute("SELECT source,excerpt FROM observations WHERE run_id=1").fetchone()[:] == ("blender", "")
    store.close()


def test_provider_uses_body_site_and_enforces_limits_and_backoff():
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"items": [{"question_id": 5, "creation_date": 100,
            "title": "Nodes &amp; light", "body": "<p>Hello <b>artist</b></p><script>bad</script>",
            "answer_count": 0, "tags": ["lighting"]}],
            "has_more": True, "quota_remaining": 9, "backoff": 60})
    provider = QuestionsProvider(lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)), lambda: 200.0)
    batch = run(provider.collect(90, 110, "graphicdesign"))
    assert len(requests) == 1
    assert requests[0].url.params["site"] == "graphicdesign"
    assert requests[0].url.params["filter"] == "withbody"
    assert requests[0].url.params["pagesize"] == "100"
    assert batch.questions[0]["title"] == "Nodes & light"
    assert batch.questions[0]["excerpt"] == "Hello artist"
    assert batch.questions[0]["source"] == "graphicdesign"
    assert batch.partial is True
    assert batch.backoff_until == 260.0
    with pytest.raises(ValueError):
        run(provider.collect(90, 110, "evil"))


def test_provider_rejects_bad_payload_and_http_failure():
    for payload in ({"items": [], "has_more": "false"},
                    {"items": [{"question_id": 1, "creation_date": 89, "answer_count": 0,
                                "title": "Outside", "tags": []}], "has_more": False},
                    {"error_id": 502, "error_message": "throttle", "backoff": 30}):
        provider = QuestionsProvider(lambda p=payload: httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=p))))
        with pytest.raises(ProviderError):
            run(provider.collect(90, 110, "blender"))
    provider = QuestionsProvider(lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(503))))
    with pytest.raises(ProviderError, match="HTTP 503"):
        run(provider.collect(90, 110, "blender"))


def test_provider_requires_body_when_body_was_requested():
    payload = {"items": [{"question_id": 5, "creation_date": 100, "title": "No body",
                          "answer_count": 0, "tags": []}], "has_more": False}
    provider = QuestionsProvider(lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))))
    with pytest.raises(ProviderError, match="invalid text"):
        run(provider.collect(90, 110, "blender"))


def test_mcp_tool_contract_and_process_lock(tmp_path, monkeypatch):
    with ProcessLock(tmp_path / "server.lock"):
        with pytest.raises(RuntimeError, match="already in use"):
            with ProcessLock(tmp_path / "server.lock"):
                pass
    store = DigestStore(tmp_path / "state.sqlite3")
    service = DigestService(store, Batches(QuestionBatch([], False, None, None)), lambda: 1_000_000.0)
    monkeypatch.setattr(server, "service", service)
    tools = {tool.name for tool in run(server.mcp.list_tools())}
    assert tools == {"configure_digest", "pause_digest", "get_digest_state", "collect_digest",
                     "run_scheduled_digest", "list_digests", "get_digest"}
    configured = run(server.mcp.call_tool("configure_digest", {"sources": ["blender"]}))
    assert configured.structured_content["schedule"]["sources"] == ["blender"]
    created = run(server.mcp.call_tool("collect_digest", {}))
    run_id = created.structured_content["latest"]["run_id"]
    assert run(server.mcp.call_tool("get_digest", {"run_id": run_id})).structured_content["digest"]["run_id"] == run_id
    assert len(run(server.mcp.call_tool("list_digests", {})).structured_content["digests"]) == 1
    failure = run(server.mcp._handle_call_tool(None, types.CallToolRequestParams(
        name="configure_digest", arguments={"sources": ["evil"]})))
    assert failure.is_error is True
    store.close()


def test_lifespan_has_no_internal_worker(tmp_path, monkeypatch):
    monkeypatch.setenv("DIGEST_DATA_DIR", str(tmp_path / "digest"))
    async def exercise():
        async with server.lifespan(server.mcp):
            configured = await server.configure_digest()
            assert configured["schedule"]["enabled"] is True
            assert configured["runs"] == []
            await asyncio.sleep(0.02)
            assert (await server.get_digest_state())["runs"] == []
        assert server.service is None
    run(exercise())


def test_cron_tick_calls_real_loopback_mcp(tmp_path):
    from digest_server.cron_tick import tick
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = dict(os.environ, DIGEST_PORT=str(port), DIGEST_DATA_DIR=str(tmp_path / "live"),
               PYTHONDONTWRITEBYTECODE="1")
    process = subprocess.Popen([sys.executable, "-m", "digest_server.server"],
                               cwd=Path(__file__).parents[1], env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError(f"MCP server exited: {process.stderr.read()}")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            raise AssertionError("MCP server did not start")
        result = run(tick(f"http://127.0.0.1:{port}/mcp"))
        assert result["executed"] is False
        assert result["schedule"]["enabled"] is False
    finally:
        process.terminate()
        process.communicate(timeout=5)

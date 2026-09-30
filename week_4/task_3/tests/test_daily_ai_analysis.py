"""Daily analysis persistence and paid-attempt behavior at the service boundary."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx

from digest_server.analysis import DailyAnalyzer
from digest_server.provider import QuestionBatch
from digest_server.service import DigestService
from digest_server.store import DigestStore


OMSK = timezone(timedelta(hours=6))


def moment(day, hour=0, month=9):
    return datetime(2026, month, day, hour, tzinfo=OMSK).timestamp()


def run(coro):
    return asyncio.run(coro)


def question(day, month=9):
    return {"source": "blender", "question_id": 23, "title": "Почему normal map выглядит плоской?",
            "url": "https://blender.stackexchange.com/questions/23", "answer_count": 0,
            "tags": ["normal-map"], "created_at": int(moment(day, 12, month)),
            "excerpt": "Свет на модели не показывает мелкие детали."}


class Provider:
    def __init__(self, *batches):
        self.batches = list(batches)

    async def collect(self, _start, _end, _source):
        return self.batches.pop(0)


class Analyzer:
    config = {"input_policy": "bounded_digest", "output_policy": "completed_plain_text"}

    def __init__(self):
        self.calls = []

    def metadata(self, digest):
        return {"digest_date": digest["digest_date"], "run_id": digest["run_id"],
                "requested_model": "gpt-6-luna", "actual_model": None,
                "pricing": {"source": "test", "checked_at": "2026-09-30"}, "error_code": None}

    async def analyze(self, digest):
        self.calls.append(digest["digest_date"])
        await asyncio.sleep(0)
        return {"status": "ok", "text": "Вопросы о картах нормалей.", "usage": None,
                "usage_status": "unavailable", "cost_usd": None,
                "input_policy": {"name": "bounded_digest", "status": "accepted"},
                "output_policy": {"name": "completed_plain_text", "status": "accepted"},
                "metadata": self.metadata(digest)}


def test_one_analysis_for_concurrent_ticks_reopen_and_each_new_day(tmp_path):
    now = [moment(30)]
    path = tmp_path / "digest.sqlite3"
    analyzer = Analyzer()
    store = DigestStore(path)
    service = DigestService(store, Provider(QuestionBatch([question(29)], False, None, None)),
                            lambda: now[0], analyzer=analyzer)
    run(service.configure(["blender"]))

    async def twice():
        return await asyncio.gather(service.run_due(), service.run_due())

    first, second = run(twice())
    assert first["published"] is True and second["published"] is False
    assert analyzer.calls == ["2026-09-29"]
    assert first["latest"]["analysis"]["status"] == "ok"
    run_id = first["latest"]["run_id"]
    assert run(service.get_digest(run_id))["digest"]["analysis"]["attempt_id"] is not None
    assert run(service.list_digests())["digests"][0]["analysis"]["text"] == "Вопросы о картах нормалей."
    store.close()

    reopened = DigestStore(path)
    service = DigestService(reopened, Provider(QuestionBatch([question(30)], False, None, None)),
                            lambda: now[0], analyzer=analyzer)
    assert run(service.run_due())["latest"]["analysis"]["status"] == "ok"
    assert analyzer.calls == ["2026-09-29"]
    now[0] = moment(1, month=10)
    next_day = run(service.run_due())
    assert next_day["published"] is True
    assert analyzer.calls == ["2026-09-29", "2026-09-30"]
    assert [d["analysis"]["status"] for d in run(service.list_digests())["digests"]] == ["ok", "ok"]
    reopened.close()


def test_existing_publication_is_analyzed_once_without_replacing_raw_digest(tmp_path):
    now = moment(30)
    path = tmp_path / "digest.sqlite3"
    store = DigestStore(path)
    service = DigestService(store, Provider(QuestionBatch([question(29)], False, None, None)), lambda: now)
    run(service.configure(["blender"]))
    original = run(service.run_due())["latest"]
    assert original.get("analysis") is None
    store.close()

    analyzer = Analyzer()
    reopened = DigestStore(path)
    service = DigestService(reopened, Provider(), lambda: now, analyzer=analyzer)
    result = run(service.run_due())
    assert result["reason"] == "not_due"
    assert analyzer.calls == ["2026-09-29"]
    updated = result["latest"]
    assert {key: value for key, value in updated.items() if key != "analysis"} == {
        key: value for key, value in original.items() if key != "analysis"}
    assert run(service.collect())["reason"] == "already_published"
    assert analyzer.calls == ["2026-09-29"]
    reopened.close()


def test_crash_after_claim_is_visible_and_never_recalled(tmp_path):
    now = moment(30)
    path = tmp_path / "digest.sqlite3"
    store = DigestStore(path)
    service = DigestService(store, Provider(QuestionBatch([question(29)], False, None, None)), lambda: now)
    run(service.configure(["blender"]))
    digest = run(service.run_due())["latest"]
    analyzer = Analyzer()
    claim = store.claim_analysis(digest["run_id"], now, analyzer.metadata(digest),
                                 analyzer.config["input_policy"], analyzer.config["output_policy"])
    assert claim["status"] == "pending"
    store.close()

    reopened = DigestStore(path)
    service = DigestService(reopened, Provider(), lambda: now + 60, analyzer=analyzer)
    recovered = run(service.run_due())["latest"]["analysis"]
    assert recovered["status"] == "interrupted"
    assert recovered["metadata"]["error_code"] == "interrupted"
    assert analyzer.calls == []
    assert run(service.state())["analysis_summary"] == {
        "known_cost_usd": "0", "cost_complete": False,
        "unknown_cost_requests": 1, "api_requests": 1}
    reopened.close()


def test_empty_publication_does_not_call_model(tmp_path):
    analyzer = Analyzer()
    store = DigestStore(tmp_path / "digest.sqlite3")
    service = DigestService(store, Provider(QuestionBatch([], False, None, None)),
                            lambda: moment(30), analyzer=analyzer)
    run(service.configure(["blender"]))
    result = run(service.run_due())
    assert result["latest"]["analysis"]["status"] == "not_requested"
    assert "не запускался" in result["latest"]["analysis"]["text"]
    assert analyzer.calls == []
    assert result["analysis_summary"]["api_requests"] == 0
    store.close()


def test_rejected_model_output_persists_provider_usage_and_cost(tmp_path):
    calls = []

    def respond(request):
        body = __import__("json").loads(request.content)
        calls.append(body)
        return httpx.Response(200, json={"id": "resp_fake", "status": "completed", "model": "gpt-6-luna",
            "service_tier": "default", "usage": {"input_tokens": 100, "output_tokens": 20,
                "total_tokens": 120, "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 0}},
            "output": [{"type": "message", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": "# Недопустимый Markdown"}]}]})

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            analyzer = DailyAnalyzer("fake_key", client=client)
            store = DigestStore(tmp_path / "digest.sqlite3")
            service = DigestService(store, Provider(QuestionBatch([question(29)], False, None, None)),
                                    lambda: moment(30), analyzer=analyzer)
            await service.configure(["blender"])
            published = await service.run_due()
            again = await service.run_due()
            store.close()
            return published, again

    published, again = run(check())
    result = published["latest"]["analysis"]
    assert result["status"] == "rejected" and result["text"] == ""
    assert result["usage"] == {"input_tokens": 100, "output_tokens": 20,
        "total_tokens": 120, "cached_input_tokens": 0, "cache_write_input_tokens": 0,
        "reasoning_tokens": 0}
    assert result["cost_usd"] == "0.00002"
    assert result["output_policy"]["status"] == "rejected"
    assert published["analysis_summary"] == {"known_cost_usd": "0.00002",
        "cost_complete": True, "unknown_cost_requests": 0, "api_requests": 1}
    assert again["latest"]["analysis"] == result
    assert len(calls) == 1
    assert calls[0]["model"] == "gpt-6-luna"
    assert "обычный текст" in calls[0]["instructions"]


def test_cancelled_analysis_is_closed_without_restart_or_second_call(tmp_path):
    class BlockingAnalyzer(Analyzer):
        def __init__(self):
            super().__init__()
            self.entered = asyncio.Event()

        async def analyze(self, digest):
            self.calls.append(digest["digest_date"])
            self.entered.set()
            await asyncio.Event().wait()

    async def scenario():
        analyzer = BlockingAnalyzer()
        store = DigestStore(tmp_path / "digest.sqlite3")
        service = DigestService(store, Provider(QuestionBatch([question(29)], False, None, None)),
                                lambda: moment(30), analyzer=analyzer)
        await service.configure(["blender"])
        task = asyncio.create_task(service.run_due())
        await asyncio.wait_for(analyzer.entered.wait(), 2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("Cancelled cron task unexpectedly completed")
        after_cancel = await service.state()
        next_tick = await service.run_due()
        store.close()
        return after_cancel, next_tick, analyzer.calls

    after_cancel, next_tick, calls = run(scenario())
    analysis = after_cancel["latest"]["analysis"]
    assert analysis["status"] == "interrupted"
    assert analysis["metadata"]["error_code"] == "interrupted"
    assert analysis["usage_status"] == "unavailable" and analysis["cost_usd"] is None
    assert next_tick["latest"]["analysis"] == analysis
    assert calls == ["2026-09-29"]


def test_large_valid_digest_uses_bounded_context_with_coverage_and_counts():
    sent = []

    def respond(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "resp_fake", "status": "completed", "model": "gpt-6-luna",
            "service_tier": "default", "usage": {"input_tokens": 100, "output_tokens": 20,
                "total_tokens": 120, "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 0}},
            "output": [{"type": "message", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": "Темы дня: карты нормалей и материалы."}]}]})

    raw_questions = []
    for source in ("blender", "computergraphics", "graphicdesign"):
        for number in range(1, 101):
            raw_questions.append({"source": source, "question_id": number,
                "title": f"Вопрос {number} " + "Т" * 220,
                "url": f"https://{source}.stackexchange.com/questions/{number}",
                "answer_count": 0, "tags": ["graphics", "material"],
                "created_at": int(moment(29, 12)), "excerpt": "Э" * 500})
    digest = {"run_id": 1, "digest_date": "2026-09-29", "question_count": 300,
              "partial": False, "source_status": {source: "ok" for source in
                  ("blender", "computergraphics", "graphicdesign")}, "questions": raw_questions}

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            analyzer = DailyAnalyzer("fake_key", client=client)
            accepted = await analyzer.analyze(digest)
            bad = {**digest, "questions": [*raw_questions[:-1], {**raw_questions[-1], "title": 42}]}
            rejected = await analyzer.analyze(bad)
            return accepted, rejected

    accepted, rejected = run(check())
    assert accepted["status"] == "ok"
    context = accepted["metadata"]["context"]
    assert context["total_questions"] == 300
    assert 0 < context["included_questions"] < 300
    assert context["omitted_questions"] == 300 - context["included_questions"]
    assert all(context["included_by_source"][source] > 0 for source in digest["source_status"])
    prompt = sent[0]["input"][0]["content"]
    assert len(prompt) <= 220000
    payload = json.loads(prompt)
    assert payload["included_questions"] == context["included_questions"]
    assert payload["omitted_questions"] == context["omitted_questions"]
    assert len(raw_questions) == 300 and len(raw_questions[0]["excerpt"]) == 500
    assert rejected["status"] == "rejected" and rejected["metadata"]["error_code"] == "invalid_digest"
    assert len(sent) == 1

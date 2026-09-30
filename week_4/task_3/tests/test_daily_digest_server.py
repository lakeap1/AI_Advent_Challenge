"""Daily publication behavior at the MCP service boundary."""

import asyncio
from datetime import datetime, timedelta, timezone

from digest_server.provider import ProviderError, QuestionBatch
from digest_server.service import DigestService
from digest_server.store import DigestStore


OMSK = timezone(timedelta(hours=6))


def moment(day: int, hour: int = 0, minute: int = 0) -> float:
    return datetime(2026, 9, day, hour, minute, tzinfo=OMSK).timestamp()


def run(value):
    return asyncio.run(value)


class Provider:
    def __init__(self, *batches):
        self.batches = list(batches)
        self.calls = []

    async def collect(self, start, end, source):
        self.calls.append((start, end, source))
        result = self.batches.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


EMPTY = QuestionBatch([], False, None, None)


def test_four_collection_slots_publish_once_and_manual_is_idempotent(tmp_path):
    now = [moment(30)]
    provider = Provider(*(EMPTY for _ in range(12)))
    store = DigestStore(tmp_path / "digest.sqlite3")
    service = DigestService(store, provider, lambda: now[0])
    run(service.configure())

    midnight = run(service.run_due())
    assert midnight["executed"] is True and midnight["published"] is True
    digest = midnight["latest"]
    assert digest["digest_date"] == "2026-09-29"
    assert (digest["window_start"], digest["window_end"]) == (moment(29), moment(30))
    assert provider.calls == [(moment(29), moment(30) - 1, source)
                              for source in ("blender", "computergraphics", "graphicdesign")]

    for hour in (6, 12, 18):
        now[0] = moment(30, hour)
        result = run(service.run_due())
        assert result["executed"] is True and result["published"] is False
        assert result["latest"] == digest
    assert len(run(service.list_digests())["digests"]) == 1
    assert len(run(service.state())["runs"]) == 4
    assert run(service.collect())["executed"] is False
    assert run(service.collect())["published"] is False
    assert len(provider.calls) == 12
    store.close()


def test_legacy_completed_slot_does_not_block_daily_catchup(tmp_path):
    now = [moment(30, 7)]
    path = tmp_path / "digest.sqlite3"
    store = DigestStore(path)
    store.configure(["blender"], moment(30, 12))
    old_run = store.start_run(moment(30, 6), moment(29, 6), moment(30, 6), moment(30, 6))
    old = {"run_id": old_run, "window_start": moment(29, 6), "window_end": moment(30, 6),
           "partial": False, "questions": [], "text": "old"}
    store.finish_success(old_run, moment(30, 6), old, [], None, scheduled=False)
    store.close()

    provider = Provider(EMPTY)
    reopened = DigestStore(path)
    service = DigestService(reopened, provider, lambda: now[0])
    result = run(service.run_due())
    assert result["executed"] is True and result["published"] is True
    assert result["latest"]["digest_date"] == "2026-09-29"
    assert [item["run_id"] for item in run(service.list_digests())["digests"]] == [result["latest"]["run_id"]]
    archived = run(service.get_digest(old_run))["digest"]
    assert archived["run_id"] == old_run and archived["text"] == "old"
    assert "digest_date" not in archived
    assert run(service.run_due())["executed"] is False
    reopened.close()


def test_transient_partial_stays_internal_until_retry_after_restart(tmp_path):
    now = [moment(30)]
    path = tmp_path / "digest.sqlite3"
    store = DigestStore(path)
    first_provider = Provider(EMPTY, ProviderError("offline"), EMPTY)
    service = DigestService(store, first_provider, lambda: now[0])
    run(service.configure())
    failed = run(service.run_due())
    assert failed["executed"] is True and failed["published"] is False
    assert failed["latest"] is None and run(service.list_digests())["digests"] == []
    archived_run = failed["runs"][0]["id"]
    assert run(service.get_digest(archived_run))["digest"]["partial"] is True
    store.close()

    now[0] += 299
    reopened = DigestStore(path)
    retry_provider = Provider(EMPTY, EMPTY, EMPTY)
    service = DigestService(reopened, retry_provider, lambda: now[0])
    assert run(service.run_due())["executed"] is False
    now[0] += 2
    completed = run(service.run_due())
    assert completed["executed"] is True and completed["published"] is True
    assert completed["latest"]["digest_date"] == "2026-09-29"
    assert len(run(service.list_digests())["digests"]) == 1
    reopened.close()


def test_late_recovery_only_publishes_last_closed_calendar_day(tmp_path):
    now = [moment(27, 18)]
    path = tmp_path / "digest.sqlite3"
    store = DigestStore(path)
    store.configure(["blender"], moment(27, 18))
    store.close()

    now[0] = moment(30, 23, 59)
    provider = Provider(EMPTY, EMPTY)
    reopened = DigestStore(path)
    service = DigestService(reopened, provider, lambda: now[0])
    first = run(service.run_due())
    assert first["published"] is True
    assert first["latest"]["digest_date"] == "2026-09-29"
    assert provider.calls == [(moment(29), moment(30) - 1, "blender")]
    assert run(service.collect())["reason"] == "already_published"

    now[0] = moment(30) + 86400
    next_day = run(service.run_due())
    assert next_day["published"] is True
    assert next_day["latest"]["digest_date"] == "2026-09-30"
    assert provider.calls[-1] == (moment(30), moment(30) + 86400 - 1, "blender")
    assert [d["digest_date"] for d in run(service.list_digests())["digests"]] == [
        "2026-09-29", "2026-09-30"]
    reopened.close()


def test_failed_attempt_before_midnight_blocks_new_day_until_retry_deadline(tmp_path):
    now = [moment(30, 23, 59) + 58]
    path = tmp_path / "digest.sqlite3"
    store = DigestStore(path)
    service = DigestService(store, Provider(ProviderError("offline")), lambda: now[0])
    run(service.configure(sources=["blender"]))
    first = run(service.run_due())
    assert first["executed"] is True and first["published"] is False
    store.close()

    now[0] = moment(30) + 86400
    provider = Provider(EMPTY)
    reopened = DigestStore(path)
    service = DigestService(reopened, provider, lambda: now[0])
    for call in (service.run_due, service.collect):
        blocked = run(call())
        assert blocked["executed"] is False and blocked["reason"] == "retry_wait"
    assert provider.calls == []

    now[0] += 299
    published = run(service.run_due())
    assert published["executed"] is True and published["published"] is True
    assert published["latest"]["digest_date"] == "2026-09-30"
    assert provider.calls == [(moment(30), moment(30) + 86400 - 1, "blender")]
    reopened.close()

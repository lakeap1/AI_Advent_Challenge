"""Explicit cron due guard and deterministic multi-source digest aggregation."""

from __future__ import annotations

import asyncio
import logging
import math
import sqlite3
import time
import tomllib
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .provider import ProviderError, QuestionBatch, QuestionsProvider, SOURCES
from .store import DigestStore


POLICY = tomllib.loads((Path(__file__).parent / "policy.toml").read_text(encoding="utf-8"))
LOGGER = logging.getLogger(__name__)
HOURS = (0, 6, 12, 18)
RETRY_SECONDS = 300
try:
    OMSK = ZoneInfo("Asia/Omsk")
except ZoneInfoNotFoundError:
    # Windows Python often lacks IANA tzdata; Omsk has UTC+06 in this schedule.
    OMSK = timezone(timedelta(hours=6), "Asia/Omsk")


def current_slot(now: float) -> float:
    local = datetime.fromtimestamp(now, OMSK)
    hour = max(hour for hour in HOURS if hour <= local.hour)
    return local.replace(hour=hour, minute=0, second=0, microsecond=0).timestamp()


def next_slot(now: float) -> float:
    local = datetime.fromtimestamp(now, OMSK)
    for hour in HOURS:
        candidate = local.replace(hour=hour, minute=0, second=0, microsecond=0)
        if candidate.timestamp() > now:
            return candidate.timestamp()
    return (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _check_batch(batch: QuestionBatch, source: str, start: int, end: int) -> None:
    policy = POLICY["output_policy"]
    if (not isinstance(batch, QuestionBatch) or not isinstance(batch.questions, list)
            or len(batch.questions) > policy["max_questions_per_source"] or type(batch.partial) is not bool):
        raise ValueError("Output policy rejected the question batch")
    for q in batch.questions:
        if (not isinstance(q, dict) or q.get("source") != source
                or type(q.get("question_id")) is not int or q["question_id"] <= 0
                or not isinstance(q.get("title"), str) or not q["title"]
                or len(q["title"]) > policy["max_title_length"]
                or q.get("url") != f"https://{source}.stackexchange.com/questions/{q['question_id']}"
                or type(q.get("answer_count")) is not int or q["answer_count"] < 0
                or not isinstance(q.get("tags"), list)
                or len(q["tags"]) > policy["max_tags_per_question"]
                or any(not isinstance(tag, str) or not tag or len(tag) > 40 for tag in q["tags"])
                or type(q.get("created_at")) is not int or not start <= q["created_at"] <= end
                or not isinstance(q.get("excerpt"), str)
                or len(q["excerpt"]) > policy["max_excerpt_length"]):
            raise ValueError("Output policy rejected a question")


def _digest(run_id: int, start: int, end: int, questions: list[dict[str, Any]],
            sources: list[str], source_status: dict[str, str], source_errors: dict[str, str],
            generated: float) -> dict[str, Any]:
    unique = {(q["source"], q["question_id"]): q for q in questions}
    ordered = sorted(unique.values(), key=lambda q: (sources.index(q["source"]), -q["created_at"], q["question_id"]))
    counts = {source: sum(q["source"] == source for q in ordered) for source in sources}
    tags = Counter(tag for q in ordered for tag in set(q["tags"]))
    top = [{"tag": tag, "count": count} for tag, count in sorted(tags.items(), key=lambda item: (-item[1], item[0]))[:10]]
    highlights = [{"source": source, "title": q["title"], "url": q["url"]}
                  for source in sources for q in [item for item in ordered if item["source"] == source][:3]]
    unanswered = sum(q["answer_count"] == 0 for q in ordered)
    partial = any(status != "ok" for status in source_status.values())
    lines = ["Сводка вопросов по компьютерной графике и арту.",
             f"Вопросов за 24 часа: {len(ordered)}. Без ответов: {unanswered}."]
    lines.extend(f"{source}: {counts[source]} ({source_status[source]})." for source in sources)
    if top:
        lines.append("Популярные теги: " + ", ".join(f"{item['tag']} ({item['count']})" for item in top) + ".")
    if highlights:
        lines.append("Заметные вопросы:\n" + "\n".join(f"{h['source']}: {h['title']} — {h['url']}" for h in highlights))
    if partial:
        lines.append("Сводка неполная: часть источников или вопросов недоступна.")
    return {"run_id": run_id, "window_start": start, "window_end": end,
            "question_count": len(ordered), "unanswered_count": unanswered,
            "source_counts": counts, "source_status": source_status, "source_errors": source_errors,
            "top_tags": top, "highlights": highlights, "questions": ordered,
            "partial": partial, "text": "\n\n".join(lines), "generated_at": generated}


class DigestService:
    def __init__(self, store: DigestStore, provider: QuestionsProvider | None = None,
                 clock: Callable[[], float] = time.time):
        self.store = store
        self.provider = provider or QuestionsProvider(clock=clock)
        self.clock = clock
        self._lock = asyncio.Lock()
        now = self.clock()
        self.store.recover(now, current_slot(now))

    async def configure(self, sources: list[str] | None = None) -> dict[str, Any]:
        chosen = list(SOURCES) if sources is None else sources
        if (not isinstance(chosen, list) or not chosen or len(chosen) > len(SOURCES)
                or any(not isinstance(source, str) or source not in SOURCES for source in chosen)
                or len(set(chosen)) != len(chosen)):
            raise ValueError("Sources must be a nonempty unique subset of the allowlist")
        async with self._lock:
            slot = current_slot(self.clock())
            due = next_slot(self.clock()) if self.store.has_completed_slot(slot) else slot
            self.store.configure(chosen, due)
            return self.store.state()

    async def pause(self) -> dict[str, Any]:
        async with self._lock:
            self.store.pause()
            return self.store.state()

    async def state(self) -> dict[str, Any]:
        async with self._lock:
            return self.store.state()

    async def list_digests(self, after_run_id: int = 0, limit: int = 20) -> dict[str, Any]:
        if type(after_run_id) is not int or after_run_id < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid digest cursor or page limit")
        async with self._lock:
            return self.store.list_digests(after_run_id, limit)

    async def get_digest(self, run_id: int) -> dict[str, Any]:
        if type(run_id) is not int or run_id <= 0:
            raise ValueError("Invalid digest run ID")
        async with self._lock:
            digest = self.store.get_digest(run_id)
            if digest is None:
                raise ValueError("Digest not found")
            return {"digest": digest}

    async def collect(self) -> dict[str, Any]:
        async with self._lock:
            return await self._collect(scheduled=False, slot=None)

    async def run_due(self) -> dict[str, Any]:
        async with self._lock:
            state = self.store.state()
            schedule = state["schedule"]
            now = self.clock()
            if not schedule["enabled"] or schedule["next_due"] is None or schedule["next_due"] > now:
                return {**state, "executed": False}
            if schedule["backoff_until"] and schedule["backoff_until"] > now:
                return {**state, "executed": False}
            slot = current_slot(now)
            if self.store.has_completed_slot(slot):
                self.store.defer(next_slot(now), schedule["backoff_until"])
                return {**self.store.state(), "executed": False}
            return await self._collect(scheduled=True, slot=slot)

    async def _collect(self, *, scheduled: bool, slot: float | None) -> dict[str, Any]:
        schedule = self.store.schedule()
        now = self.clock()
        if schedule["backoff_until"] and schedule["backoff_until"] > now:
            return {**self.store.state(), "executed": False}
        end = int(now)
        start = end - 86400
        run_id = self.store.start_run(now, start, end, slot)
        questions: list[dict[str, Any]] = []
        status: dict[str, str] = {}
        errors: dict[str, str] = {}
        quota: int | None = None
        backoff_until: float | None = None
        valid_sources = 0
        output_rejected = False
        for source in schedule["sources"]:
            if backoff_until and backoff_until > self.clock():
                status[source] = "skipped_backoff"
                continue
            batch: QuestionBatch | None = None
            try:
                batch = await self.provider.collect(start, end, source)
                backoff_until = self._remember_backoff(
                    backoff_until, batch.backoff_until if isinstance(batch, QuestionBatch) else None)
                _check_batch(batch, source, start, end)
                questions.extend(batch.questions)
                valid_sources += 1
                status[source] = "partial" if batch.partial else "ok"
                quota = batch.quota_remaining
            except ProviderError as exc:
                status[source] = "error"
                errors[source] = str(exc)[:200]
                backoff_until = self._remember_backoff(backoff_until, exc.backoff_until)
            except (ValueError, TypeError, KeyError) as exc:
                status[source] = "error"
                errors[source] = str(exc)[:200]
                output_rejected = True
                if isinstance(batch, QuestionBatch):
                    backoff_until = self._remember_backoff(backoff_until, batch.backoff_until)
            except sqlite3.Error:
                # A failed durable backoff write must stop all /questions calls.
                raise
            except Exception:
                LOGGER.exception("Unexpected digest source failure: %s", source)
                status[source] = "error"
                errors[source] = "Unexpected source failure"
        finished = self.clock()
        retry_due = max(finished + RETRY_SECONDS, backoff_until or 0)
        if valid_sources:
            digest = _digest(run_id, start, end, questions, schedule["sources"], status, errors, finished)
            retryable = any(value in ("error", "skipped_backoff") for value in status.values())
            self.store.finish_success(run_id, finished, digest, digest["questions"], quota,
                                      retryable=retryable,
                                      next_due=retry_due if retryable else next_slot(finished),
                                      backoff_until=backoff_until, scheduled=scheduled)
        else:
            self.store.finish_error(run_id, finished, "All digest sources failed",
                                    "rejected" if output_rejected else "not_checked",
                                    next_due=retry_due, backoff_until=backoff_until,
                                    scheduled=scheduled)
        return {**self.store.state(), "executed": True}

    def _remember_backoff(self, current: float | None, candidate: float | None) -> float | None:
        if (type(candidate) not in (int, float) or not math.isfinite(candidate)
                or candidate <= self.clock()):
            return current
        until = max(current or 0, candidate)
        self.store.record_backoff(until)
        return until

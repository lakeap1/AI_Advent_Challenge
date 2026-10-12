"""Bounded Stack Exchange question retrieval for the configured sites."""

from __future__ import annotations

import html
import json
import re
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Callable

import httpx

API_URL = "https://api.stackexchange.com/2.3/questions"
SOURCES = ("blender", "computergraphics", "graphicdesign")
MAX_RESPONSE_BYTES = 768 * 1024
MAX_EXCERPT = 500
HEADERS = {"User-Agent": "AI-Advent-Graphics-Digest/2.0", "Accept-Encoding": "identity"}


class ProviderError(Exception):
    def __init__(self, message: str, *, backoff_until: float | None = None):
        super().__init__(message)
        self.backoff_until = backoff_until


@dataclass(frozen=True)
class QuestionBatch:
    questions: list[dict[str, Any]]
    partial: bool
    quota_remaining: int | None
    backoff_until: float | None


class _PlainText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.suppressed = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self.suppressed += 1
        elif tag in ("p", "br", "li", "div"):
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self.suppressed:
            self.suppressed -= 1
        elif tag in ("p", "li", "div"):
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self.suppressed:
            self.parts.append(data)


def _plain(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        raise ProviderError("Stack Exchange returned invalid text")
    parser = _PlainText()
    parser.feed(value)
    result = re.sub(r"\s+", " ", html.unescape("".join(parser.parts))).strip()
    result = "".join(char for char in result if ord(char) >= 32)
    return result[:limit]


class QuestionsProvider:
    def __init__(self, client_factory: Callable[[], httpx.AsyncClient] | None = None,
                 clock: Callable[[], float] = time.time):
        self._client_factory = client_factory or (lambda: httpx.AsyncClient(
            timeout=httpx.Timeout(18.0, connect=5.0), follow_redirects=False, headers=HEADERS))
        self._clock = clock

    async def collect(self, window_start: int, window_end: int, source: str) -> QuestionBatch:
        if source not in SOURCES:
            raise ValueError("Unknown Stack Exchange source")
        params: dict[str, Any] = {"site": source, "sort": "creation", "order": "desc",
                                  "fromdate": window_start, "todate": window_end,
                                  "page": 1, "pagesize": 100, "filter": "withbody"}
        header_backoff_until: float | None = None
        try:
            async with self._client_factory() as client:
                async with client.stream("GET", API_URL, params=params) as response:
                    status = response.status_code
                    retry_after = response.headers.get("retry-after", "")
                    if retry_after.isascii() and retry_after.isdecimal():
                        seconds = int(retry_after)
                        if 0 < seconds <= 86400:
                            header_backoff_until = self._clock() + seconds
                    chunks = bytearray()
                    async for chunk in response.aiter_bytes():
                        chunks.extend(chunk)
                        if len(chunks) > MAX_RESPONSE_BYTES:
                            raise ProviderError("Stack Exchange response exceeded size limit",
                                                backoff_until=header_backoff_until)
        except httpx.TimeoutException as exc:
            raise ProviderError("Stack Exchange timed out") from exc
        except httpx.RequestError as exc:
            raise ProviderError("Stack Exchange unavailable") from exc
        try:
            payload = json.loads(chunks)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            if status != 200:
                raise ProviderError(f"Stack Exchange returned HTTP {status}",
                                    backoff_until=header_backoff_until) from exc
            raise ProviderError("Stack Exchange returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise ProviderError(f"Stack Exchange returned HTTP {status}" if status != 200
                                else "Stack Exchange returned invalid data",
                                backoff_until=header_backoff_until)
        backoff = payload.get("backoff")
        if backoff is not None and (type(backoff) is not int or not 0 <= backoff <= 86400):
            raise ProviderError("Stack Exchange returned invalid backoff", backoff_until=header_backoff_until)
        until = max(header_backoff_until or 0, self._clock() + backoff if backoff else 0) or None
        if status != 200:
            raise ProviderError(f"Stack Exchange returned HTTP {status}", backoff_until=until)
        if "error_id" in payload or "error_message" in payload:
            raise ProviderError("Stack Exchange reported an error", backoff_until=until)
        items = payload.get("items")
        has_more = payload.get("has_more")
        if not isinstance(items, list) or type(has_more) is not bool or len(items) > 100:
            raise ProviderError("Stack Exchange returned invalid questions", backoff_until=until)
        quota = payload.get("quota_remaining")
        if quota is not None and (type(quota) is not int or quota < 0):
            raise ProviderError("Stack Exchange returned invalid quota", backoff_until=until)
        questions: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                raise ProviderError("Stack Exchange returned invalid question", backoff_until=until)
            qid, created, answers, tags = (item.get("question_id"), item.get("creation_date"),
                                            item.get("answer_count"), item.get("tags"))
            if (type(qid) is not int or qid <= 0 or type(created) is not int
                    or not window_start <= created <= window_end or type(answers) is not int
                    or answers < 0 or not isinstance(tags, list) or len(tags) > 20
                    or any(not isinstance(t, str) or not t or len(t) > 40 for t in tags)):
                raise ProviderError("Stack Exchange returned invalid question", backoff_until=until)
            title = _plain(item.get("title"), 240)
            if not title:
                raise ProviderError("Stack Exchange returned invalid title", backoff_until=until)
            excerpt = _plain(item.get("body"), MAX_EXCERPT)
            questions.append({"source": source, "question_id": qid, "title": title,
                              "url": f"https://{source}.stackexchange.com/questions/{qid}",
                              "answer_count": answers, "tags": tags, "created_at": created,
                              "excerpt": excerpt})
        return QuestionBatch(questions, has_more, quota, until)

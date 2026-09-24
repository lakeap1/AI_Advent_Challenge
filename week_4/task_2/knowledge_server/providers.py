"""Read-only, bounded adapters for Wikipedia and Stack Exchange APIs."""

from __future__ import annotations

import asyncio
import html
import json
import re
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.parse import quote, urlsplit

import httpx


MAX_QUERY_LENGTH = 256
MAX_RESULTS = 3
MAX_RESPONSE_BYTES = 256 * 1024
MAX_EXCERPT_LENGTH = 1200
LANGUAGES = frozenset({"en", "ru"})
COMMUNITIES = frozenset({"blender", "computergraphics", "gamedev"})
HEADERS = {
    "User-Agent": "AI-Advent-Graphics-Knowledge/1.0 (https://github.com/lakeap1/AI_Advent_Challenge) httpx/0.28.1",
    "Accept-Encoding": "identity",
}


class ProviderError(Exception):
    """A rejected request or an unavailable external source."""


class _PlainText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.ignored += 1
        elif tag in {"br", "p", "div", "li", "blockquote", "pre"}:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self.ignored:
            self.ignored -= 1
        elif tag in {"p", "div", "li", "blockquote", "pre"}:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self.ignored:
            self.parts.append(data)


def _plain(value: Any, limit: int) -> str:
    parser = _PlainText()
    parser.feed(str(value or ""))
    parser.close()
    return re.sub(r"\s+", " ", html.unescape("".join(parser.parts))).strip()[:limit]


def _query(value: Any) -> str:
    if not isinstance(value, str):
        raise ProviderError("query must be a string")
    clean = value.strip()
    if not clean or len(clean) > MAX_QUERY_LENGTH or any(ord(char) < 32 for char in clean):
        raise ProviderError("query must contain 1–256 characters without control characters")
    return clean


def _choice(value: Any, allowed: frozenset[str], name: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ProviderError(f"{name} must be one of: {', '.join(sorted(allowed))}")
    return value


def _limit(value: Any) -> int:
    if type(value) is not int or not 1 <= value <= MAX_RESULTS:
        raise ProviderError("limit must be an integer from 1 to 3")
    return value


def _safe_url(value: Any, host: str) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.netloc != host or not parsed.path.startswith("/"):
        return None
    return value


def _date(value: Any) -> str | None:
    if type(value) is not int or value < 0:
        return None
    try:
        return datetime.fromtimestamp(value, timezone.utc).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


class KnowledgeProviders:
    def __init__(
        self,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client_factory = client_factory or (
            lambda: httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=3.0), follow_redirects=False, headers=HEADERS)
        )
        self._clock = clock
        self._stack_backoff_until = 0.0
        self._stack_lock = asyncio.Lock()

    async def _request_json(self, url: str, params: dict[str, Any], provider: str) -> dict[str, Any]:
        try:
            async with self._client_factory() as client:
                async with client.stream("GET", url, params=params) as response:
                    if response.status_code != 200:
                        raise ProviderError(f"{provider} API returned HTTP {response.status_code}")
                    chunks = bytearray()
                    async for chunk in response.aiter_bytes():
                        chunks.extend(chunk)
                        if len(chunks) > MAX_RESPONSE_BYTES:
                            raise ProviderError(f"{provider} API response exceeded {MAX_RESPONSE_BYTES} bytes")
        except httpx.TimeoutException as exc:
            raise ProviderError(f"{provider} API timed out") from exc
        except httpx.RequestError as exc:
            raise ProviderError(f"{provider} API unavailable") from exc
        try:
            payload = json.loads(chunks)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderError(f"{provider} API returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise ProviderError(f"{provider} API returned invalid data")
        return payload

    async def lookup_wikipedia(self, query: str, language: str = "en", limit: int = 3) -> dict[str, Any]:
        query = _query(query)
        language = _choice(language, LANGUAGES, "language")
        limit = _limit(limit)
        host = f"{language}.wikipedia.org"
        payload = await self._request_json(
            f"https://{host}/w/api.php",
            {
                "action": "query", "generator": "search", "gsrsearch": query,
                "gsrlimit": limit, "prop": "extracts|info", "exintro": "1",
                "explaintext": "1", "inprop": "url", "format": "json", "formatversion": "2",
            },
            "Wikipedia",
        )
        if "error" in payload:
            raise ProviderError("Wikipedia API reported an error")
        pages = payload.get("query", {}).get("pages", [])
        if not isinstance(pages, list):
            raise ProviderError("Wikipedia API returned invalid pages")
        sources = []
        for page in sorted((p for p in pages if isinstance(p, dict)), key=lambda p: p.get("index", 999999))[:limit]:
            title = _plain(page.get("title"), 160)
            if not title:
                continue
            url = _safe_url(page.get("fullurl"), host) or f"https://{host}/wiki/{quote(title.replace(' ', '_'))}"
            sources.append({"title": title, "url": url, "excerpt": _plain(page.get("extract"), MAX_EXCERPT_LENGTH)})
        return {
            "provider": "wikipedia", "query": query, "sources": sources,
            "metadata": {"language": language, "result_count": len(sources), "empty": not sources},
        }

    async def search_stackexchange(self, query: str, community: str = "blender", limit: int = 3) -> dict[str, Any]:
        query = _query(query)
        community = _choice(community, COMMUNITIES, "community")
        limit = _limit(limit)
        async with self._stack_lock:
            if self._clock() < self._stack_backoff_until:
                remaining = int(self._stack_backoff_until - self._clock() + 0.999)
                raise ProviderError(f"Stack Exchange backoff active for {remaining} seconds")
            search = await self._stack_request(
                "search/advanced",
                {"q": query, "site": community, "sort": "relevance", "answers": 1, "pagesize": limit, "filter": "withbody"},
            )
            questions = search.get("items", [])
            if not isinstance(questions, list):
                raise ProviderError("Stack Exchange API returned invalid questions")
            questions = [item for item in questions[:limit] if isinstance(item, dict) and type(item.get("question_id")) is int]
            if not questions:
                return self._stack_result(query, community, [], search)
            ids = ";".join(str(item["question_id"]) for item in questions)
            answers_payload = await self._stack_request(
                f"questions/{ids}/answers",
                {"site": community, "sort": "votes", "pagesize": 30, "filter": "withbody"},
            )
            answers = answers_payload.get("items", [])
            if not isinstance(answers, list):
                raise ProviderError("Stack Exchange API returned invalid answers")
            best: dict[int, dict[str, Any]] = {}
            for item in answers:
                if not isinstance(item, dict) or type(item.get("question_id")) is not int:
                    continue
                qid = item["question_id"]
                if qid not in best or item.get("score", 0) > best[qid].get("score", 0):
                    best[qid] = item
            host = f"{community}.stackexchange.com"
            sources = []
            for question in questions:
                qid = question["question_id"]
                answer = best.get(qid)
                if answer is None:
                    continue
                title = _plain(question.get("title"), 160)
                answer_text = _plain(answer.get("body"), MAX_EXCERPT_LENGTH)
                if not title or not answer_text:
                    continue
                answer_id = answer.get("answer_id")
                canonical = f"https://{host}/questions/{qid}"
                if type(answer_id) is int and answer_id > 0:
                    canonical = f"https://{host}/a/{answer_id}"
                source: dict[str, Any] = {
                    "title": title, "url": canonical, "excerpt": answer_text,
                    "question_id": qid,
                }
                owner = answer.get("owner")
                if isinstance(owner, dict):
                    author = _plain(owner.get("display_name"), 100)
                    if author:
                        source["author"] = author
                date = _date(answer.get("creation_date"))
                if date:
                    source["date"] = date
                if type(answer.get("score")) is int:
                    source["score"] = answer["score"]
                sources.append(source)
            return self._stack_result(query, community, sources, answers_payload)

    async def _stack_request(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        payload = await self._request_json(f"https://api.stackexchange.com/2.3/{path}", params, "Stack Exchange")
        backoff = payload.get("backoff")
        if type(backoff) is int and backoff > 0:
            self._stack_backoff_until = max(self._stack_backoff_until, self._clock() + backoff)
        if "error_id" in payload or "error_message" in payload:
            raise ProviderError("Stack Exchange API reported an error")
        if self._clock() < self._stack_backoff_until and path == "search/advanced":
            raise ProviderError("Stack Exchange API requested backoff before answer retrieval")
        return payload

    @staticmethod
    def _stack_result(query: str, community: str, sources: list[dict[str, Any]], payload: dict[str, Any]) -> dict[str, Any]:
        metadata: dict[str, Any] = {"community": community, "result_count": len(sources), "empty": not sources}
        if type(payload.get("quota_remaining")) is int:
            metadata["quota_remaining"] = payload["quota_remaining"]
        return {"provider": "stackexchange", "query": query, "sources": sources, "metadata": metadata}

"""Bounded MCP transport and normalization for model-selected references."""

import asyncio
import json
import math
import os
from urllib.parse import urlsplit


DEFAULT_URL = "http://127.0.0.1:8017/mcp"
TIMEOUT_SECONDS = 6


class RetrievalError(RuntimeError):
    """Selected source is unavailable or returned an invalid result."""


def _text(value, max_chars):
    if not isinstance(value, str):
        return ""
    return value.strip()[:max_chars]


def _safe_url(value, provider):
    if not isinstance(value, str) or len(value) > 2048:
        return ""
    try:
        parts = urlsplit(value)
    except ValueError:
        return ""
    host = (parts.hostname or "").lower()
    allowed = (host.endswith(".wikipedia.org") if provider == "wikipedia" else
               host == "stackexchange.com" or host.endswith(".stackexchange.com") or
               host == "stackoverflow.com" or host == "superuser.com")
    return value if parts.scheme == "https" and allowed and not parts.username and not parts.password else ""


def _small_metadata(value):
    if not isinstance(value, dict):
        return {}
    return {key[:80]: str(val)[:240] for key, val in list(value.items())[:12]
            if isinstance(key, str) and isinstance(val, (str, int, float, bool))}


def normalize_result(value, provider, query):
    if not isinstance(value, dict) or value.get("provider") != provider or value.get("query") != query:
        raise RetrievalError("Источник вернул неожиданный формат результата.")
    raw = value.get("sources")
    if not isinstance(raw, list) or len(raw) > 3:
        raise RetrievalError("Источник вернул слишком много материалов.")
    sources = []
    for item in raw:
        if not isinstance(item, dict):
            raise RetrievalError("Источник вернул повреждённый материал.")
        url = _safe_url(item.get("url"), provider)
        title = _text(item.get("title"), 240)
        if not url or not title:
            raise RetrievalError("Источник вернул материал без допустимой ссылки или заголовка.")
        source = dict(title=title, url=url, excerpt=_text(item.get("excerpt"), 1800))
        for key, limit in (("author", 120), ("date", 80)):
            if item.get(key) is not None:
                source[key] = _text(item[key], limit)
        if type(item.get("score")) in (int, float) and math.isfinite(item["score"]):
            source["score"] = item["score"]
        if isinstance(item.get("metadata"), dict):
            source["metadata"] = _small_metadata(item["metadata"])
        sources.append(source)
    # Metadata is provider data, not a control channel. Keep only a small record.
    metadata = _small_metadata(value.get("metadata"))
    return dict(provider=provider, query=query, sources=sources, metadata=metadata)


class MCPRetrievalClient:
    def __init__(self, url=None, timeout=TIMEOUT_SECONDS):
        self.url = url or os.getenv("KNOWLEDGE_MCP_URL", DEFAULT_URL)
        self.timeout = timeout

    def fetch(self, provider, query, language, community, *, limit=3):
        if type(limit) is not int or not 1 <= limit <= 3:
            raise RetrievalError("Недопустимый лимит выдачи.")
        return asyncio.run(self._fetch(provider, query, language, community, limit))

    async def _fetch(self, provider, query, language, community, limit):
        try:
            from mcp import ClientSession
            from mcp.client.streamable_http import streamable_http_client
            name = "lookup_wikipedia" if provider == "wikipedia" else "search_stackexchange"
            args = dict(query=query, limit=limit)
            args["language" if provider == "wikipedia" else "community"] = (
                language if provider == "wikipedia" else community)
            async with asyncio.timeout(self.timeout):
                async with streamable_http_client(self.url) as (read, write, *_):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        result = await session.call_tool(name, arguments=args)
            if result.is_error:
                raise RetrievalError("Выбранный источник сообщил об ошибке.")
            data = result.structured_content
            if data is None and len(result.content) == 1 and getattr(result.content[0], "type", None) == "text":
                data = json.loads(result.content[0].text)
            return normalize_result(data, provider, query)
        except RetrievalError:
            raise
        except Exception as exc:
            raise RetrievalError("Не удалось получить данные выбранного источника через MCP.") from exc

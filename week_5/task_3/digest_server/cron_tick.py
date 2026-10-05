"""Bounded MCP cron client: python -m digest_server.cron_tick."""

from __future__ import annotations

import asyncio
import json
import os
from urllib.parse import urlsplit

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def tick(url: str = "http://127.0.0.1:8018/mcp") -> dict:
    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1") or parsed.path != "/mcp" or parsed.username or parsed.password:
        raise ValueError("Digest cron MCP URL must be loopback /mcp")
    async with asyncio.timeout(120):
        async with streamable_http_client(url) as (read, write, *_):
            async with ClientSession(read, write, read_timeout_seconds=115) as session:
                await session.initialize()
                result = await session.call_tool("run_scheduled_digest", arguments={})
    if result.is_error or not isinstance(result.structured_content, dict):
        raise RuntimeError("Scheduled digest MCP call failed")
    payload = result.structured_content
    if type(payload.get("executed")) is not bool or not isinstance(payload.get("schedule"), dict):
        raise RuntimeError("Scheduled digest MCP returned invalid state")
    return payload


def main() -> None:
    result = asyncio.run(tick(os.environ.get("DIGEST_MCP_URL", "http://127.0.0.1:8018/mcp")))
    print(json.dumps({"executed": result["executed"], "next_due": result["schedule"].get("next_due")},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()

"""Loopback MCP server; cron invokes the due tool separately."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .service import DigestService
from .store import DigestStore, ProcessLock


service: DigestService | None = None


@asynccontextmanager
async def lifespan(_server: MCPServer) -> AsyncIterator[None]:
    global service
    data_dir = Path(os.environ.get("DIGEST_DATA_DIR", str(Path(__file__).parents[1] / "data" / "digest")))
    with ProcessLock(data_dir / "server.lock"):
        store = DigestStore(data_dir / "digest.sqlite3")
        service = DigestService(store)
        try:
            yield
        finally:
            service = None
            store.close()


mcp = MCPServer("graphics-scheduled-digest",
                description="Daily graphics and art publication with cron driven internal collection",
                lifespan=lifespan)


def _service() -> DigestService:
    if service is None:
        raise ToolError("Digest server is not ready")
    return service


@mcp.tool(description="Enable daily publication at 00:00 Asia/Omsk and internal collection at 00/06/12/18. Sources are an optional allowlisted subset.",
          structured_output=True)
async def configure_digest(sources: list[str] | None = None) -> dict[str, Any]:
    try:
        return await _service().configure(sources)
    except ValueError as exc:
        raise ToolError(str(exc)) from None


@mcp.tool(description="Pause future scheduled collections without deleting saved digests.", structured_output=True)
async def pause_digest() -> dict[str, Any]:
    return await _service().pause()


@mcp.tool(description="Read the fixed schedule, recent runs, and latest daily publication.", structured_output=True)
async def get_digest_state() -> dict[str, Any]:
    return await _service().state()


@mcp.tool(description="Get or create the daily publication for the last closed Asia/Omsk calendar day.", structured_output=True)
async def collect_digest() -> dict[str, Any]:
    return await _service().collect()


@mcp.tool(description="Publish the missing last closed day or run a due internal collection; safe for concurrent cron ticks.",
          structured_output=True)
async def run_scheduled_digest() -> dict[str, Any]:
    return await _service().run_due()


@mcp.tool(description="List daily publications after a run ID in ascending order.", structured_output=True)
async def list_digests(after_run_id: int = 0, limit: int = 20) -> dict[str, Any]:
    try:
        return await _service().list_digests(after_run_id, limit)
    except ValueError as exc:
        raise ToolError(str(exc)) from None


@mcp.tool(description="Get one saved daily digest with source materials by run ID.", structured_output=True)
async def get_digest(run_id: int) -> dict[str, Any]:
    try:
        return await _service().get_digest(run_id)
    except ValueError as exc:
        raise ToolError(str(exc)) from None


def main() -> None:
    port = int(os.environ.get("DIGEST_PORT", "8018"))
    if not 1 <= port <= 65535:
        raise ValueError("DIGEST_PORT must be 1..65535")
    mcp.run("streamable-http", host="127.0.0.1", port=port, streamable_http_path="/mcp",
            json_response=True, stateless_http=True, max_request_body_size=16 * 1024)


if __name__ == "__main__":
    main()

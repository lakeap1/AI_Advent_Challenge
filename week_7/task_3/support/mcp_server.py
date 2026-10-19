"""Standalone MCP stdio server. The launcher supplies the actor and private DB path."""

from __future__ import annotations

import json
import os
import re
import sys

import anyio
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from .domain import Actor
from .tickets import TicketStore
from .tools import TOOL_SCHEMAS, ToolGateway
from .service_bridge import binding_from_environment


def _trusted_configuration() -> ToolGateway:
    actor_id = os.environ.get("SUPPORT_ACTOR_ID", "")
    db_path = os.environ.get("SUPPORT_DB_PATH", "")
    if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", actor_id) is None or not db_path:
        raise ValueError("Trusted support actor and DB path are required")
    binding = os.environ.get("SUPPORT_SERVICE_BINDING")
    bridge = binding_from_environment(binding, actor_id) if binding is not None else None
    return ToolGateway(TicketStore(db_path), Actor(actor_id), service_bridge=bridge)


def build_server(gateway: ToolGateway) -> Server:
    server = Server("day33-support", version="0.1.0")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [types.Tool(name=name, description="Private support operation", inputSchema=schema)
                for name, schema in gateway.schemas.items()]

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict):
        result = gateway.call(name, arguments)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))],
            structuredContent=result,
            isError=not result["ok"],
        )

    return server


async def serve() -> None:
    gateway = _trusted_configuration()
    server = build_server(gateway)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> int:
    try:
        anyio.run(serve)
        return 0
    except Exception as error:
        print("Support MCP server unavailable: " + type(error).__name__, file=sys.stderr)
        return 1

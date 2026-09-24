"""Run the graphics knowledge MCP on loopback: python -m knowledge_server.server."""

from __future__ import annotations

from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .providers import KnowledgeProviders, ProviderError


providers = KnowledgeProviders()
mcp = MCPServer("graphics-knowledge", description="Public Wikipedia and Stack Exchange references for graphics and art questions")


@mcp.tool(description="Search English or Russian Wikipedia for short introductory reference extracts.", structured_output=True)
async def lookup_wikipedia(query: str, language: str = "en", limit: int = 3) -> dict[str, Any]:
    try:
        return await providers.lookup_wikipedia(query, language, limit)
    except ProviderError as exc:
        raise ToolError(str(exc)) from None


@mcp.tool(description="Search a graphics Stack Exchange community and return text from actual answers.", structured_output=True)
async def search_stackexchange(query: str, community: str = "blender", limit: int = 3) -> dict[str, Any]:
    try:
        return await providers.search_stackexchange(query, community, limit)
    except ProviderError as exc:
        raise ToolError(str(exc)) from None


def main() -> None:
    mcp.run(
        "streamable-http", host="127.0.0.1", port=8017, streamable_http_path="/mcp",
        json_response=True, stateless_http=True, max_request_body_size=16 * 1024,
    )


if __name__ == "__main__":
    main()

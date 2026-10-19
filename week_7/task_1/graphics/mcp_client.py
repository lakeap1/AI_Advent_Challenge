"""Single-tool MCP stdio client with validation before subprocess startup."""
import asyncio
from datetime import timedelta
import json
import os
from pathlib import Path
import sys


class MCPClient:
    def __init__(self, repo_root, timeout=12):
        self.repo_root = Path(repo_root).resolve()
        if not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("timeout must be positive")
        self.timeout = timeout

    def call(self, name, args):
        if name != "git_current_branch" or type(args) is not dict or args:
            raise ValueError("tool or arguments not allowed")
        return asyncio.run(self._call_stdio())

    async def _call_stdio(self):
        try:
            import anyio
            from mcp import ClientSession
            from mcp.client.stdio import StdioServerParameters, stdio_client
        except ImportError as error:
            raise RuntimeError("MCP SDK is unavailable") from error
        package_root = Path(__file__).resolve().parents[1]
        env = {key: os.environ[key] for key in ("PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC") if key in os.environ}
        env.update(GRAPHICS_REPO_ROOT=str(self.repo_root), PYTHONPATH=str(package_root), PYTHONIOENCODING="utf-8")
        parameters = StdioServerParameters(command=sys.executable, args=["-B", "-m", "graphics.mcp_server"],
                                            env=env, cwd=package_root)
        with anyio.fail_after(self.timeout):
            async with stdio_client(parameters) as (reader, writer):
                async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=self.timeout)) as session:
                    initialized = await session.initialize()
                    listed = await session.list_tools()
                    if {tool.name for tool in listed.tools} != {"git_current_branch"}:
                        raise RuntimeError("MCP server exposes unexpected tools")
                    result = await session.call_tool("git_current_branch", arguments={})
                    if result.isError:
                        raise RuntimeError("Git checkout context is unavailable")
                    data = result.structuredContent
                    if isinstance(data, dict) and isinstance(data.get("result"), dict):
                        data = data["result"]
                    if not isinstance(data, dict) or "branch" not in data or "commit" not in data:
                        if len(result.content) != 1 or not hasattr(result.content[0], "text"):
                            raise RuntimeError("MCP returned invalid Git context")
                        data = json.loads(result.content[0].text)
                    if not isinstance(data, dict) or not isinstance(data.get("branch"), str) or not data["branch"] or not isinstance(data.get("commit"), str):
                        raise RuntimeError("MCP returned invalid Git context")
                    return {"branch": data["branch"], "commit": data["commit"],
                            "tool": "git_current_branch", "protocol": "mcp-stdio",
                            "protocol_version": str(initialized.protocolVersion)}

    def project_context(self):
        return self.call("git_current_branch", {})

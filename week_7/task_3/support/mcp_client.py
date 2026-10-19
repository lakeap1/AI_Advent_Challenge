"""Bounded MCP SDK client for the fixed local support server."""

from __future__ import annotations

import tempfile
import os
import re
import sys
import json
from datetime import timedelta
from pathlib import Path

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from .service_bridge import validate_binding


class SupportMCPClient:
    def __init__(self, db_path: str | Path, actor_id: str, timeout: float = 15,
                 max_calls: int = 20, service_binding: dict | None = None):
        if service_binding is not None:
            service_binding = validate_binding(service_binding)
            actor_id = str(actor_id)
        if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", actor_id or "") is None:
            raise ValueError("Trusted actor is required")
        if not isinstance(timeout, (float, int)) or isinstance(timeout, bool) or timeout <= 0:
            raise ValueError("Timeout must be positive")
        if not isinstance(max_calls, int) or isinstance(max_calls, bool) or max_calls < 1:
            raise ValueError("Call budget must be positive")
        self.db_path = Path(db_path).resolve()
        self.actor_id = actor_id
        self.timeout = float(timeout)
        self.max_calls = max_calls
        self.calls = 0
        self.evidence: list[dict] = []
        self._stderr = ""
        self._cwd = Path(__file__).resolve().parents[1]
        self.service_binding = service_binding

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def _parameters(self) -> StdioServerParameters:
        allowed = ("SystemRoot", "WINDIR", "PATH", "TEMP", "TMP", "LANG")
        environment = {key: os.environ[key] for key in allowed if key in os.environ}
        environment.update({
            "SUPPORT_DB_PATH": str(self.db_path),
            "SUPPORT_ACTOR_ID": self.actor_id,
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        if self.service_binding is not None:
            environment["SUPPORT_SERVICE_BINDING"] = json.dumps(
                self.service_binding, ensure_ascii=False, separators=(",", ":"))
        return StdioServerParameters(
            command=sys.executable,
            args=["-B", "-m", "support", "serve"],
            cwd=self._cwd,
            env=environment,
        )

    async def _exchange(self, name: str | None, arguments: dict | None):
        with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as error_output:
            try:
                with anyio.fail_after(self.timeout):
                    async with stdio_client(self._parameters(), errlog=error_output) as (read, write):
                        async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=self.timeout)) as session:
                            initialized = await session.initialize()
                            self.evidence.append({"event": "initialize", "protocol_version": initialized.protocolVersion})
                            listed = await session.list_tools()
                            names = [tool.name for tool in listed.tools]
                            self.evidence.append({"event": "list_tools", "names": names})
                            if name is None:
                                return names
                            result = await session.call_tool(name, arguments, read_timeout_seconds=timedelta(seconds=self.timeout))
                            payload = result.structuredContent
                            if not isinstance(payload, dict) or payload.get("ok") is not (not bool(result.isError)):
                                raise RuntimeError("Invalid support MCP response")
                            self.evidence.append({"event": "call_tool", "name": name, "is_error": bool(result.isError), "code": payload.get("error", {}).get("code")})
                            return payload
            finally:
                error_output.seek(0)
                self._stderr += error_output.read()
    def list_tools(self) -> list[str]:
        try:
            return anyio.run(self._exchange, None, None)
        except TimeoutError as error:
            raise TimeoutError("Support MCP deadline exceeded") from error

    def call(self, name: str, args: dict) -> dict:
        if self.calls >= self.max_calls:
            raise RuntimeError("Support MCP call budget exhausted")
        self.calls += 1
        try:
            return anyio.run(self._exchange, name, args)
        except TimeoutError as error:
            raise TimeoutError("Support MCP deadline exceeded") from error

"""Opt-in integration check against the actual public MCP endpoint."""

import asyncio
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
from unittest.mock import patch

import httpx2

import client


async def check():
    messages = []

    async def observe(request):
        if request.method == "POST":
            message = json.loads(request.content)
            # Refuse unexpected operations before sending anything to the server.
            if message.get("method") not in {"initialize", "notifications/initialized", "tools/list"}:
                raise AssertionError(f"Unexpected MCP method: {message.get('method')}")
            messages.append(message)

    original_transport = client.streamable_http_client
    output = io.StringIO()
    async with httpx2.AsyncClient(event_hooks={"request": [observe]}) as http:
        # Real HTTP client and real server; this injects an observer, not responses.
        with patch.object(client, "streamable_http_client", lambda url: original_transport(url, http_client=http)):
            with redirect_stdout(output):
                result = await client.discover(client.DEFAULT_URL, 30)
    methods = [message["method"] for message in messages]
    assert methods[:2] == ["initialize", "notifications/initialized"], methods
    assert methods[2:] and all(method == "tools/list" for method in methods[2:]), methods
    names = [tool["name"] for page in result["pages"] for tool in page["tools"]]
    assert names, "Acceptance requires a public server with tools"
    rendered_names = [line.split(". ", 1)[1] for line in output.getvalue().splitlines() if line[:1].isdigit()]
    assert rendered_names == names, (rendered_names, names)
    report = {"methods": methods, "names_match": True, "stdout": output.getvalue(), "response": result}
    destination = Path(__file__).parent / "docs" / "live-check.json"
    destination.parent.mkdir(exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(output.getvalue(), end="")
    print("Проверено: " + " → ".join(methods))
    print("Имена в выводе совпадают с ответом сервера. tools/call не отправлялся.")


if __name__ == "__main__":
    asyncio.run(check())

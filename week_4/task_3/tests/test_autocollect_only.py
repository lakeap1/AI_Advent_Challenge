"""The deployed MCP entry point must not enable background model calls."""

import asyncio

from digest_server import server


def test_server_collects_without_analyzer_even_if_a_key_is_present(tmp_path, monkeypatch):
    monkeypatch.setenv("DIGEST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "unused-test-key")

    async def check():
        async with server.lifespan(None):
            assert server.service is not None
            assert server.service.analyzer is None
            state = await server.service.state()
            assert state["analysis_summary"]["api_requests"] == 0
            assert not any(key.startswith("analysis_") for key in state["schedule"])
        assert server.service is None

    asyncio.run(check())

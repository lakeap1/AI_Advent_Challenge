"""Three production stdio registrations and qualified routing over MCP."""
import asyncio
import os

import pytest
from mcp import ClientSession
from mcp.client.stdio import stdio_client

from orchestration.router import OrchestrationRouter, server_specs, OrchestrationError
from orchestration.store import OrchestrationStore


def test_three_process_catalog_and_server_guard(tmp_path):
    store = OrchestrationStore(tmp_path)
    run = store.create(1, 2, 1, 'Normal mapping', 5)
    async def execute():
        from contextlib import AsyncExitStack
        async with asyncio.timeout(35):
            async with AsyncExitStack() as stack:
                sessions = {}
                infos = {}
                for role, spec in server_specs(tmp_path, run['id']).items():
                    read, write = await stack.enter_async_context(stdio_client(spec))
                    session = await stack.enter_async_context(ClientSession(read, write))
                    info = await session.initialize()
                    sessions[role] = session
                    infos[role] = info.server_info.model_dump(mode='json')
                router = OrchestrationRouter(sessions, server_info=infos)
                tools = await router.discover()
                assert {t['name'] for t in tools} == {
                    'research__lookup_wikipedia','research__search_stackexchange',
                    'processing__compare_sources','processing__prepare_report',
                    'library__save_report','library__read_report'}
                catalog = router.catalog
                assert len(catalog) == 3
                assert len({row['session_id'] for row in catalog}) == 3
                with pytest.raises(OrchestrationError):
                    await router.call('library__save_report', {'report_id':'foreign'})
                with pytest.raises(OrchestrationError):
                    await router.call('library__read_report', {'report_id':'foreign'})
    asyncio.run(execute())

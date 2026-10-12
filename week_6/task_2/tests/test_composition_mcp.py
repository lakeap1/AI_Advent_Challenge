"""Real stdio MCP lifecycle and tool calls; external search/model are test fixtures."""
import asyncio
import json
import os
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import pytest

from agent.composition import CompositionAgent
from composition.store import CompositionStore


@pytest.mark.parametrize('refusal', [False, True])
def test_real_mcp_order_data_and_error_stop(tmp_path, refusal):
    store = CompositionStore(tmp_path)
    agent = CompositionAgent(store)
    run = agent.create(1, 2, 'Как проверить швы normal map?', 'normal map seams')

    async def execute():
        server = StdioServerParameters(command=sys.executable,
            args=[str(Path(__file__).with_name('mcp_fixture_server.py'))],
            env={**os.environ, 'TEST_COMPOSITION_DIR': str(tmp_path), 'TEST_REFUSAL': '1' if refusal else '0', 'PYTHONIOENCODING':'utf-8'})
        async with asyncio.timeout(30):
            async with stdio_client(server) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    await agent.run_session(run['id'], session)
    asyncio.run(execute())
    result = store.get(run['id'])
    observed = [json.loads(s) for s in (tmp_path / 'wire-observations.jsonl').read_text(encoding='utf-8').splitlines()]
    assert len(result['catalog']) == 3
    assert all(t['description'] and t['inputSchema'] and t['outputSchema'] for t in result['catalog'])
    assert observed[1]['arguments'] == result['materials']
    assert result['call']['usage']['total_tokens'] == 120
    if refusal:
        assert [r['step'] for r in observed] == ['search', 'summarize']
        assert result['stage'] == 'summarize' and result['status'] == 'error'
        assert result['summary'] is None and result['saved'] is None
        assert not list(store.results.glob('*.txt'))
    else:
        assert [r['step'] for r in observed] == ['search', 'summarize', 'save']
        assert observed[2]['arguments'] == result['summary']
        assert result['status'] == 'success'
        assert (store.results / result['saved']['filename']).read_text(encoding='utf-8') == result['summary']['content'] == result['saved']['content']

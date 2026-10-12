"""Explicit live check of the remote MCP and both public APIs; no LLM calls."""

import asyncio
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def check():
    load_dotenv(Path(__file__).with_name('.env'))
    url = os.getenv('KNOWLEDGE_MCP_URL', 'http://127.0.0.1:8017/mcp')
    async with asyncio.timeout(60):
        async with streamable_http_client(url) as (read, write, *_):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                catalog = await session.list_tools()
                names = {tool.name for tool in catalog.tools}
                assert {'lookup_wikipedia', 'search_stackexchange'} <= names, names
                report = {'server': initialized.server_info.name, 'tools': sorted(names), 'results': []}
                for name, arguments in [
                    ('lookup_wikipedia', {'query': 'normal mapping', 'language': 'en', 'limit': 2}),
                    ('search_stackexchange', {'query': 'normal map baking seams', 'community': 'blender', 'limit': 2}),
                ]:
                    result = await session.call_tool(name, arguments=arguments)
                    if result.is_error:
                        raise RuntimeError(f'{name}: {result.content}')
                    data = result.structured_content
                    assert isinstance(data, dict) and data.get('sources'), f'{name}: empty response'
                    report['results'].append(data)
                print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    asyncio.run(check())

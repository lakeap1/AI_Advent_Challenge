"""Standalone MCP client, independent of Flask and application databases."""
import argparse
import asyncio
import json
import os
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def call(url, tool, arguments):
    async with asyncio.timeout(120):
        async with streamable_http_client(url) as (read, write, *_):
            async with ClientSession(read, write, read_timeout_seconds=115) as client:
                await client.initialize()
                response = await client.call_tool(tool, arguments=arguments)
                if response.is_error:
                    raise RuntimeError('MCP tool rejected the request: ' + str(response.content))
                return response.structured_content


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('tool', choices=['get_digest_state','collect_digest','run_scheduled_digest','configure_digest','pause_digest','list_digests','get_digest'])
    parser.add_argument('--url', default=os.environ.get('DIGEST_MCP_URL','http://127.0.0.1:8018/mcp'))
    parser.add_argument('--run-id', type=int)
    parser.add_argument('--after', type=int, default=0)
    parser.add_argument('--limit', type=int, default=20)
    parser.add_argument('--sources', nargs='+', choices=['blender','computergraphics','graphicdesign'])
    args = parser.parse_args()
    values = {}
    if args.tool == 'get_digest':
        if not args.run_id or args.run_id < 1:
            parser.error('get_digest requires positive --run-id')
        values = {'run_id':args.run_id}
    elif args.tool == 'list_digests':
        values = {'after_run_id':args.after,'limit':args.limit}
    elif args.tool == 'configure_digest' and args.sources:
        values = {'sources':args.sources}
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    print(json.dumps(asyncio.run(call(args.url,args.tool,values)),ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()

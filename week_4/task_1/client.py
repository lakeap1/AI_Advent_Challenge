"""List tools from a public MCP server. No tool execution or model calls."""

import argparse
import asyncio
import json
import math
import sys
from pathlib import Path

from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client

DEFAULT_URL = "https://learn.microsoft.com/api/mcp"


async def list_pages(session):
    pages = []
    cursor = None
    seen = set()
    while True:
        params = types.PaginatedRequestParams(cursor=cursor) if cursor else None
        page = await session.list_tools(params=params)
        pages.append(page)
        cursor = page.next_cursor
        if cursor is None:
            return pages
        if not cursor or cursor in seen:
            raise RuntimeError("сервер вернул пустой или повторяющийся cursor")
        seen.add(cursor)


async def discover(url, timeout):
    print(f"Сервер: {url}", flush=True)
    print("Транспорт: Streamable HTTP. Подключение и initialize...", flush=True)
    async with asyncio.timeout(timeout):
        async with streamable_http_client(url) as (read, write):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                print(
                    f"MCP initialize: OK — {initialized.server_info.name} "
                    f"{initialized.server_info.version}; протокол {initialized.protocol_version}",
                    flush=True,
                )
                if initialized.capabilities.tools is None:
                    raise RuntimeError("сервер не объявил поддержку tools")
                print("Запрос tools/list...", flush=True)
                pages = await list_pages(session)
    tools = [tool for page in pages for tool in page.tools]
    print(f"tools/list: OK. Получено инструментов: {len(tools)}", flush=True)
    for index, tool in enumerate(tools, 1):
        print(f"{index}. {tool.name}", flush=True)
    return {
        "url": url,
        "initialize": initialized.model_dump(mode="json", by_alias=True),
        "pages": [page.model_dump(mode="json", by_alias=True) for page in pages],
    }


def error_text(error):
    if isinstance(error, BaseExceptionGroup):
        return "; ".join(error_text(item) for item in error.exceptions)
    if isinstance(error, TimeoutError):
        return "превышено время ожидания сервера"
    return f"{type(error).__name__}: {error}"


def positive_timeout(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("таймаут должен быть конечным числом больше 0")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description="Получить инструменты публичного MCP-сервера.")
    parser.add_argument("--url", default=DEFAULT_URL, help="адрес Streamable HTTP MCP-сервера")
    parser.add_argument("--timeout", type=positive_timeout, default=30, help="общий таймаут, секунд (30)")
    parser.add_argument("--json-output", type=Path, help="сохранить ответы initialize и tools/list в JSON")
    args = parser.parse_args(argv)
    try:
        result = asyncio.run(discover(args.url, args.timeout))
    except KeyboardInterrupt:
        print("Остановлено пользователем.", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"Не удалось получить список MCP-инструментов: {error_text(error)}", file=sys.stderr)
        print("Проверьте адрес, сеть и доступность сервера.", file=sys.stderr)
        return 1
    if args.json_output:
        try:
            args.json_output.parent.mkdir(parents=True, exist_ok=True)
            args.json_output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as error:
            print(f"Список получен, но JSON не сохранён: {error}", file=sys.stderr)
            return 1
        print(f"Ответы сервера сохранены: {args.json_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

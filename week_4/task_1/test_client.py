"""Offline edge-case checks; the public-server check is check_live.py."""

import argparse
import asyncio
from contextlib import redirect_stderr, redirect_stdout
import io
import unittest
from unittest.mock import AsyncMock, patch

from mcp import types

import client


class PaginationTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_pages_and_cursor(self):
        first = types.ListToolsResult(tools=[types.Tool(name="one", input_schema={})], next_cursor="page-2")
        last = types.ListToolsResult(tools=[types.Tool(name="two", input_schema={})])
        session = AsyncMock()
        session.list_tools.side_effect = [first, last]
        result = await client.list_pages(session)
        self.assertEqual([tool.name for page in result for tool in page.tools], ["one", "two"])
        self.assertIsNone(session.list_tools.call_args_list[0].kwargs["params"])
        self.assertEqual(session.list_tools.call_args_list[1].kwargs["params"].cursor, "page-2")

    async def test_empty_list(self):
        session = AsyncMock()
        session.list_tools.return_value = types.ListToolsResult(tools=[])
        pages = await client.list_pages(session)
        self.assertEqual(pages[0].tools, [])
        session.list_tools.assert_awaited_once()

    async def test_repeated_cursor_stops(self):
        session = AsyncMock()
        session.list_tools.return_value = types.ListToolsResult(tools=[], next_cursor="repeat")
        with self.assertRaisesRegex(RuntimeError, "cursor"):
            await client.list_pages(session)
        self.assertEqual(session.list_tools.await_count, 2)


class ErrorTests(unittest.TestCase):
    def test_nested_error_is_readable(self):
        error = ExceptionGroup("transport", [ExceptionGroup("session", [ConnectionError("network down")])])
        self.assertIn("network down", client.error_text(error))

    def test_invalid_timeouts(self):
        for value in ("0", "-1", "nan", "inf"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                client.positive_timeout(value)

    def test_failure_exit_and_no_success(self):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(client, "discover", AsyncMock(side_effect=ConnectionError("network down"))):
            with redirect_stdout(out), redirect_stderr(err):
                code = client.main([])
        self.assertEqual(code, 1)
        self.assertIn("network down", err.getvalue())
        self.assertNotIn("tools/list: OK", out.getvalue())

    def test_timeout_exit(self):
        async def too_slow(*args):
            async with asyncio.timeout(0.01):
                await asyncio.sleep(1)

        err = io.StringIO()
        with patch.object(client, "discover", too_slow), redirect_stderr(err):
            self.assertEqual(client.main([]), 1)
        self.assertIn("время ожидания", err.getvalue())


if __name__ == "__main__":
    unittest.main()

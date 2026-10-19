import json
import anyio
from jsonschema import Draft202012Validator
from mcp import ClientSession
from mcp.client.stdio import stdio_client
from contextlib import asynccontextmanager
from unittest.mock import patch
import tempfile
import unittest
from pathlib import Path

from support.domain import Actor, SupportError
from support.tickets import TicketStore
from support.tools import ToolGateway, TOOL_SCHEMAS
from support.mcp_client import SupportMCPClient
from support.smoke import run_smoke


class MCPTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db = Path(temporary.name) / 'tickets.sqlite3'

    def test_gateway_rejects_forged_actor_and_unknown_tools(self):
        gateway = ToolGateway(TicketStore(self.db), Actor('alice'))
        self.assertEqual(set(TOOL_SCHEMAS), {'support_ticket_create', 'support_ticket_get', 'support_ticket_list', 'support_ticket_append', 'support_ticket_close', 'support_status'})
        create = {'title': 'Help', 'description': 'Synthetic request', 'request_key': 'k1'}
        for extra in ({'user_id': 'bob'}, {'db_path': 'foreign.sqlite3'}):
            result = gateway.call('support_ticket_create', create | extra)
            self.assertFalse(result['ok'])
            self.assertEqual(result['error']['code'], 'invalid_argument')
        self.assertEqual(gateway.call('other', {})['error']['code'], 'invalid_argument')
        self.assertEqual(gateway.call('support_ticket_receipt', {
            'tool': 'support_ticket_create', 'arguments': create})['error']['code'], 'invalid_argument')
        self.assertFalse(self.db.exists())

    def test_real_stdio_roundtrip_restart_and_owner_denial(self):
        create = {'title': 'Help', 'description': 'Synthetic request', 'request_key': 'k1'}
        with SupportMCPClient(self.db, 'alice') as client:
            self.assertIn('support_ticket_create', client.list_tools())
            self.assertEqual(set(client.list_tools()), set(TOOL_SCHEMAS))
            created = client.call('support_ticket_create', create)
            self.assertTrue(created['ok'], created)
            ticket_id = created['data']['ticket']['id']
            self.assertEqual(client.call('support_ticket_create', create), created)
            appended = client.call('support_ticket_append', {'ticket_id': ticket_id, 'message': 'Follow up', 'request_key': 'k2'})
            self.assertTrue(appended['ok'], appended)
            self.assertEqual(len(client.call('support_ticket_get', {'ticket_id': ticket_id})['data']['messages']), 2)
            self.assertTrue(client.call('support_ticket_close', {'ticket_id': ticket_id, 'request_key': 'k3'})['ok'])
            self.assertEqual(client.call('support_ticket_create', create | {'user_id': 'bob'})['error']['code'], 'invalid_argument')
            self.assertEqual(client.call('support_ticket_create', create | {'db_path': 'foreign'})['error']['code'], 'invalid_argument')
            self.assertEqual(client.call('support_ticket_list', {'limit': True})['error']['code'], 'invalid_argument')
            self.assertEqual(client.call('unknown_support_tool', {})['error']['code'], 'invalid_argument')
            self.assertIn('not listed, no validation will be performed', client._stderr)
            self.assertTrue(any(item['event'] == 'call_tool' and item['is_error'] and item['code'] == 'invalid_argument' for item in client.evidence))
            status = client.call('support_status', {})
            self.assertEqual(status['data']['storage']['status'], 'ok')
            self.assertEqual(status['data']['main_service']['status'], 'unknown')
            self.assertEqual(status['accounting'], {'llm_calls': 0, 'usage': None, 'cost_usd': None, 'reason': 'no_request'})
        with SupportMCPClient(self.db, 'alice') as restarted:
            self.assertEqual(restarted.call('support_ticket_create', create), created)
            self.assertEqual(restarted.call('support_ticket_get', {'ticket_id': ticket_id})['data']['status'], 'closed_by_user')
        with SupportMCPClient(self.db, 'bob') as bob:
            self.assertEqual(bob.call('support_ticket_get', {'ticket_id': ticket_id})['error']['code'], 'not_found')
            self.assertEqual(bob.call('support_ticket_list', {})['data'], [])
            self.assertEqual(bob.call('support_ticket_append', {'ticket_id': ticket_id, 'message': 'No', 'request_key': 'bob-1'})['error']['code'], 'not_found')
            self.assertEqual(bob.call('support_ticket_close', {'ticket_id': ticket_id, 'request_key': 'bob-2'})['error']['code'], 'not_found')

    def test_advertised_standard_schema_matches_trimmed_text_limits(self):
        async def discovered_schemas():
            params = SupportMCPClient(self.db, 'alice')._parameters()
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.list_tools()
                    return {tool.name: tool.inputSchema for tool in result.tools}

        schemas = anyio.run(discovered_schemas)
        cases = (
            ('support_ticket_create', 'title', 160, {'description': 'B', 'request_key': 'schema-1'}),
            ('support_ticket_create', 'description', 8000, {'title': 'A', 'request_key': 'schema-2'}),
            ('support_ticket_append', 'message', 8000, {'ticket_id': '0' * 32, 'request_key': 'schema-3'}),
        )
        for tool, field, maximum, other in cases:
            schema = schemas[tool]
            Draft202012Validator.check_schema(schema)
            validator = Draft202012Validator(schema)
            for value in ('A' * maximum, 'A' + ' ' * maximum, '\u00a0A\u3000', 'A\tB',
                          'A' + ' ' * (maximum - 2) + 'B'):
                with self.subTest(tool=tool, field=field, kind='accepted', length=len(value)):
                    self.assertTrue(validator.is_valid(other | {field: value}))
            for value in ('A' * (maximum + 1), 'A' + ' ' * (maximum - 1) + 'B',
                          ' \t\n\u00a0\u3000'):
                with self.subTest(tool=tool, field=field, kind='rejected', length=len(value)):
                    self.assertFalse(validator.is_valid(other | {field: value}))
    def test_discovered_key_and_id_reject_terminal_newline_like_gateway(self):
        async def discovered_schemas():
            params = SupportMCPClient(self.db, 'alice')._parameters()
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.list_tools()
                    return {tool.name: tool.inputSchema for tool in result.tools}

        schemas = anyio.run(discovered_schemas)
        gateway = ToolGateway(TicketStore(self.db), Actor('alice'))
        cases = (
            ('support_ticket_create', {'title': 'A', 'description': 'B', 'request_key': 'key\n'}),
            ('support_ticket_get', {'ticket_id': '0' * 32 + '\n'}),
        )
        for name, arguments in cases:
            with self.subTest(name=name):
                Draft202012Validator.check_schema(schemas[name])
                self.assertFalse(Draft202012Validator(schemas[name]).is_valid(arguments))
                result = gateway.call(name, arguments)
                self.assertFalse(result['ok'])
                self.assertEqual(result['error']['code'], 'invalid_argument')
        self.assertFalse(self.db.exists())
    def test_raw_mcp_text_limits_use_trimmed_length_and_preserve_original_replay_key(self):
        with SupportMCPClient(self.db, 'alice') as client:
            schema = TOOL_SCHEMAS['support_ticket_create']['properties']['title']
            self.assertNotIn('maxLength', schema)
            self.assertIn('after', schema['description'])
            padded = {'title': 'A' + ' ' * 160, 'description': 'B', 'request_key': 'trim-1'}
            created = client.call('support_ticket_create', padded)
            self.assertTrue(created['ok'], created)
            self.assertEqual(created['data']['ticket']['title'], 'A')
            conflict = client.call('support_ticket_create', {'title': 'A', 'description': 'B', 'request_key': 'trim-1'})
            self.assertEqual(conflict['error']['code'], 'idempotency_conflict')
            overlong = client.call('support_ticket_create', {'title': 'A' * 161, 'description': 'B', 'request_key': 'trim-2'})
            self.assertEqual(overlong['error']['code'], 'invalid_argument')
            self.assertEqual(len(client.call('support_ticket_list', {})['data']), 1)

    def test_missing_storage_status_reports_unavailable_and_does_not_create_db(self):
        with SupportMCPClient(self.db, 'alice') as client:
            status = client.call('support_status', {})
            self.assertTrue(status['ok'], status)
            self.assertEqual(status['data']['storage']['status'], 'unavailable')
            from datetime import datetime
            self.assertIsNotNone(datetime.fromisoformat(status['data']['storage']['checked_at']).utcoffset())
            for dependency in ('main_service', 'llm', 'rag'):
                self.assertEqual(status['data'][dependency]['status'], 'unknown')
            self.assertEqual(status['accounting']['llm_calls'], 0)
            self.assertFalse(client.evidence[-1]['is_error'])
        self.assertFalse(self.db.exists())
        self.assertEqual(list(self.db.parent.iterdir()), [])
    def test_status_only_degrades_expected_storage_health_failure(self):
        store = TicketStore(self.db)
        gateway = ToolGateway(store, Actor('alice'))
        with patch.object(store, 'health', side_effect=SupportError('invalid_argument', 'health rejected')):
            result = gateway.call('support_status', {})
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['code'], 'invalid_argument')
        unavailable = self.db.parent / 'missing-parent' / 'support.sqlite3'
        gateway = ToolGateway(TicketStore(unavailable), Actor('alice'))
        self.assertTrue(gateway.call('support_status', {})['ok'])
        failed = gateway.call('support_ticket_create', {
            'title': 'Help', 'description': 'Synthetic', 'request_key': 'failure'})
        self.assertFalse(failed['ok'])
        self.assertEqual(failed['error']['code'], 'storage_unavailable')
        self.assertFalse(unavailable.exists())

    def test_budget_and_invalid_configuration(self):
        with self.assertRaises(ValueError):
            SupportMCPClient(self.db, '')
        TicketStore(self.db).create(Actor('alice'), 'A', 'B', 'setup')
        with SupportMCPClient(self.db, 'alice', max_calls=1) as client:
            self.assertTrue(client.call('support_status', {})['ok'])
            self.assertTrue(any(item['event'] == 'initialize' for item in client.evidence))
            self.assertTrue(any(item['event'] == 'list_tools' for item in client.evidence))
            with self.assertRaises(RuntimeError):
                client.call('support_status', {})

    def test_expired_deadline_is_explicit_and_does_not_write(self):
        @asynccontextmanager
        async def stalled_transport(*_args, **_kwargs):
            await anyio.sleep(1)
            yield None, None

        with patch('support.mcp_client.stdio_client', stalled_transport):
            with SupportMCPClient(self.db, 'alice', timeout=0.001) as client:
                with self.assertRaises(TimeoutError):
                    client.call('support_ticket_create', {'title': 'Help', 'description': 'Synthetic', 'request_key': 'late'})
        self.assertFalse(self.db.exists())
    def test_smoke_refuses_existing_path_without_change(self):
        self.db.write_bytes(b'foreign')
        with self.assertRaises(FileExistsError):
            run_smoke(self.db)
        self.assertEqual(self.db.read_bytes(), b'foreign')

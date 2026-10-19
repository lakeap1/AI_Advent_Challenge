import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

import anyio
from jsonschema import Draft202012Validator
from mcp import ClientSession
from mcp.client.stdio import stdio_client

from support.mcp_client import SupportMCPClient
from support.domain import SupportError
from support.service_bridge import ServiceBridge
from support.tools import TOOL_SCHEMAS


APP_ROOT = Path(__file__).resolve().parents[2] / "task_1"
sys.path.insert(0, str(APP_ROOT))
from graphics.store import Store  # noqa: E402


class IntegratedMCPTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="day33-task1-")
        def cleanup():
            for attempt in range(30):
                try:
                    temporary.cleanup()
                    return
                except PermissionError:
                    if attempt == 29:
                        raise
                    time.sleep(0.1)
        self.addCleanup(cleanup)
        self.root = Path(temporary.name)
        self.main_db = self.root / "main.sqlite3"
        self.ticket_db = self.root / "tickets.sqlite3"
        self.store = Store(self.main_db)
        self.owner = self.store.create_user("owner", "long password")
        self.other = self.store.create_user("other", "other password")
        self.branch = self.store.create_chat(self.owner)["branch_id"]
        self.turn = self.store.begin_turn(self.owner, self.branch, "support")["id"]

    def binding(self, **changes):
        return {"app_root": str(APP_ROOT), "db_path": str(self.main_db),
                "branch_id": self.branch, "turn_id": self.turn,
                "explicit_budget": 4000} | changes

    def client(self, binding=None, actor=None):
        return SupportMCPClient(self.ticket_db, str(self.owner if actor is None else actor),
                                service_binding=self.binding() if binding is None else binding)

    def test_real_stdio_shared_profile_memory_and_replay(self):
        with self.client() as client:
            self.assertEqual(len(client.list_tools()), 12)
            self.assertEqual(set(TOOL_SCHEMAS), set(client.list_tools()) - {
                "support_user_get", "support_profile_update", "support_memory_set",
                "support_memory_delete", "support_diagnostics", "support_ticket_receipt"})
            profile = client.call("support_profile_update", {
                "changes": {"role": "QA", "level": "beginner"}, "request_key": "profile-1"})
            self.assertTrue(profile["ok"], profile)
            self.assertEqual(profile["integration_status"], "integrated")
            self.assertEqual(profile["data"]["receipt"]["changed_fields"], ["level", "role"])
            saved = client.call("support_memory_set", {
                "key": "format", "value": "short answers", "request_key": "memory-1"})
            self.assertTrue(saved["ok"], saved)
            self.assertEqual(client.call("support_memory_set", {
                "key": "format", "value": "short answers", "request_key": "memory-1"}), saved)
            context = client.call("support_user_get", {})
            self.assertEqual(context["data"]["profile"]["role"], "QA")
            self.assertEqual(context["data"]["memories"][0]["value"], "short answers")
            conflict = client.call("support_memory_set", {
                "key": "format", "value": "long answers", "request_key": "memory-1"})
            self.assertEqual(conflict["error"]["code"], "idempotency_conflict")
            deleted = client.call("support_memory_delete", {
                "memory_id": saved["data"]["receipt"]["memory_id"], "request_key": "delete-1"})
            self.assertTrue(deleted["ok"], deleted)
        self.assertEqual(Store(self.main_db).get_profile(self.owner)["level"], "beginner")
        self.assertEqual(Store(self.main_db).list_memories(self.owner), [])
        with self.client() as restarted:
            self.assertEqual(restarted.call("support_memory_delete", {
                "memory_id": saved["data"]["receipt"]["memory_id"], "request_key": "delete-1"}), deleted)

    def test_receipt_read_requires_original_owner_branch_turn_and_exact_arguments(self):
        args = {"title": "Private title", "description": "PRIVATE_TICKET_TEXT", "request_key": "recover-1"}
        self.store.record_support_action(self.owner, self.branch, self.turn, "recover-1",
                                         "support_ticket_create", "unknown")
        query = {"tool": "support_ticket_create", "arguments": args}
        with self.client() as client:
            created = client.call("support_ticket_create", args)
            self.assertTrue(created["ok"], created)
            recovered = client.call("support_ticket_receipt", query)
            self.assertTrue(recovered["ok"], recovered)
            self.assertEqual(recovered["data"], {"receipt": created["data"]["receipt"]})
            self.assertEqual(recovered["accounting"]["llm_calls"], 0)
            self.assertNotIn("PRIVATE_TICKET_TEXT", str(recovered))
            conflict = client.call("support_ticket_receipt", {**query, "arguments": {**args, "description": "changed"}})
            self.assertEqual(conflict["error"]["code"], "idempotency_conflict")
            for forged in ({**query, "user_id": self.other},
                           {**query, "arguments": {**args, "turn_id": self.turn}},
                           {**query, "tool": "support_profile_update"},
                           {**query, "tool": "support_ticket_close"}):
                result = client.call("support_ticket_receipt", forged)
                self.assertEqual(result["error"]["code"], "invalid_argument")
        with self.client(actor=self.other) as foreign:
            result = foreign.call("support_ticket_receipt", query)
            self.assertFalse(result["ok"])
            self.assertNotIn(created["data"]["receipt"]["ticket_id"], str(result))
        self.store.move_turn(self.turn, "input_check")
        self.store.end_turn(self.turn, "error")
        other_turn = self.store.begin_turn(self.owner, self.branch, "other")["id"]
        with self.client(binding=self.binding(turn_id=other_turn)) as wrong_turn:
            result = wrong_turn.call("support_ticket_receipt", query)
            self.assertEqual(result["error"]["code"], "not_found")
        other_branch = self.store.create_chat(self.owner)["branch_id"]
        other_turn = self.store.begin_turn(self.owner, other_branch, "other branch")["id"]
        self.store.record_support_action(self.owner, other_branch, other_turn, "recover-1",
                                         "support_ticket_create", "unknown")
        with self.client(binding=self.binding(branch_id=other_branch, turn_id=other_turn)) as wrong_branch:
            result = wrong_branch.call("support_ticket_receipt", query)
            self.assertTrue(result["ok"], result)
            self.assertIsNone(result["data"]["receipt"])

    def test_real_stdio_clears_profile_fields_without_erasing_memory(self):
        self.store.update_profile(self.owner, {"role": "QA", "level": "beginner", "goal": "materials"})
        self.store.remember(self.owner, "style", "short")
        args = {"changes": {"role": "", "level": ""}, "request_key": "clear-mcp"}
        with self.client() as client:
            cleared = client.call("support_profile_update", args)
            self.assertTrue(cleared["ok"], cleared)
            self.assertEqual(cleared["data"]["receipt"]["changed_fields"], ["level", "role"])
            self.assertEqual(client.call("support_user_get", {})["data"]["profile"], {"goal": "materials"})
        self.store.update_profile(self.owner, {"role": "artist"})
        with self.client() as restarted:
            self.assertEqual(restarted.call("support_profile_update", args), cleared)
            current = restarted.call("support_user_get", {})["data"]
            self.assertEqual(current["profile"], {"role": "artist", "goal": "materials"})
            self.assertEqual(current["memories"][0]["value"], "short")

    def test_forged_arguments_and_binding_do_not_expose_foreign_data(self):
        self.store.update_profile(self.owner, {"goal": "private goal"})
        with self.client() as client:
            for extra in ({"user_id": self.other}, {"branch_id": "other"}, {"db_path": "other"}):
                result = client.call("support_user_get", extra)
                self.assertEqual(result["error"]["code"], "invalid_argument")
        with self.client(actor=self.other) as foreign:
            result = foreign.call("support_user_get", {})
            self.assertFalse(result["ok"])
            self.assertNotIn("private goal", str(result))
        with self.client(binding=self.binding(branch_id="0" * 32)) as wrong_branch:
            result = wrong_branch.call("support_user_get", {})
            self.assertFalse(result["ok"])
            self.assertNotIn("private goal", str(result))

    def test_foreign_or_missing_service_database_is_never_created_or_migrated(self):
        missing = self.root / "missing.sqlite3"
        with self.assertRaises(Exception):
            ServiceBridge(self.binding(db_path=str(missing)), str(self.owner))
        self.assertFalse(missing.exists())
        foreign = self.root / "foreign.sqlite3"
        with closing(sqlite3.connect(foreign)) as db:
            db.execute("CREATE TABLE unrelated(secret TEXT)")
            db.execute("INSERT INTO unrelated VALUES('keep')")
            db.commit()
        before = foreign.read_bytes()
        with self.assertRaises(Exception):
            ServiceBridge(self.binding(db_path=str(foreign)), str(self.owner))
        self.assertEqual(foreign.read_bytes(), before)
        lookalike = self.root / "lookalike.sqlite3"
        with closing(sqlite3.connect(lookalike)) as db:
            db.executescript("""
                CREATE TABLE users(id INTEGER,profile TEXT);
                CREATE TABLE memories(id TEXT,user_id INTEGER,key TEXT,value TEXT);
                CREATE TABLE chats(id TEXT,user_id INTEGER);
                CREATE TABLE branches(id TEXT,user_id INTEGER,chat_id TEXT);
                CREATE TABLE messages(id TEXT,branch_id TEXT,user_id INTEGER);
                CREATE TABLE turns(id TEXT,branch_id TEXT,user_id INTEGER,status TEXT);
                CREATE TABLE calls(id TEXT,user_id INTEGER,branch_id TEXT,turn_id TEXT,status TEXT);
            """)
        before = lookalike.read_bytes()
        with self.assertRaises(SupportError) as failure:
            ServiceBridge(self.binding(db_path=str(lookalike)), str(self.owner))
        self.assertEqual(failure.exception.code, "storage_unavailable")
        self.assertEqual(lookalike.read_bytes(), before)

    def test_budget_error_does_not_mutate_profile_or_save_receipt(self):
        with self.client(binding=self.binding(explicit_budget=100)) as client:
            failure = client.call("support_profile_update", {
                "changes": {"goal": "x" * 100}, "request_key": "too-large"})
            self.assertEqual(failure["error"]["code"], "limit_exceeded")
        self.assertEqual(self.store.get_profile(self.owner), {})
        with closing(sqlite3.connect(self.main_db)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM support_operations").fetchone()[0], 0)

    def test_diagnostics_are_observed_private_and_do_not_call_provider(self):
        self.store.update_profile(self.owner, {"goal": "secret profile"})
        with self.client() as client:
            result = client.call("support_diagnostics", {})
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["data"]["main_storage"],
                             {"status": "ok", "scope": "main_store"})
            self.assertNotEqual(result["data"].get("main_service", {}).get("status"), "ok")
            self.assertEqual(result["data"]["llm"]["status"], "unknown")
            self.assertEqual(result["data"]["rag"]["status"], "unknown")
            self.assertEqual(result["integration_status"], "integrated")
            self.assertIn("checked_at", result["data"])
            self.assertEqual(result["accounting"]["llm_calls"], 0)
            self.assertNotIn("secret profile", str(result))
            self.assertNotIn("ZAI_API_KEY", str(result))

    def test_diagnostics_show_only_recent_owned_branch_outcomes(self):
        first = self.turn
        self.store.move_turn(first, "input_check")
        self.store.end_turn(first, "error")
        second = self.store.begin_turn(self.owner, self.branch, "private question")["id"]
        for state in ("input_check", "context", "sources", "draft"):
            self.store.move_turn(second, state)
        call_id = self.store.start_call(self.owner, self.branch, second, "generator", "glm-5.3-flash")
        self.store.finish_call(call_id, None, 0.0, status="not_sent", metadata={"api_request_sent": False})
        for state in ("program_check", "semantic_check"):
            self.store.move_turn(second, state)
        self.store.end_turn(second, "rejected")
        other_branch = self.store.create_chat(self.owner)["branch_id"]
        foreign_branch_turn = self.store.begin_turn(self.owner, other_branch, "other branch secret")["id"]
        self.store.move_turn(foreign_branch_turn, "input_check")
        self.store.end_turn(foreign_branch_turn, "error")
        foreign_user_branch = self.store.create_chat(self.other)["branch_id"]
        foreign_user_turn = self.store.begin_turn(self.other, foreign_user_branch, "foreign secret")["id"]
        self.store.move_turn(foreign_user_turn, "input_check")
        self.store.end_turn(foreign_user_turn, "error")
        self.turn = self.store.begin_turn(self.owner, self.branch, "status")["id"]
        with self.client() as client:
            result = client.call("support_diagnostics", {})
        self.assertTrue(result["ok"], result)
        recent = result["data"]["recent_turns"]
        self.assertEqual([(item["id"], item["status"]) for item in recent],
                         [(second, "rejected"), (first, "error")])
        self.assertEqual(recent[0]["calls"], [{"stage": "generator", "status": "not_sent", "count": 1}])
        self.assertEqual(set(recent[0]), {"id", "status", "calls"})
        self.assertNotIn("private question", str(result))
        self.assertNotIn("other branch secret", str(result))
        self.assertNotIn("foreign secret", str(result))
        self.assertNotIn(foreign_branch_turn, str(result))
        self.assertNotIn(foreign_user_turn, str(result))

    def test_diagnostics_limit_previous_turns_to_three(self):
        previous = []
        for index in range(4):
            turn_id = self.turn if index == 0 else self.store.begin_turn(
                self.owner, self.branch, "private question")["id"]
            previous.append(turn_id)
            self.store.move_turn(turn_id, "input_check")
            self.store.end_turn(turn_id, "error")
        self.turn = self.store.begin_turn(self.owner, self.branch, "status")["id"]
        with self.client() as client:
            result = client.call("support_diagnostics", {})
        self.assertTrue(result["ok"], result)
        self.assertEqual([item["id"] for item in result["data"]["recent_turns"]],
                         list(reversed(previous[1:])))

    def test_integrated_schemas_are_strict_and_limit_fields(self):
        from support.tools import INTEGRATED_TOOL_SCHEMAS
        for schema in INTEGRATED_TOOL_SCHEMAS.values():
            Draft202012Validator.check_schema(schema)
            self.assertFalse(schema["additionalProperties"])
        update = Draft202012Validator(INTEGRATED_TOOL_SCHEMAS["support_profile_update"])
        self.assertFalse(update.is_valid({"changes": {"role": "x" * 501}, "request_key": "a"}))
        self.assertTrue(update.is_valid({"changes": {"role": ""}, "request_key": "a"}))
        self.assertFalse(update.is_valid({"changes": {"role": "QA", "status": "premium"}, "request_key": "a"}))
        self.assertFalse(update.is_valid({"changes": {"role": "QA"}, "request_key": "a", "user_id": 2}))

    def test_same_ticket_request_key_in_another_branch_does_not_replay_first_ticket(self):
        args = {"title": "Help", "description": "Synthetic issue", "request_key": "same-request"}
        with self.client() as first_client:
            first = first_client.call("support_ticket_create", args)
        self.assertTrue(first["ok"], first)
        other_branch = self.store.create_chat(self.owner)["branch_id"]
        other_turn = self.store.begin_turn(self.owner, other_branch, "support")["id"]
        with self.client(binding=self.binding(branch_id=other_branch, turn_id=other_turn)) as second_client:
            second = second_client.call("support_ticket_create", args)
            self.assertEqual(second_client.call("support_ticket_create", args), second)
            status = second_client.call("support_status", {})
            self.assertTrue(status["ok"], status)
            self.assertEqual(status["integration_status"], "integrated")
            self.assertEqual(status["data"]["llm"]["status"], "unknown")
            self.assertEqual(status["data"]["main_storage"],
                             {"status": "ok", "scope": "main_store"})
            self.assertNotEqual(status["data"]["main_service"]["status"], "ok")
            self.assertNotIn("integration_required", str(status))
        self.assertTrue(second["ok"], second)
        self.assertNotEqual(first["data"]["ticket"]["id"], second["data"]["ticket"]["id"])
        self.assertEqual(second["data"]["receipt"]["request_key"], "same-request")

    def test_malformed_binding_is_rejected_before_spawning_server(self):
        with self.assertRaises(ValueError):
            self.client(binding=self.binding(user_id=self.other))
        with self.assertRaises(ValueError):
            self.client(binding=self.binding(explicit_budget=0))
        self.assertFalse(self.ticket_db.exists())

    def test_real_stdio_advertises_strict_integrated_schemas(self):
        async def discover():
            params = self.client()._parameters()
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    return {tool.name: tool.inputSchema for tool in tools.tools}

        schemas = anyio.run(discover)
        self.assertEqual(len(schemas), 12)
        for schema in schemas.values():
            Draft202012Validator.check_schema(schema)
            self.assertFalse(schema["additionalProperties"])
        receipt_schema = Draft202012Validator(schemas["support_ticket_receipt"])
        create = {"title": "A", "description": "B", "request_key": "lookup"}
        self.assertTrue(receipt_schema.is_valid({"tool": "support_ticket_create", "arguments": create}))
        self.assertFalse(receipt_schema.is_valid({"tool": "support_ticket_close", "arguments": create}))
        self.assertFalse(receipt_schema.is_valid({"tool": "support_ticket_create", "arguments": create | {"user_id": 1}}))
        self.assertFalse(receipt_schema.is_valid({"tool": "support_ticket_create", "arguments": create, "turn_id": self.turn}))
        self.assertFalse(Draft202012Validator(schemas["support_memory_set"]).is_valid({
            "key": "x", "value": "v", "request_key": "r", "user_id": self.other}))


if __name__ == "__main__":
    unittest.main()

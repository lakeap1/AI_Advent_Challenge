import concurrent.futures
import json
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from support.domain import Actor, SupportError
from support.tickets import TicketStore


class TicketStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "support.sqlite3"
        self.alice = Actor("alice")
        self.bob = Actor("bob")
        self.store = TicketStore(self.path)

    def assert_code(self, code, action):
        with self.assertRaises(SupportError) as raised:
            action()
        self.assertEqual(raised.exception.code, code)

    def count(self, table):
        with closing(sqlite3.connect(self.path)) as connection:
            return connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]

    def test_readonly_receipt_lookup_never_creates_storage_or_reexecutes(self):
        args = {"title": " A ", "description": "private body", "request_key": "lookup"}
        self.assert_code("storage_unavailable", lambda: self.store.lookup_receipt(self.alice, "create", args))
        self.assertFalse(self.path.exists())
        original = self.store.create(self.alice, **args)
        before = self.path.read_bytes()
        self.assertEqual(self.store.lookup_receipt(self.alice, "create", args), original["receipt"])
        self.assertIsNone(self.store.lookup_receipt(self.bob, "create", args))
        self.assertIsNone(self.store.lookup_receipt(self.alice, "create", {**args, "request_key": "missing"}))
        self.assert_code("idempotency_conflict", lambda: self.store.lookup_receipt(
            self.alice, "create", {**args, "title": "A"}))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.count("support_operations"), 1)

    def test_restart_preserves_ticket_and_receipt(self):
        created = self.store.create(self.alice, "  Login  ", " Cannot sign in ", "create-1")
        ticket = created["ticket"]
        self.assertEqual(ticket["title"], "Login")
        self.assertEqual(ticket["status"], "open")
        self.assertEqual([item["body"] for item in ticket["messages"]], ["Cannot sign in"])
        self.assertEqual(len(ticket["id"]), 32)
        self.assertEqual(created["receipt"]["operation"], "create")
        self.assertEqual(created["receipt"]["ticket_id"], ticket["id"])
        self.assertEqual(created["receipt"]["request_key"], "create-1")
        self.assertIn("+00:00", created["receipt"]["committed_at"])
        reopened = TicketStore(self.path)
        self.assertEqual(reopened.get(self.alice, ticket["id"]), ticket)
        self.assertEqual(reopened.create(self.alice, "  Login  ", " Cannot sign in ", "create-1"), created)

    def test_exact_accepted_arguments_define_replay(self):
        original = self.store.create(self.alice, " A ", " B ", "create-key")
        ticket_id = original["ticket"]["id"]
        self.assert_code(
            "idempotency_conflict",
            lambda: self.store.create(self.alice, "A", " B ", "create-key"),
        )
        appended = self.store.append(self.alice, ticket_id, " C ", "append-key")
        self.assert_code(
            "idempotency_conflict",
            lambda: self.store.append(self.alice, ticket_id, "C", "append-key"),
        )
        self.assertEqual(self.store.create(self.alice, " A ", " B ", "create-key"), original)
        self.assertEqual(self.store.append(self.alice, ticket_id, " C ", "append-key"), appended)
        self.assertEqual(self.count("support_messages"), 2)
        self.assertEqual(self.count("support_operations"), 2)

    def test_existing_empty_databases_are_rejected_without_change(self):
        empty_sqlite = Path(self.temporary.name) / "empty.sqlite3"
        with closing(sqlite3.connect(empty_sqlite)):
            pass
        zero_byte = Path(self.temporary.name) / "zero.sqlite3"
        zero_byte.write_bytes(b"")
        for foreign in (empty_sqlite, zero_byte):
            with self.subTest(path=foreign.name):
                before = foreign.read_bytes()
                self.assert_code("storage_unavailable", lambda: TicketStore(foreign))
                self.assertEqual(foreign.read_bytes(), before)
                self.assertEqual(set(Path(self.temporary.name).glob(foreign.name + "*")), {foreign})

    def test_surrogate_text_is_invalid_before_io(self):
        bad_text = chr(0xD800)
        self.assert_code("invalid_argument", lambda: self.store.create(self.alice, bad_text, "B", "one"))
        self.assert_code("invalid_argument", lambda: self.store.create(self.alice, "A", bad_text, "two"))
        self.assertFalse(self.path.exists())
        ticket_id = self.store.create(self.alice, "A", "B", "three")["ticket"]["id"]
        self.assert_code("invalid_argument", lambda: self.store.append(self.alice, ticket_id, bad_text, "four"))
        self.assertEqual(self.count("support_operations"), 1)
    def test_get_snapshot_is_consistent_when_append_commits_between_selects(self):
        created = self.store.create(self.alice, "A", "Initial", "create")
        ticket_id = created["ticket"]["id"]
        before = created["ticket"]
        with closing(sqlite3.connect(self.path)) as setup_connection:
            self.assertEqual(setup_connection.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")

        writer = TicketStore(self.path)

        class InterleavingConnection(sqlite3.Connection):
            injected = False

            def execute(self, sql, parameters=(), /):
                if sql.startswith("SELECT id, body, created_at") and not self.injected:
                    self.injected = True
                    writer.append(self_actor, ticket_id, "Committed after header", "append")
                return super().execute(sql, parameters)

        self_actor = self.alice
        reader = sqlite3.connect(self.path, isolation_level=None, factory=InterleavingConnection)
        reader.row_factory = sqlite3.Row
        with patch.object(self.store, "_connect", return_value=reader):
            observed = self.store.get(self.alice, ticket_id)
        self.assertTrue(reader.injected)
        after = writer.get(self.alice, ticket_id)
        self.assertEqual(len(after["messages"]), 2)
        self.assertIn(
            (observed["updated_at"], len(observed["messages"])),
            ((before["updated_at"], 1), (after["updated_at"], 2)),
        )
    def test_owner_scope_and_metadata_list(self):
        created = self.store.create(self.alice, "Access", "Private body", "k1")
        ticket_id = created["ticket"]["id"]
        self.assert_code("not_found", lambda: self.store.get(self.bob, ticket_id))
        self.assertEqual(self.store.list(self.bob), [])
        self.assert_code("not_found", lambda: self.store.get(self.alice, "0" * 32))
        self.assert_code("not_found", lambda: self.store.append(self.bob, ticket_id, "mine", "k2"))
        self.assert_code("not_found", lambda: self.store.close(self.bob, ticket_id, "k3"))
        self.assertEqual(self.count("support_operations"), 1)
        listing = self.store.list(self.alice)
        self.assertEqual(len(listing), 1)
        self.assertEqual(listing[0]["id"], ticket_id)
        self.assertNotIn("messages", listing[0])
        self.assertNotIn("Private body", str(listing))

    def test_replay_returns_original_snapshot_after_later_mutations(self):
        created = self.store.create(self.alice, "Title", "First", "c")
        ticket_id = created["ticket"]["id"]
        first = self.store.append(self.alice, ticket_id, "Second", "a")
        self.store.append(self.alice, ticket_id, "Third", "b")
        self.assertEqual(self.store.append(self.alice, ticket_id, "Second", "a"), first)
        self.assertEqual([m["body"] for m in first["ticket"]["messages"]], ["First", "Second"])
        self.assertEqual([m["body"] for m in self.store.get(self.alice, ticket_id)["messages"]], ["First", "Second", "Third"])
        self.assertEqual(self.count("support_messages"), 3)
        self.assertEqual(self.count("support_operations"), 3)

    def test_key_conflicts_across_operation_and_payload(self):
        created = self.store.create(self.alice, "A", "Body", "same")
        ticket_id = created["ticket"]["id"]
        self.assert_code("idempotency_conflict", lambda: self.store.create(self.alice, "B", "Body", "same"))
        self.assert_code("idempotency_conflict", lambda: self.store.append(self.alice, ticket_id, "Message", "same"))
        self.assertEqual(self.count("support_operations"), 1)
        self.assertEqual(self.count("support_messages"), 1)

    def test_close_is_idempotent_and_closed_append_is_rejected(self):
        ticket_id = self.store.create(self.alice, "A", "Body", "c")["ticket"]["id"]
        closed = self.store.close(self.alice, ticket_id, "close")
        self.assertEqual(closed["ticket"]["status"], "closed_by_user")
        self.assertEqual(self.store.close(self.alice, ticket_id, "close"), closed)
        self.assert_code("closed", lambda: self.store.append(self.alice, ticket_id, "No", "a"))
        self.assertEqual(self.count("support_messages"), 1)
        self.assertEqual(self.count("support_operations"), 2)

    def test_validation_rejects_empty_boolean_and_limits_without_receipts(self):
        invalid = [
            ("", "description", "k"),
            (" ", "description", "k"),
            ("x" * 161, "description", "k"),
            ("title", "", "k"),
            ("title", "x" * 8001, "k"),
            ("title", "description", ""),
            ("title", "description", "not valid"),
            (True, "description", "k"),
        ]
        for title, description, key in invalid:
            with self.subTest(title=str(title)[:8], key=key):
                self.assert_code("invalid_argument", lambda: self.store.create(self.alice, title, description, key))
        self.assert_code("invalid_argument", lambda: self.store.create(Actor("bad actor"), "A", "B", "k"))
        self.assertFalse(self.path.exists())
        ticket_id = self.store.create(self.alice, "A", "Body", "c")["ticket"]["id"]
        self.assert_code("invalid_argument", lambda: self.store.append(self.alice, ticket_id, " ", "a"))
        self.assert_code("invalid_argument", lambda: self.store.list(self.alice, True))
        self.assert_code("invalid_argument", lambda: self.store.list(self.alice, 51))
        self.assertEqual(self.count("support_operations"), 1)

    def test_message_limit_rolls_back_receipt(self):
        ticket_id = self.store.create(self.alice, "A", "Body", "c")["ticket"]["id"]
        for index in range(99):
            self.store.append(self.alice, ticket_id, "ok", "m" + str(index))
        self.assertEqual(self.count("support_messages"), 100)
        self.assert_code("limit_exceeded", lambda: self.store.append(self.alice, ticket_id, "overflow", "overflow"))
        self.assertEqual(self.count("support_messages"), 100)
        self.assertEqual(self.count("support_operations"), 100)

    def test_concurrent_duplicate_create(self):
        def create():
            return TicketStore(self.path).create(self.alice, "A", "Body", "shared")
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            first, second = list(executor.map(lambda ignored: create(), range(2)))
        self.assertEqual(first, second)
        self.assertEqual(self.count("support_tickets"), 1)
        self.assertEqual(self.count("support_operations"), 1)

    def test_two_processes_create_same_new_database_once(self):
        code = """
import json
import sys
import time
from pathlib import Path
from support.domain import Actor
from support.tickets import TicketStore

db, ready, go = map(Path, sys.argv[1:])
ready.touch()
for _ in range(500):
    if go.exists():
        break
    time.sleep(0.01)
else:
    raise RuntimeError("barrier timeout")
result = TicketStore(db).create(Actor("alice"), "A", "Body", "shared")
print(json.dumps(result))
"""
        go = Path(self.temporary.name) / "go"
        ready_paths = [Path(self.temporary.name) / "ready-1", Path(self.temporary.name) / "ready-2"]
        processes = [
            subprocess.Popen(
                [sys.executable, "-B", "-c", code, str(self.path), str(ready), str(go)],
                cwd=Path(__file__).resolve().parents[1],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for ready in ready_paths
        ]
        try:
            for _ in range(500):
                if all(ready.exists() for ready in ready_paths):
                    break
                time.sleep(0.01)
            else:
                self.fail("subprocess startup timeout")
            go.touch()
            outputs = [process.communicate(timeout=15) for process in processes]
            for process, (_, stderr) in zip(processes, outputs):
                self.assertEqual(process.returncode, 0, stderr)
            first, second = [json.loads(stdout) for stdout, _ in outputs]
            self.assertEqual(first, second)
            self.assertEqual(self.count("support_tickets"), 1)
            self.assertEqual(self.count("support_operations"), 1)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)

    def test_failed_initialization_leaves_no_target_or_owned_temp(self):
        with patch("support.tickets._SCHEMA", "THIS IS NOT SQL;"):
            self.assert_code(
                "storage_unavailable",
                lambda: self.store.create(self.alice, "A", "Body", "initial"),
            )
        self.assertFalse(self.path.exists())
        self.assertEqual(
            set(Path(self.temporary.name).glob("*.sqlite3")),
            set(),
        )
        created = self.store.create(self.alice, "A", "Body", "retry")
        self.assertEqual(created["ticket"]["title"], "A")
        self.assertEqual(self.count("support_tickets"), 1)
    def test_health_only_reads_existing_storage(self):
        self.assert_code('storage_unavailable', self.store.health)
        self.assertFalse(self.path.exists())
        self.assertEqual(list(Path(self.temporary.name).iterdir()), [])
        self.store.create(self.alice, 'A', 'B', 'setup')
        result = self.store.health()
        self.assertEqual(result['storage'], 'ok')
        self.assertIn('+00:00', result['checked_at'])
    def test_foreign_database_is_unchanged(self):
        foreign = Path(self.temporary.name) / "foreign.sqlite3"
        with closing(sqlite3.connect(foreign)) as connection:
            connection.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
            connection.execute("INSERT INTO users(name) VALUES ('existing')")
            connection.commit()
        before = foreign.read_bytes()
        self.assert_code("storage_unavailable", lambda: TicketStore(foreign))
        self.assertEqual(foreign.read_bytes(), before)
        with closing(sqlite3.connect(foreign)) as connection:
            self.assertEqual(connection.execute("SELECT name FROM users").fetchone()[0], "existing")

    def test_tampered_support_schema_is_rejected_before_write(self):
        created = self.store.create(self.alice, "A", "Body", "c")
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("ALTER TABLE support_tickets RENAME COLUMN title TO subject")
            connection.commit()
        before = self.path.read_bytes()
        self.assert_code("storage_unavailable", lambda: TicketStore(self.path))
        self.assertEqual(self.path.read_bytes(), before)
    def test_invalid_actor_rejected_before_io(self):
        missing_parent = Path(self.temporary.name) / "missing" / "support.sqlite3"
        store = TicketStore(missing_parent)
        self.assert_code("invalid_argument", lambda: store.get(Actor("bad actor"), "0" * 32))
        self.assertFalse(missing_parent.exists())


if __name__ == "__main__":
    unittest.main()

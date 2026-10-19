import tempfile
import unittest
from pathlib import Path

from graphics.context import ExplicitBudgetError
from graphics.store import Store


class SupportStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db = Path(temporary.name) / "main.sqlite3"
        self.store = Store(self.db)
        self.owner = self.store.create_user("owner", "long password")
        self.other = self.store.create_user("other", "other password")
        self.branch = self.store.create_chat(self.owner)["branch_id"]
        self.turn = self.store.begin_turn(self.owner, self.branch, "support")["id"]

    def test_profile_receipt_replays_without_restoring_old_value(self):
        first = self.store.support_update_profile(self.owner, self.branch, self.turn,
                                                  {"role": "QA", "level": "beginner"}, "same-key")
        self.assertEqual(first["receipt"]["changed_fields"], ["level", "role"])
        self.assertEqual(self.store.get_profile(self.owner)["role"], "QA")
        action = self.store.support_actions(self.owner, self.branch, self.turn)[0]
        self.assertEqual(action["status"], "confirmed")
        self.assertEqual(action["receipt"], first["receipt"])
        self.store.update_profile(self.owner, {"role": "artist"})
        self.assertEqual(Store(self.db).support_update_profile(self.owner, self.branch, self.turn,
                         {"role": "QA", "level": "beginner"}, "same-key"), first)
        self.assertEqual(self.store.get_profile(self.owner)["role"], "artist")
        self.assertNotIn("artist", str(self.store.support_actions(self.owner, self.branch)))
        with self.assertRaises(ValueError):
            self.store.support_update_profile(self.owner, self.branch, self.turn,
                                              {"role": "manager"}, "same-key")

    def test_memory_set_delete_are_atomic_replayable_and_owner_scoped(self):
        saved = self.store.support_memory_set(self.owner, self.branch, self.turn,
                                              "style", "short answers", "set-1")
        memory_id = saved["receipt"]["memory_id"]
        self.assertEqual(self.store.list_memories(self.owner)[0]["value"], "short answers")
        self.assertEqual(Store(self.db).support_memory_set(self.owner, self.branch, self.turn,
                         "style", "short answers", "set-1"), saved)
        deleted = self.store.support_memory_delete(self.owner, self.branch, self.turn,
                                                   memory_id, "delete-1")
        self.assertEqual(self.store.list_memories(self.owner), [])
        self.assertEqual(Store(self.db).support_memory_delete(self.owner, self.branch, self.turn,
                         memory_id, "delete-1"), deleted)
        self.assertNotIn("short answers", str(self.store.support_actions(self.owner, self.branch)))
        with self.assertRaises(PermissionError):
            self.store.support_memory_set(self.other, self.branch, self.turn, "bad", "bad", "foreign")

    def test_combined_explicit_budget_rejects_without_receipt_or_mutation(self):
        limited = Store(self.db, explicit_budget=100)
        with self.assertRaises(ExplicitBudgetError):
            limited.support_update_profile(self.owner, self.branch, self.turn,
                                           {"goal": "x" * 100}, "over-budget")
        self.assertEqual(limited.get_profile(self.owner), {})
        self.assertEqual(limited.support_actions(self.owner, self.branch), [])

    def test_journal_checks_owner_branch_and_turn_on_each_access(self):
        receipt = {"operation_id": "a" * 32, "request_key": "req-1",
                   "operation": "support_profile_update", "branch_id": self.branch,
                   "turn_id": self.turn, "committed_at": "2026-10-19T00:00:00+00:00",
                   "changed_fields": ["role"]}
        saved = self.store.record_support_action(self.owner, self.branch, self.turn,
                         "req-1", "support_profile_update", "confirmed", receipt=receipt)
        self.assertEqual(self.store.support_actions(self.owner, self.branch, self.turn), [saved])
        with self.assertRaises(PermissionError):
            self.store.support_actions(self.other, self.branch)
        with self.assertRaises(PermissionError):
            self.store.record_support_action(self.other, self.branch, self.turn,
                                             "req-2", "support_profile_update", "pending")
        with self.assertRaises(PermissionError):
            self.store.support_actions(self.owner, self.branch, "bad-turn")

    def test_pending_intent_becomes_atomic_confirmation_and_cannot_regress(self):
        self.store.record_support_action(self.owner, self.branch, self.turn, "req-profile",
                                         "support_profile_update", "pending")
        changed = self.store.support_update_profile(self.owner, self.branch, self.turn,
                                                    {"role": "QA"}, "req-profile")
        action = self.store.support_actions(self.owner, self.branch, self.turn)[0]
        self.assertEqual(action["status"], "confirmed")
        self.assertEqual(action["receipt"], changed["receipt"])
        retry = self.store.record_support_action(self.owner, self.branch, self.turn,
                                                 "req-profile", "support_profile_update", "pending")
        self.assertEqual(retry["status"], "confirmed")
        self.assertEqual(retry["receipt"], changed["receipt"])

    def test_empty_profile_values_clear_only_named_fields_and_replay_does_not_reapply(self):
        self.store.update_profile(self.owner, {"role": "QA", "level": "beginner", "goal": "materials"})
        memory = self.store.remember(self.owner, "style", "short")
        cleared = self.store.support_update_profile(self.owner, self.branch, self.turn,
                                                    {"role": "", "level": ""}, "clear-1")
        self.assertEqual(self.store.get_profile(self.owner), {"goal": "materials"})
        self.assertEqual(self.store.list_memories(self.owner)[0]["id"], memory["id"])
        self.store.update_profile(self.owner, {"role": "artist"})
        self.assertEqual(Store(self.db).support_update_profile(self.owner, self.branch, self.turn,
                         {"role": "", "level": ""}, "clear-1"), cleared)
        self.assertEqual(self.store.get_profile(self.owner), {"role": "artist", "goal": "materials"})

    def test_confirmed_journal_rejects_unbound_or_wrong_receipt_without_row(self):
        base = {"operation_id": "a" * 32, "request_key": "req-1", "operation": "create",
                "ticket_id": "b" * 32, "committed_at": "2026-10-19T00:00:00+00:00"}
        invalid = (None, {**base, "request_key": "other"}, {**base, "operation": "close"},
                   {**base, "branch_id": "0" * 32}, {key: value for key, value in base.items()
                                                       if key != "operation_id"})
        for receipt in invalid:
            with self.subTest(receipt=receipt):
                with self.assertRaises(ValueError):
                    self.store.record_support_action(self.owner, self.branch, self.turn,
                         "req-1", "support_ticket_create", "confirmed", receipt=receipt)
                self.assertEqual(self.store.support_actions(self.owner, self.branch), [])
        for status in ("pending", "error", "unknown"):
            with self.subTest(status=status):
                with self.assertRaises(ValueError):
                    self.store.record_support_action(self.owner, self.branch, self.turn,
                         "req-1", "support_ticket_create", status, receipt=base)
        saved = self.store.record_support_action(self.owner, self.branch, self.turn,
                     "req-1", "support_ticket_create", "confirmed", receipt=base)
        self.assertEqual(saved["receipt"], base)
        self.assertEqual(self.store.record_support_action(self.owner, self.branch, self.turn,
                         "req-1", "support_ticket_create", "pending")["receipt"], base)
        malformed_profile = {"operation_id": "c" * 32, "request_key": "req-profile2",
                             "operation": "support_profile_update", "branch_id": self.branch,
                             "turn_id": self.turn, "committed_at": "2026-10-19T00:00:00+00:00",
                             "changed_fields": [{}]}
        with self.assertRaises(ValueError):
            self.store.record_support_action(self.owner, self.branch, self.turn,
                "req-profile2", "support_profile_update", "confirmed", receipt=malformed_profile)


if __name__ == "__main__":
    unittest.main()

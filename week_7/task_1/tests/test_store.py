import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from graphics.store import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "service.sqlite3"
        self.store = Store(self.path)
        self.user = self.store.create_user("owner", "long password")
        self.other = self.store.create_user("other", "other password")
        self.chat = self.store.create_chat(self.user)
        self.branch_id = self.chat["branch_id"]

    def test_auth_profile_memory_and_owner_scope_survive_restart(self):
        self.assertEqual(self.store.authenticate("owner", "long password"), self.user)
        self.assertIsNone(self.store.authenticate("owner", "wrong password"))
        self.store.update_profile(self.user, {"role": "QA", "level": "beginner"})
        memory = self.store.remember(self.user, "style", "short")
        reopened = Store(self.path)
        self.assertEqual(reopened.get_profile(self.user)["level"], "beginner")
        self.assertEqual(reopened.list_memories(self.user)[0]["value"], "short")
        self.assertEqual(reopened.list_memories(self.other), [])
        with self.assertRaises(PermissionError):
            reopened.get_branch(self.other, self.branch_id)
        with self.assertRaises(PermissionError):
            reopened.fork_branch(self.other, self.branch_id, 1)
        reopened.forget(self.user, memory["id"])
        self.assertEqual(Store(self.path).list_memories(self.user), [])

    def test_fork_at_old_message_uses_old_snapshot_and_only_prefix(self):
        self.store.update_branch(self.user, self.branch_id, {
            "conditions": {"unity": "6.0"}, "working_state": {"topic": "materials"},
            "summary": "Earlier material discussion", "summary_through": 0,
        })
        anchor = self.store.append_message(self.user, self.branch_id, "user", "What is a material?")
        self.store.update_branch(self.user, self.branch_id, {
            "conditions": {"unity": "6.3"}, "working_state": {"topic": "lighting"},
            "summary": "Later lighting discussion", "summary_through": 1,
        })
        self.store.append_message(self.user, self.branch_id, "user", "Later correction")
        child = self.store.fork_branch(self.user, self.branch_id, anchor["id"])
        self.assertEqual(child["conditions"], {"unity": "6.0"})
        self.assertEqual(child["working_state"], {"topic": "materials"})
        self.assertEqual(child["summary"], "Earlier material discussion")
        self.assertEqual([m["content"] for m in child["messages"]], ["What is a material?"])
        child["conditions"]["unity"] = "mutated"
        self.assertEqual(self.store.get_branch(self.user, self.branch_id)["conditions"], {"unity": "6.3"})
        self.assertEqual(Store(self.path).get_branch(self.user, child["id"])["conditions"], {"unity": "6.0"})
        with self.assertRaises(PermissionError):
            self.store.fork_branch(self.user, child["id"], anchor["id"])

    def test_turn_lock_valid_transitions_and_rejected_draft_is_not_message(self):
        turn = self.store.begin_turn(self.user, self.branch_id, "question")
        with self.assertRaises(RuntimeError):
            Store(self.path).begin_turn(self.user, self.branch_id, "duplicate")
        with self.assertRaises(ValueError):
            self.store.move_turn(turn["id"], "accepted")
        for state in ("input_check", "context", "sources"):
            self.store.move_turn(turn["id"], state)
        self.store.move_turn(turn["id"], "draft", {"draft": "unapproved content"})
        self.assertNotIn("unapproved content", str(self.store.get_turn(self.user, turn["id"])))
        for state in ("program_check", "semantic_check"):
            self.store.move_turn(turn["id"], state)
        self.store.end_turn(turn["id"], "rejected", error="Ответ не прошёл проверку")
        receipt = Store(self.path).get_turn(self.user, turn["id"])
        self.assertEqual(receipt["status"], "rejected")
        self.assertNotIn("unapproved content", str(receipt).lower())
        self.assertEqual(self.store.get_branch(self.user, self.branch_id)["messages"], [])
        self.assertEqual(self.store.begin_turn(self.user, self.branch_id, "next")["status"], "new")

    def test_terminal_error_cannot_expose_draft_content(self):
        turn = self.store.begin_turn(self.user, self.branch_id, "question")
        for state in ("input_check", "context", "sources", "draft", "program_check", "semantic_check"):
            self.store.move_turn(turn["id"], state)
        self.store.end_turn(turn["id"], "rejected", error="unaccepted private draft")
        receipt = self.store.get_turn(self.user, turn["id"])
        self.assertNotIn("unaccepted private draft", str(receipt))
        self.assertIsInstance(receipt["error"], str)

    def test_external_condition_edit_is_blocked_during_active_turn(self):
        self.store.update_branch(self.user, self.branch_id, {"conditions": {"unity": "6.0"}})
        turn = self.store.begin_turn(self.user, self.branch_id, "question")
        self.store.move_turn(turn["id"], "input_check")
        self.store.move_turn(turn["id"], "context")
        with self.assertRaises(RuntimeError):
            Store(self.path).update_branch(self.user, self.branch_id, {"conditions": {"unity": "6.3"}})
        self.assertEqual(self.store.get_branch(self.user, self.branch_id)["conditions"], {"unity": "6.0"})
        self.store.update_branch(self.user, self.branch_id, {"working_state": {"topic": "materials"}},
                                 turn_id=turn["id"])
        self.assertEqual(self.store.get_branch(self.user, self.branch_id)["working_state"], {"topic": "materials"})
        with self.assertRaises(PermissionError):
            self.store.update_branch(self.user, self.branch_id, {"conditions": {"unity": "6.3"}},
                                     turn_id="wrong turn")

    def test_accepted_answer_snapshot_uses_acceptance_state_not_later_edit(self):
        self.store.update_branch(self.user, self.branch_id, {"conditions": {"unity": "6.0"}})
        turn = self.store.begin_turn(self.user, self.branch_id, "question")
        for state in ("input_check", "context", "sources", "draft", "program_check", "semantic_check"):
            self.store.move_turn(turn["id"], state)
        answer = self.store.publish_answer(self.user, self.branch_id, turn["id"], "About Unity 6.0")
        self.store.update_branch(self.user, self.branch_id, {"conditions": {"unity": "6.3"}})
        self.assertEqual(answer["snapshot"]["conditions"], {"unity": "6.0"})

    def test_parallel_begin_turn_admits_exactly_one_writer(self):
        def begin(_):
            try:
                return Store(self.path).begin_turn(self.user, self.branch_id, "question")["id"]
            except RuntimeError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(begin, range(2)))
        self.assertEqual(sum(value is not None for value in results), 1)

    def test_assistant_message_requires_accepted_turn(self):
        turn = self.store.begin_turn(self.user, self.branch_id, "question")
        with self.assertRaises(PermissionError):
            self.store.append_message(self.user, self.branch_id, "assistant", "unapproved content")
        with self.assertRaises(PermissionError):
            self.store.append_message(self.user, self.branch_id, "assistant", "unapproved content", turn_id=turn["id"])
        self.assertEqual(self.store.get_branch(self.user, self.branch_id)["messages"], [])
        for state in ("input_check", "context", "sources", "draft", "program_check", "semantic_check"):
            self.store.move_turn(turn["id"], state)
        with self.assertRaises(ValueError):
            self.store.end_turn(turn["id"], "accepted")
        accepted = self.store.publish_answer(self.user, self.branch_id, turn["id"], "approved")
        self.assertEqual(accepted["content"], "approved")
        with self.assertRaises(ValueError):
            self.store.publish_answer(self.user, self.branch_id, turn["id"], "duplicate")

    def test_pending_or_unknown_call_blocks_budget_after_restart(self):
        turn = self.store.begin_turn(self.user, self.branch_id, "question")
        for state in ("input_check", "context", "sources", "draft"):
            self.store.move_turn(turn["id"], state)
        call = self.store.start_call(self.user, self.branch_id, turn["id"], "generator", "glm-5.3-flash")
        reopened = Store(self.path)
        with self.assertRaises(RuntimeError):
            reopened.check_budget(self.user)
        reopened.finish_call(call, None, None, status="unknown")
        ledger = reopened.ledger(self.user)
        self.assertFalse(ledger["complete"])
        self.assertIsNone(ledger["cost_estimated_usd"])
        with self.assertRaises(RuntimeError):
            Store(self.path).check_budget(self.user)

    def test_budget_is_global_across_users(self):
        turn = self.store.begin_turn(self.user, self.branch_id, "question")
        for state in ("input_check", "context", "sources", "draft"):
            self.store.move_turn(turn["id"], state)
        call = self.store.start_call(self.user, self.branch_id, turn["id"], "generator", "glm-5.3-flash")
        self.store.finish_call(call, {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}, 0.75)
        with self.assertRaises(RuntimeError):
            self.store.check_budget(self.other, reserve_usd=0.30)
        other_chat = self.store.create_chat(self.other)
        other_turn = self.store.begin_turn(self.other, other_chat["branch_id"], "question")
        for state in ("input_check", "context", "sources", "draft"):
            self.store.move_turn(other_turn["id"], state)
        with self.assertRaises(RuntimeError):
            self.store.start_call(self.other, other_chat["branch_id"], other_turn["id"],
                                  "generator", "glm-5.3-flash", reserve_usd=0.30)


if __name__ == "__main__":
    unittest.main()

"""Regression boundaries from final review; all model responses are offline fixtures."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import sqlite3
import threading
import unittest

import test_service
from graphics.context import build_context
from graphics.policies import check_answer
from graphics.store import Store


class FinalFixTests(unittest.TestCase):
    setUp = test_service.ServiceTests.setUp
    transport = test_service.ServiceTests.transport
    queue_success = test_service.ServiceTests.queue_success
    def semantic_turn(self):
        turn = self.store.begin_turn(self.user, self.branch, "A")["id"]
        self.store.move_turn(turn, "input_check")
        self.store.append_message(self.user, self.branch, "user", "A", turn_id=turn)
        for state in ("context", "sources", "draft", "program_check", "semantic_check"):
            self.store.move_turn(turn, state)
        return turn

    def test_publication_has_no_accepted_orphan_on_insert_failure(self):
        self.queue_success()
        with self.store._connect() as db:
            db.execute("CREATE TRIGGER fail_answer BEFORE INSERT ON messages WHEN NEW.role='assistant' "
                       "BEGIN SELECT RAISE(ABORT,'injected insert failure'); END")
        result = self.service.ask(self.user, self.branch, "Что такое шейдер?")
        self.assertEqual(result["status"], "error")
        self.assertEqual(self.store.get_turn(self.user, result["turn_id"])["status"], "error")
        self.assertEqual([m["role"] for m in self.store.get_branch(self.user, self.branch)["messages"]], ["user"])

    def test_atomic_publication_blocks_second_turn_until_answer_visible(self):
        turn = self.semantic_turn()
        inserted = threading.Event()
        attempted = threading.Event()
        observation = []
        original = self.store._connect
        from contextlib import contextmanager
        @contextmanager
        def hooked():
            with original() as db:
                def trace(sql):
                    if sql.startswith("INSERT INTO messages") and "'assistant'" in sql:
                        inserted.set()
                        self.assertTrue(attempted.wait(3))
                db.set_trace_callback(trace)
                yield db
        self.store._connect = hooked
        def second():
            self.assertTrue(inserted.wait(3))
            attempted.set()
            next_store = Store(self.store.path)
            next_turn = next_store.begin_turn(self.user, self.branch, "B")["id"]
            next_store.append_message(self.user, self.branch, "user", "B", turn_id=next_turn)
            observation.extend(m["role"] for m in Store(self.store.path).get_branch(self.user, self.branch)["messages"])
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(second)
            self.store.publish_answer(self.user, self.branch, turn, "Ответ A")
            future.result(timeout=5)
        self.assertEqual(observation, ["user", "assistant", "user"])
        with self.assertRaises((ValueError, RuntimeError, PermissionError)):
            self.store.publish_answer(self.user, self.branch, turn, "дубль")

    def test_atomic_publication_requires_owner_semantic_state_and_rollback(self):
        turn = self.store.begin_turn(self.user, self.branch, "A")["id"]
        with self.assertRaises((ValueError, PermissionError)):
            self.store.publish_answer(self.user, self.branch, turn, "слишком рано")
        for state in ("input_check", "context", "sources", "draft", "program_check", "semantic_check"):
            self.store.move_turn(turn, state)
        other = self.store.create_user("foreign", "foreign-password")
        with self.assertRaises(PermissionError):
            self.store.publish_answer(other, self.branch, turn, "чужой")
        with self.store._connect() as db:
            db.execute("CREATE TRIGGER fail_answer BEFORE INSERT ON messages WHEN NEW.role='assistant' "
                       "BEGIN SELECT RAISE(ABORT,'failure'); END")
        with self.assertRaises((sqlite3.IntegrityError, RuntimeError)):
            self.store.publish_answer(self.user, self.branch, turn, "сбой")
        self.assertEqual(self.store.get_turn(self.user, turn)["status"], "semantic_check")
        with self.store._connect() as db:
            self.assertIsNone(db.execute("SELECT accepted_snapshot FROM turns WHERE id=?", (turn,)).fetchone()[0])
        self.assertEqual(self.store.get_branch(self.user, self.branch)["messages"], [])
        with self.assertRaises(RuntimeError):
            Store(self.store.path).begin_turn(self.user, self.branch, "B")

    def test_cyrillic_memory_refused_without_poisoning_current_or_new_chat(self):
        with self.assertRaisesRegex(ValueError, "бюджет"):
            self.store.remember(self.user, "подача", "Я" * 2000)
        self.assertEqual(self.store.list_memories(self.user), [])
        for branch in (self.branch, self.store.create_chat(self.user)["branch_id"]):
            build_context(self.store.get_branch(self.user, branch), {}, [], self.settings)

    def test_aggregate_commands_and_failed_update_preserve_configuration(self):
        for i in range(3):
            self.assertEqual(self.service.ask(self.user, self.branch, f"/remember k{i}: " + "x" * 950)["status"], "updated")
        before = self.store.list_memories(self.user)
        refused = self.service.ask(self.user, self.branch, "/remember k3: " + "x" * 950)
        self.assertEqual(refused["status"], "input_rejected")
        self.assertIn("бюджет", refused["message"])
        with self.assertRaisesRegex(ValueError, "бюджет"):
            self.store.remember(self.user, "k0", "Я" * 1700)
        self.assertEqual(self.store.list_memories(self.user), before)
        self.queue_success()
        self.assertEqual(self.service.ask(self.user, self.branch, "Что такое шейдер?")["status"], "accepted")

    def test_userwide_edits_consider_other_branches_and_actual_settings(self):
        self.settings = replace(self.settings, explicit_budget=700)
        self.service = type(self.service)(self.settings, self.store, self.provider, self.retriever, self.mcp)
        other = self.store.create_chat(self.user)["branch_id"]
        self.store.update_branch(self.user, other, {"conditions": {"pipeline": "x" * 500}})
        with self.assertRaisesRegex(ValueError, "бюджет"):
            self.store.update_profile(self.user, {"goal": "y" * 150})
        with self.assertRaisesRegex(ValueError, "бюджет"):
            self.store.remember(self.user, "goal", "y" * 100)
        self.assertEqual(self.store.get_profile(self.user), {})
        self.assertEqual(self.store.list_memories(self.user), [])

    def test_concurrent_memory_saves_cannot_cross_aggregate_budget(self):
        self.store.remember(self.user, "base", "x" * 2800)
        def save(i):
            try:
                Store(self.store.path).remember(self.user, str(i), "x" * 700)
                return True
            except ValueError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(save, range(2)))
        self.assertEqual(sum(results), 1)
        build_context(self.store.get_branch(self.user, self.branch), {}, self.store.list_memories(self.user), self.settings)

    def test_branch_conditions_refused_without_changing_revision_or_state(self):
        before = self.store.get_branch(self.user, self.branch)
        with self.assertRaisesRegex(ValueError, "бюджет"):
            self.store.update_branch(self.user, self.branch, {"conditions": {"pipeline": "Я" * 2000}})
        after = self.store.get_branch(self.user, self.branch)
        self.assertEqual(after, before)

    def test_fork_old_explicit_state_must_fit_current_user_facts(self):
        self.store.update_branch(self.user, self.branch, {"conditions": {"old": "x" * 2700}})
        anchor = self.store.append_message(self.user, self.branch, "user", "якорь")
        self.store.update_branch(self.user, self.branch, {"conditions": {}})
        self.store.remember(self.user, "new", "x" * 1500)
        with self.assertRaisesRegex(ValueError, "бюджет"):
            self.store.fork_branch(self.user, self.branch, anchor["id"])
        with self.store._connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM branches").fetchone()[0], 1)

    def test_oversized_preparation_rejected_without_persisting_or_losing_cost(self):
        self.queue_success()
        self.outputs[0]["working_state"] = {"topic": "Я" * 2200}
        result = self.service.ask(self.user, self.branch, "Что такое шейдер?")
        self.assertEqual(result["status"], "error")
        self.assertEqual(self.store.get_branch(self.user, self.branch)["working_state"], {})
        calls = self.store.ledger(self.user)["calls"]
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0]["output_policy"]["ok"])
        self.assertGreater(calls[0]["cost_estimated_usd"], 0)
        self.assertIn("бюджет", result["message"])

    def test_long_cyrillic_input_is_rejected_before_history_then_short_works(self):
        result = self.service.ask(self.user, self.branch, "Я" * 5001)
        self.assertEqual(result["status"], "input_rejected")
        self.assertIn("10000", result["message"])
        self.assertEqual(self.store.get_branch(self.user, self.branch)["messages"], [])
        self.assertEqual(self.calls, [])
        self.assertIn("input_history_budget", self.store.get_turn(self.user, result["turn_id"])["details"]["check_codes"])
        self.queue_success()
        self.assertEqual(self.service.ask(self.user, self.branch, "Что такое шейдер?")["status"], "accepted")

    def test_plain_formula_power_operator_allowed_but_paired_bold_rejected(self):
        for answer, accepted in (("Результат: roughness ** 2.", True),
                                 ("Результат: roughness ** 2 + x ** 3.", True),
                                 ("Результат: a**2 + b**3.", True),
                                 ("Результат: 2**3**4.", True),
                                 ("Результат: roughness**2.", True),
                                 ("Результат: a **2 + b**3.", True),
                                 ("Результат: (a+b)**2 + c**(3).", True),
                                 ("Результат: **bold**.", False),
                                 ("Результат: **важно**.", False)):
            with self.subTest(answer=answer):
                self.assertEqual(check_answer(answer, ["S1"], {"S1"}, "Формула Python обычным текстом")["ok"], accepted)



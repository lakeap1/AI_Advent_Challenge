"""Integration contracts for the primary conversation service."""
import json
from pathlib import Path
import tempfile
import unittest

from graphics.config import Settings
from graphics.chunking import structural_chunks
from graphics.provider import GLMProvider
from graphics.store import Store
from graphics.service import ChatService


def chunk(domain="learning"):
    return {"chunk_id": f"{domain}:shader:0", "source_id": "shader", "domain": domain,
            "title": "Shader", "section": "Basics", "text": "Шейдер задаёт расчёт вида поверхности.",
            "url": "https://example.test/shader", "version": "2026", "pipeline": "general",
            "language": "ru", "license": "test", "path": "corpus/shader.md",
            "cosine": 0.8, "bm25": 1.0, "rrf": 0.03}


class FakeRetriever:
    def __init__(self, candidates=None, tokens=7):
        self.candidates = [chunk()] if candidates is None else candidates
        self.tokens = tokens
        self.calls = []

    def search(self, query, domain="learning", limit=8, on_request=None):
        self.calls.append((query, domain))
        if self.candidates and on_request:
            on_request()
        return {"candidates": self.candidates,
                "receipt": {"model": "qwen3-embedding:0.6b", "model_digest": "fixture",
                            "dimensions": 1024, "prompt_eval_count": self.tokens if self.candidates else None,
                            "domain": domain}}


class FakeMCP:
    def __init__(self):
        self.calls = 0

    def project_context(self):
        self.calls += 1
        return {"branch": "feature/day31", "commit": "abc123", "tool": "git_current_branch",
                "protocol": "mcp-stdio", "protocol_version": "2025-11-25"}


class ServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.settings = Settings(root, root / "data", api_key="fixture-key")
        self.store = Store(self.settings.db_path)
        self.user = self.store.create_user("owner", "long-fixture-password")
        self.branch = self.store.create_chat(self.user)["branch_id"]
        self.calls = []
        self.outputs = []
        self.retriever = FakeRetriever()
        self.mcp = FakeMCP()
        self.provider = GLMProvider(self.settings, self.store, transport=self.transport)
        self.service = ChatService(self.settings, self.store, provider=self.provider,
                                   retriever=self.retriever, mcp_client=self.mcp)

    def transport(self, payload):
        self.calls.append(payload)
        value = self.outputs.pop(0)
        return {"model": self.settings.model,
                "choices": [{"message": {"content": json.dumps(value, ensure_ascii=False)}}],
                "usage": {"prompt_tokens": 80, "completion_tokens": 20, "total_tokens": 100,
                          "prompt_tokens_details": {"cached_tokens": 0},
                          "completion_tokens_details": {"reasoning_tokens": 3}}}

    def queue_success(self, identifier="S1"):
        self.outputs += [
            {"query": "шейдер", "working_state": {"topic": "shader"},
             "needs_clarification": False, "clarification": "", "answer_kind": "explanation"},
            {"keep": [identifier], "reason": "достаточно"},
            {"answer": "Шейдер определяет вид поверхности. Например, он меняет освещение материала.",
             "source_ids": [identifier]},
            {"accepted": True, "reason": "подтверждено", "used_source_ids": [identifier]},
        ]

    def queue_help_success(self):
        self.outputs += [
            {"keep": ["S1"], "reason": "Документ описывает сервис"},
            {"answer": "Грани объясняют компьютерную графику; текущая ветка feature/day31.",
             "source_ids": ["S1"]},
            {"accepted": True, "reason": "подтверждено", "used_source_ids": ["S1"]},
        ]

    def test_accepted_answer_publishes_only_after_one_judge_and_keeps_real_receipts(self):
        self.queue_success()
        result = self.service.ask(self.user, self.branch, "Что такое шейдер?")
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(len(self.store.get_branch(self.user, self.branch)["messages"]), 2)
        self.assertEqual([c["stage"] for c in self.store.ledger(self.user)["calls"]],
                         ["preparation", "embedding", "filter", "generator", "validator"])
        self.assertEqual(self.store.get_turn(self.user, result["turn_id"])["events"],
                         ["new", "input_check", "context", "sources", "draft", "program_check", "semantic_check", "accepted"])
        self.assertEqual(result["sources"][0]["chunk_id"], "learning:shader:0")
        self.assertIn("original_question", self.calls[-1]["messages"][-1]["content"])

    def test_model_stages_use_short_labels_and_persist_original_chunk(self):
        self.queue_success()
        result = self.service.ask(self.user, self.branch, "Что такое шейдер?")
        self.assertEqual(result["status"], "accepted")
        stages = [json.loads(call["messages"][-1]["content"]) for call in self.calls]
        candidate = stages[1]["candidates"][0]
        self.assertEqual(candidate["label"], "S1")
        self.assertEqual(candidate["text"], "Шейдер задаёт расчёт вида поверхности.")
        self.assertEqual(candidate["url"], "https://example.test/shader")
        for stage in stages[1:]:
            self.assertNotIn("learning:shader:0", json.dumps(stage, ensure_ascii=False))
            self.assertNotIn('"source_id"', json.dumps(stage, ensure_ascii=False))
        self.assertEqual(stages[1]["allowed_labels"], ["S1"])
        self.assertEqual(stages[2]["allowed_labels"], ["S1"])
        self.assertEqual(stages[3]["draft"]["source_ids"], ["S1"])
        self.assertEqual(result["sources"][0]["chunk_id"], "learning:shader:0")
        self.assertEqual(self.store.get_branch(self.user, self.branch)["messages"][-1]["sources"][0]["chunk_id"],
                         "learning:shader:0")

    def test_real_chunk_hash_stays_internal_to_source_mapping(self):
        source = {"id": "shader", "title": "Shader", "url": "https://example.test/shader",
                  "domain": "learning", "version": "2026", "pipeline": "general",
                  "language": "ru", "license": "test", "path": "corpus/shader.md"}
        actual = structural_chunks("# Шейдеры\nШейдер задаёт расчёт вида поверхности.", source)[0]
        self.assertEqual(len(actual["document_hash"]), 64)
        self.retriever.candidates = [actual]
        self.queue_success()
        result = self.service.ask(self.user, self.branch, "Что такое шейдер?")
        self.assertEqual(result["status"], "accepted")
        for call in self.calls[1:]:
            stage = json.loads(call["messages"][-1]["content"])
            encoded = json.dumps(stage, ensure_ascii=False)
            self.assertNotIn(actual["chunk_id"], encoded)
            self.assertNotIn(actual["document_hash"], encoded)
            self.assertNotIn('"document_hash"', encoded)
            self.assertNotIn('"source_id"', encoded)
        self.assertEqual(result["sources"][0]["chunk_id"], actual["chunk_id"])
        self.assertEqual(result["sources"][0]["document_hash"], actual["document_hash"])
        saved = self.store.get_branch(self.user, self.branch)["messages"][-1]["sources"][0]
        self.assertEqual(saved["document_hash"], actual["document_hash"])

    def test_same_document_chunks_retain_distinct_labels_and_sources(self):
        second = {**chunk(), "chunk_id": "learning:shader:1", "text": "Материал задаёт параметры шейдера."}
        self.retriever.candidates = [chunk(), second]
        self.queue_success()
        self.outputs[1]["keep"] = ["S1", "S2"]
        self.outputs[2]["source_ids"] = ["S2", "S1"]
        self.outputs[3]["used_source_ids"] = ["S2", "S1"]
        result = self.service.ask(self.user, self.branch, "Как работают материал и шейдер?")
        self.assertEqual(result["status"], "accepted")
        self.assertEqual([source["chunk_id"] for source in result["sources"]],
                         ["learning:shader:1", "learning:shader:0"])
        filtered = json.loads(self.calls[1]["messages"][-1]["content"])
        self.assertEqual([candidate["label"] for candidate in filtered["candidates"]], ["S1", "S2"])

    def test_unknown_generator_label_rejects_without_judge_and_keeps_cost(self):
        self.queue_success()
        self.outputs[2]["source_ids"] = ["learning:shader:0"]
        result = self.service.ask(self.user, self.branch, "Что такое шейдер?")
        self.assertEqual(result["status"], "rejected")
        self.assertNotIn("answer", result)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.store.ledger(self.user)["calls"][-1]["output_policy"]["code"],
                         "source_ids_invalid")
        self.assertEqual(len(self.store.get_branch(self.user, self.branch)["messages"]), 1)
        self.assertTrue(self.store.ledger(self.user)["complete"])

    def test_validator_cannot_add_label_not_cited_by_generator(self):
        self.retriever.candidates = [chunk(), {**chunk(), "chunk_id": "learning:shader:1"}]
        self.queue_success()
        self.outputs[1]["keep"] = ["S1", "S2"]
        self.outputs[3]["used_source_ids"] = ["S2"]
        result = self.service.ask(self.user, self.branch, "Что такое шейдер?")
        self.assertEqual(result["status"], "rejected")
        self.assertNotIn("answer", result)
        self.assertEqual(len(self.store.get_branch(self.user, self.branch)["messages"]), 1)
        self.assertEqual(self.store.ledger(self.user)["calls"][-1]["output_policy"]["code"],
                         "source_ids_invalid")

    def test_duplicate_filter_labels_cannot_expand_sources(self):
        self.queue_success()
        self.outputs[1]["keep"] = ["S1", "S1"]
        result = self.service.ask(self.user, self.branch, "Что такое шейдер?")
        self.assertEqual(result["status"], "error")
        self.assertEqual(len(self.calls), 2)
        self.assertNotIn("answer", result)
        self.assertEqual(self.store.ledger(self.user)["calls"][-1]["output_policy"]["code"],
                         "source_ids_invalid")

    def test_help_uses_service_docs_and_actual_mcp_branch(self):
        self.retriever.candidates = [{**chunk("service"), "title": "Грани — документация",
                                      "text": "Грани объясняют компьютерную графику."}]
        self.store.update_branch(self.user, self.branch, {"working_state": {"topic": "шейдеры"}})
        self.store.update_profile(self.user, {"role": "QA", "level": "beginner"})
        self.store.remember(self.user, "интерес", "освещение")
        self.store.update_branch(self.user, self.branch, {"conditions": {"depth": "short"}})
        self.store.append_message(self.user, self.branch, "user", "Объясни шейдеры Unity URP")
        self.queue_help_success()
        result = self.service.ask(self.user, self.branch, "/help")
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(self.retriever.calls[0][1], "service")
        query = self.retriever.calls[0][0]
        self.assertIn("сервис", query)
        self.assertIn("документация", query)
        self.assertNotIn("шейдер", query)
        self.assertEqual(self.mcp.calls, 1)
        self.assertEqual([c["stage"] for c in self.store.ledger(self.user)["calls"]],
                         ["embedding", "filter", "generator", "validator"])
        filter_data = json.loads(self.calls[0]["messages"][-1]["content"])
        self.assertEqual(filter_data["original_question"], "/help")
        self.assertIn("назначение", filter_data["question_meaning"])
        self.assertIn("возможности", filter_data["question_meaning"])
        for call in self.calls[1:]:
            payload = call["messages"][-1]["content"]
            self.assertIn('"answer_kind":"help"', payload)
            self.assertIn('"role":"QA"', payload)
            self.assertIn('"интерес"', payload)
            self.assertIn('"depth":"short"', payload)
            self.assertIn("feature/day31", payload)
        self.assertEqual(self.store.get_branch(self.user, self.branch)["working_state"],
                         {"topic": "шейдеры"})
        self.assertEqual(self.store.get_turn(self.user, result["turn_id"])["events"],
                         ["new", "input_check", "context", "sources", "draft", "program_check", "semantic_check", "accepted"])

    def test_help_mentions_in_ordinary_question_remain_learning_questions(self):
        self.queue_success()
        result = self.service.ask(self.user, self.branch, "Что означает команда /help в Unity?")
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(self.retriever.calls[0][1], "learning")
        self.assertEqual(self.mcp.calls, 0)

    def test_help_without_confirming_service_source_stops_before_draft(self):
        self.retriever.candidates = [chunk("service")]
        self.outputs = [{"keep": [], "reason": "Недостаточно сведений"}]
        result = self.service.ask(self.user, self.branch, "/help")
        self.assertEqual(result["status"], "needs_clarification")
        self.assertNotIn("answer", result)
        self.assertEqual([c["stage"] for c in self.store.ledger(self.user)["calls"]],
                         ["embedding", "filter"])
        self.assertEqual(self.mcp.calls, 1)
        self.assertEqual(len(self.store.get_branch(self.user, self.branch)["messages"]), 1)

    def test_help_after_long_history_summarizes_then_skips_generic_preparation(self):
        self.retriever.candidates = [chunk("service")]
        for number in range(6):
            self.store.append_message(self.user, self.branch, "user", f"Шейдер {number}. " + "x" * 2400)
        self.outputs = [{"summary": "Старые реплики были о шейдерах."}]
        self.queue_help_success()
        result = self.service.ask(self.user, self.branch, "/help")
        self.assertEqual(result["status"], "accepted")
        self.assertEqual([c["stage"] for c in self.store.ledger(self.user)["calls"]],
                         ["summary", "embedding", "filter", "generator", "validator"])
        self.assertGreater(self.store.get_branch(self.user, self.branch)["summary_through"], 0)

    def test_model_help_label_does_not_reroute_ordinary_question(self):
        self.queue_success()
        self.outputs[0]["answer_kind"] = "help"
        result = self.service.ask(self.user, self.branch, "Что такое шейдер?")
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(self.retriever.calls[0][1], "learning")
        self.assertEqual(self.mcp.calls, 0)

    def test_missing_sources_never_generates_confident_answer(self):
        self.retriever.candidates = []
        self.outputs = [{"query": "несуществующая тема", "working_state": {},
                         "needs_clarification": False, "clarification": "", "answer_kind": "explanation"}]
        result = self.service.ask(self.user, self.branch, "Объясни несуществующую тему")
        self.assertEqual(result["status"], "needs_clarification")
        self.assertNotIn("answer", result)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.store.get_branch(self.user, self.branch)["messages"]), 1)

    def test_semantic_rejection_retains_cost_but_never_exposes_draft(self):
        self.queue_success()
        self.outputs[-1] = {"accepted": False, "reason": "неверно", "used_source_ids": []}
        result = self.service.ask(self.user, self.branch, "Что такое шейдер?")
        self.assertEqual(result["status"], "rejected")
        self.assertNotIn("answer", result)
        self.assertEqual(len(self.store.get_branch(self.user, self.branch)["messages"]), 1)
        self.assertTrue(self.store.ledger(self.user)["complete"])
        self.assertEqual(self.store.ledger(self.user)["calls"][-1]["output_policy"]["code"],
                         "semantic_rejected")

    def test_explicit_profile_and_condition_are_frozen_for_judge(self):
        self.assertEqual(self.service.ask(self.user, self.branch,
                         "/profile role=QA; level=beginner; depth=short")["status"], "updated")
        self.assertEqual(self.service.ask(self.user, self.branch,
                         "/condition pipeline: URP")["status"], "updated")
        self.queue_success()
        self.service.ask(self.user, self.branch, "Что такое шейдер?")
        judge = self.calls[-1]["messages"][-1]["content"]
        self.assertIn('"role":"QA"', judge)
        self.assertIn('"level":"beginner"', judge)
        self.assertIn('"pipeline":"URP"', judge)
        self.assertEqual(self.store.get_profile(self.user)["level"], "beginner")

    def test_memory_command_is_persistent_without_model_calls(self):
        result = self.service.ask(self.user, self.branch, "/remember pipeline: URP")
        self.assertEqual(result["status"], "updated")
        self.assertEqual(self.store.list_memories(self.user)[0]["value"], "URP")
        self.assertEqual(self.store.ledger(self.user)["calls"], [])
        self.service.ask(self.user, self.branch, "/forget pipeline")
        self.assertEqual(self.store.list_memories(self.user), [])

    def test_oversized_explicit_memory_is_rejected_before_persistence(self):
        result = self.service.ask(self.user, self.branch,
                                  "/remember note: " + "x" * 5000)
        self.assertEqual(result["status"], "input_rejected")
        self.assertEqual(self.store.list_memories(self.user), [])

    def test_input_policy_rejection_survives_restart_without_raw_oversized_text(self):
        secret = "private-" + "x" * 13000
        rejected = self.service.ask(self.user, self.branch, secret)
        self.assertEqual(rejected["status"], "input_rejected")
        self.assertIsNotNone(rejected["turn_id"])
        persisted = Store(self.store.path).get_turn(self.user, rejected["turn_id"])
        self.assertEqual(persisted["status"], "input_rejected")
        self.assertIn("input_too_long", persisted["details"]["check_codes"])
        self.assertNotIn("private-", persisted["question"])
        self.assertEqual(self.store.ledger(self.user)["calls"], [])

    def test_invalid_command_has_durable_rejection_without_model_call(self):
        result = self.service.ask(self.user, self.branch, "/profile unknown=value")
        self.assertEqual(result["status"], "input_rejected")
        self.assertEqual(Store(self.store.path).get_turn(self.user, result["turn_id"])["status"],
                         "input_rejected")
        self.assertEqual(self.store.ledger(self.user)["calls"], [])

    def test_invalid_generator_schema_rejects_without_running_judge(self):
        self.queue_success()
        self.outputs[-2] = {"answer": "# Неподтверждённый заголовок", "source_ids": ["S1"]}
        result = self.service.ask(self.user, self.branch, "Что такое шейдер?")
        self.assertEqual(result["status"], "rejected")
        self.assertNotIn("answer", result)
        self.assertEqual([c["stage"] for c in self.store.ledger(self.user)["calls"]][-1], "generator")
        self.assertEqual(self.store.ledger(self.user)["calls"][-1]["output_policy"]["code"], "markdown_disallowed")

    def test_generator_cannot_publish_guessed_url(self):
        self.queue_success()
        self.outputs[-2]["answer"] = "Шейдер задаёт вид. Подробнее: https://invented.example/guide"
        result = self.service.ask(self.user, self.branch, "Что такое шейдер?")
        self.assertEqual(result["status"], "rejected")
        self.assertNotIn("answer", result)
        self.assertEqual(self.store.ledger(self.user)["calls"][-1]["output_policy"]["code"], "unknown_url")

    def test_invalid_preparation_schema_is_recorded_as_policy_failure(self):
        self.outputs = [{"query": 17, "working_state": {}, "needs_clarification": False,
                         "clarification": "", "answer_kind": "explanation"}]
        result = self.service.ask(self.user, self.branch, "Что такое шейдер?")
        self.assertEqual(result["status"], "error")
        self.assertNotIn("answer", result)
        self.assertEqual(self.store.ledger(self.user)["calls"][-1]["output_policy"]["code"],
                         "invalid_schema")

    def test_failed_dispatched_embedding_stays_unknown_and_blocks_paid_followup(self):
        class FailingRetriever(FakeRetriever):
            def search(self, query, domain="learning", limit=8, on_request=None):
                on_request()
                raise OSError("runtime failed after dispatch")
        self.retriever = FailingRetriever()
        self.service.retriever = self.retriever
        self.outputs = [{"query": "шейдер", "working_state": {}, "needs_clarification": False,
                         "clarification": "", "answer_kind": "explanation"}]
        first = self.service.ask(self.user, self.branch, "Что такое шейдер?")
        self.assertEqual(first["status"], "error")
        self.assertEqual(self.store.ledger(self.user)["calls"][-1]["status"], "unknown")
        second = self.service.ask(self.user, self.branch, "А подробнее?")
        self.assertEqual(second["status"], "error")
        self.assertEqual(len(self.calls), 1)

    def test_manual_compression_preserves_history_and_fork_snapshot(self):
        self.store.update_branch(self.user, self.branch, {"conditions": {"pipeline": "URP"}})
        for number in range(8):
            self.store.append_message(self.user, self.branch, "user", f"Реплика {number}")
        anchor = self.store.get_branch(self.user, self.branch)["messages"][7]["id"]
        self.outputs = [{"summary": "Первые реплики были о шейдерах; последняя поправка важнее старых фактов."}]
        compressed = self.service.compress(self.user, self.branch)
        self.assertEqual(compressed["status"], "compressed")
        state = self.store.get_branch(self.user, self.branch)
        self.assertEqual(len(state["messages"]), 8)
        self.assertEqual(state["summary_through"], 2)
        self.store.update_branch(self.user, self.branch, {"conditions": {"pipeline": "HDRP"}})
        fork = self.store.fork_branch(self.user, self.branch, anchor)
        self.assertEqual(fork["conditions"], {"pipeline": "URP"})
        self.assertEqual(fork["summary"], "")

    def test_long_older_history_is_summarized_in_bounded_batches(self):
        for number in range(30):
            self.store.append_message(self.user, self.branch, "user",
                                      f"Реплика {number}: " + "x" * 1600)
        self.outputs = [{"summary": f"Учтены предыдущие реплики, пакет {i}."} for i in range(8)]
        result = self.service.compress(self.user, self.branch)
        self.assertEqual(result["status"], "compressed")
        summary_request = json.loads(self.calls[0]["messages"][-1]["content"])
        self.assertEqual(summary_request["required_schema"], {"summary": "str"})
        self.assertEqual(summary_request["max_summary_utf8_bytes"], self.settings.summary_budget)
        state = self.store.get_branch(self.user, self.branch)
        self.assertEqual(state["summary_through"], 24)
        self.assertEqual(len(state["messages"]), 30)
        self.assertGreaterEqual(len(self.store.ledger(self.user)["calls"]), 2)

    def test_manual_compression_reduces_oversized_six_message_tail_and_chat_continues(self):
        for number in range(6):
            content = ("Исправление: Unity 6.3, не 6.0. " if number == 5 else f"Реплика {number}. ")
            self.store.append_message(self.user, self.branch, "user", content + "x" * 2400)
        anchor = self.store.get_branch(self.user, self.branch)["messages"][5]["id"]
        self.outputs = [{"summary": "Старые реплики обсуждали Unity 6.0."}]
        compressed = self.service.compress(self.user, self.branch)
        self.assertEqual(compressed["status"], "compressed")
        branch = self.store.get_branch(self.user, self.branch)
        self.assertEqual(branch["summary_through"], 2)
        self.assertEqual(len(branch["messages"]), 6)
        self.assertIn("Исправление: Unity 6.3", branch["messages"][-1]["content"])
        self.assertEqual(self.store.fork_branch(self.user, self.branch, anchor)["summary"], "")
        self.queue_success()
        answered = self.service.ask(self.user, self.branch, "Что такое шейдер?")
        self.assertEqual(answered["status"], "accepted")

    def test_automatic_compression_reduces_oversized_recent_tail_before_preparation(self):
        for number in range(6):
            self.store.append_message(self.user, self.branch, "user", f"Реплика {number}. " + "x" * 2400)
        self.outputs = [{"summary": "Старые реплики сохранены в сводке."}]
        self.queue_success()
        answered = self.service.ask(self.user, self.branch, "Что такое шейдер?")
        self.assertEqual(answered["status"], "accepted")
        self.assertGreater(self.store.get_branch(self.user, self.branch)["summary_through"], 0)
        self.assertEqual(self.store.ledger(self.user)["calls"][0]["stage"], "summary")


if __name__ == "__main__":
    unittest.main()

"""Same-chat support contracts, using the real owned Store."""
import json
import tempfile
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from unittest.mock import patch

from graphics.config import Settings
from graphics.service import ChatService
from graphics.store import Store, SupportIdempotencyConflict
from graphics.support_integration import parse_support, compact_evidence, compact_ticket_action
from graphics.provider import GLMProvider
from tests.test_service import FakeRetriever, chunk


class SupportServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.settings = Settings(root, root / "data", api_key="fixture")
        self.store = Store(self.settings.db_path)
        self.user = self.store.create_user("support-owner", "strong-test-password")
        self.branch = self.store.create_chat(self.user)["branch_id"]

    def test_parser_rejects_ambiguous_operations(self):
        self.assertEqual(parse_support("/support /ticket create Shader | Rendering fails")["tool"],
                         "support_ticket_create")
        self.assertEqual(parse_support("/support /profile role=QA; level=beginner; depth=")["args"]["changes"]["depth"], "")
        for text in ("/support /ticket create no-separator", "/support /ticket get abc extra",
                     "/support /status extra", "/support /profile role=QA; role=artist",
                     "/support /ticket list extra", "/support /unknown"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_support(text)

    def test_evidence_is_bounded_and_labels_omission(self):
        value = compact_evidence({"ticket": {"messages": [{"body": "x" * 10000}]}})
        self.assertLessEqual(len(json.dumps(value, ensure_ascii=False).encode("utf-8")), 1500)
        self.assertTrue(value["omitted"])

    def test_ticket_action_final_utf8_cap_after_omission_flag_changes(self):
        receipt = {"operation_id": "a" * 32, "request_key": "b" * 64, "operation": "create",
                   "ticket_id": "c" * 32, "committed_at": "2026-10-19T00:00:00+00:00"}
        evidence = compact_ticket_action({"receipt": receipt, "ticket": {
            "id": receipt["ticket_id"], "status": "open", "title": "😀" * 160,
            "messages": [{"id": "d" * 32, "created_at": "2026-10-19T00:00:00+00:00",
                          "body": "x" * 172}]}})
        self.assertLessEqual(len(json.dumps(evidence, ensure_ascii=False,
                                            separators=(",", ":")).encode("utf-8")), 1400)
        self.assertEqual(evidence["receipt"], receipt)
        self.assertEqual(evidence["ticket"]["status"], "open")
        self.assertTrue(evidence["latest_message"]["body_excerpt"].startswith("x"))

    def test_request_claim_replay_and_conflict(self):
        first = self.store.begin_support_turn(self.user, self.branch, "/support /status", "stable-id")
        same = self.store.begin_support_turn(self.user, self.branch, "/support /status", "stable-id")
        self.assertEqual(first["id"], same["id"])
        self.assertTrue(same["replay"])
        with self.assertRaises(SupportIdempotencyConflict):
            self.store.begin_support_turn(self.user, self.branch, "/support /ticket list", "stable-id")

    def test_control_notice_unlocks_existing_db(self):
        first = self.store.begin_support_turn(self.user, self.branch, "/support /status", "one")
        self.store.move_turn(first["id"], "input_check")
        self.store.publish_support_control(self.user, self.branch, first["id"], "Проверка выполнена")
        reopened = Store(self.settings.db_path)
        second = reopened.begin_support_turn(self.user, self.branch, "/support /me", "two")
        self.assertNotEqual(first["id"], second["id"])

    def test_old_day31_active_index_is_migrated_for_controls(self):
        with self.store._connect() as db:
            db.execute("DROP INDEX one_active_turn_per_branch")
            db.execute("CREATE UNIQUE INDEX one_active_turn_per_branch ON turns(branch_id) "
                       "WHERE status NOT IN ('input_rejected','needs_clarification','rejected',"
                       "'error','accepted','compressed')")
        reopened = Store(self.settings.db_path)
        first = reopened.begin_support_turn(self.user, self.branch, "/support /status", "old-one")
        with self.assertRaises(RuntimeError):
            reopened.begin_support_turn(self.user, self.branch, "/support /me", "overlap")
        reopened.move_turn(first["id"], "input_check")
        reopened.publish_support_control(self.user, self.branch, first["id"], "Подтверждено")
        self.assertNotEqual(first["id"], reopened.begin_support_turn(
            self.user, self.branch, "/support /me", "old-two")["id"])

    def test_failed_index_replacement_rolls_back_drop(self):
        old_definition = ("CREATE UNIQUE INDEX one_active_turn_per_branch ON turns(branch_id) "
                          "WHERE status NOT IN ('input_rejected','needs_clarification','rejected',"
                          "'error','accepted','compressed')")
        with self.store._connect() as db:
            db.execute("DROP INDEX one_active_turn_per_branch")
            db.execute(old_definition)

        original_connect = Store._connect

        class FailingConnection:
            def __init__(self, db):
                self.db = db

            def __getattr__(self, name):
                return getattr(self.db, name)

            def execute(self, statement, *args):
                if statement.startswith("CREATE UNIQUE INDEX one_active_turn_per_branch"):
                    raise RuntimeError("injected after DROP")
                return self.db.execute(statement, *args)

        @contextmanager
        def fail_after_drop(store):
            with original_connect(store) as db:
                yield FailingConnection(db)

        with patch.object(Store, "_connect", fail_after_drop):
            with self.assertRaisesRegex(RuntimeError, "injected after DROP"):
                Store(self.settings.db_path)
        with self.store._connect() as db:
            definition = db.execute("SELECT sql FROM sqlite_master WHERE name=?",
                                    ("one_active_turn_per_branch",)).fetchone()
        self.assertIsNotNone(definition)
        self.assertEqual(definition["sql"], old_definition)

    def _real_service(self, outputs=None, retriever=None):
        app_root = Path(__file__).resolve().parents[1]
        settings = Settings(app_root, self.settings.data_dir, api_key="fixture")
        calls = []
        pending = list(outputs or [])
        def transport(payload):
            calls.append(json.loads(payload["messages"][-1]["content"]))
            item = pending.pop(0)
            if isinstance(item, Exception):
                raise item
            return {"model": settings.model,
                    "choices": [{"message": {"content": json.dumps(item, ensure_ascii=False)}}],
                    "usage": {"prompt_tokens": 80, "completion_tokens": 20, "total_tokens": 100,
                              "prompt_tokens_details": {"cached_tokens": 0}}}
        provider = GLMProvider(settings, self.store, transport=transport)
        service = ChatService(settings, self.store, provider=provider,
                              retriever=retriever or FakeRetriever([chunk("service")]))
        return service, calls

    def test_support_failure_diagnostic_is_bounded_private_and_keeps_safe_response(self):
        marker = "SECRET_DIAGNOSTIC_SENTINEL_" + "x" * 12000
        class FailedClient:
            def call(self, name, args):
                try:
                    raise OSError(13, marker)
                except OSError as cause:
                    try:
                        raise RuntimeError(marker) from cause
                    except RuntimeError as nested:
                        raise ExceptionGroup(marker, [nested] + [ValueError(marker) for _ in range(50)])
        service, calls = self._real_service()
        service.support_client_factory = lambda *_: FailedClient()
        with self.assertLogs("graphics.service", level="WARNING") as captured:
            result = service.ask(self.user, self.branch, "/support /me", request_id="diagnostic-private")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["message"], "Не удалось завершить ответ")
        self.assertEqual(calls, [])
        self.assertEqual(len(captured.records), 1)
        record = captured.records[0]
        self.assertIsNone(record.exc_info)
        self.assertIsNone(record.stack_info)
        encoded = record.getMessage()
        diagnostic = json.loads(encoded)
        self.assertEqual(diagnostic["event"], "support_request_failed")
        self.assertEqual(diagnostic["branch_id"], self.branch)
        self.assertEqual(diagnostic["turn_id"], result["turn_id"])
        self.assertIn("ExceptionGroup", encoded)
        self.assertIn("PermissionError", encoded)
        self.assertIn('"errno":13', encoded.replace(" ", ""))
        self.assertIn("test_support_service.py", encoded)
        self.assertIn('"function":"call"', encoded.replace(" ", ""))
        self.assertLessEqual(len(encoded.encode("utf-8")), 12000)
        self.assertNotIn("SECRET_DIAGNOSTIC_SENTINEL", encoded)
        self.assertNotIn(str(Path(__file__).resolve().parent), encoded)
        self.assertNotIn("raise OSError", encoded)
        self.assertNotIn("SECRET_DIAGNOSTIC_SENTINEL", json.dumps(result))
        self.assertNotIn("SECRET_DIAGNOSTIC_SENTINEL", json.dumps(
            self.store.get_branch(self.user, self.branch)["messages"]))
        self.assertEqual(self.store.get_turn(self.user, result["turn_id"])["status"], "error")

    def test_broken_diagnostic_logger_preserves_confirmation_and_never_retries(self):
        marker = "SECRET_BROKEN_LOGGER"
        service, calls = self._real_service([RuntimeError(marker)])
        text = "/support /ticket create Diagnostic | Synthetic problem\nExplain the result"
        with patch("graphics.service._SUPPORT_LOG.warning", side_effect=RuntimeError(marker)) as warning:
            result = service.ask(self.user, self.branch, text, request_id="broken-logger")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["message"], "Не удалось завершить ответ")
        self.assertEqual(warning.call_count, 1)
        receipt = result["support_confirmation"]["receipt"]
        self.assertEqual(receipt["operation"], "create")
        self.assertEqual(self.store.support_actions(self.user, self.branch, result["turn_id"])[0]["status"], "confirmed")
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(result["usage"]["calls"]), 1)
        again = service.ask(self.user, self.branch, text, request_id="broken-logger")
        self.assertEqual(again["support_confirmation"]["receipt"], receipt)
        self.assertEqual(again["usage"], result["usage"])
        self.assertEqual(len(calls), 1)
        self.assertNotIn(marker, json.dumps(result))
        self.assertNotIn(marker, json.dumps(self.store.get_branch(self.user, self.branch)["messages"]))

    def test_learning_failure_does_not_emit_support_diagnostic(self):
        service, calls = self._real_service([RuntimeError("PRIVATE_LEARNING_ERROR")])
        with patch("graphics.service._SUPPORT_LOG.warning") as warning:
            result = service.ask(self.user, self.branch, "Что такое шейдер?")
        warning.assert_not_called()
        self.assertEqual(result["status"], "error")
        self.assertEqual(len(calls), 1)

    def test_first_real_mcp_status_reports_unavailable_without_creating_ticket_storage(self):
        from datetime import datetime
        service, calls = self._real_service()
        ticket_db = service.settings.data_dir / "support.sqlite3"
        self.assertTrue(service.settings.db_path.is_file())
        self.assertFalse(ticket_db.exists())
        responses = []
        original_factory = service.support_client_factory
        class ObservedClient:
            def __init__(self, client):
                self.client = client
            def call(self, name, args):
                result = self.client.call(name, args)
                responses.append((name, result))
                return result
        service.support_client_factory = lambda *args: ObservedClient(original_factory(*args))
        result = service.ask(self.user, self.branch, "/support /status", request_id="first-status")
        self.assertEqual(result["status"], "support_control", result)
        self.assertIn("storage: unavailable", result["message"])
        self.assertIn("main_storage: ok", result["message"])
        self.assertEqual([name for name, _ in responses], ["support_status"])
        envelope = responses[0][1]
        self.assertTrue(envelope["ok"], envelope)
        self.assertEqual(envelope["integration_status"], "integrated")
        data = envelope["data"]
        self.assertEqual(data["storage"]["status"], "unavailable")
        self.assertIsNotNone(datetime.fromisoformat(data["storage"]["checked_at"]).utcoffset())
        self.assertEqual(data["main_storage"], {"status": "ok", "scope": "main_store"})
        for dependency in ("main_service", "llm", "rag"):
            self.assertEqual(data[dependency]["status"], "unknown")
        self.assertEqual(envelope["accounting"]["llm_calls"], 0)
        self.assertEqual(calls, [])
        self.assertEqual(self.store.ledger(self.user)["calls"], [])
        self.assertFalse(ticket_db.exists())

    def test_real_mcp_profile_control_changes_later_learning_context(self):
        service, calls = self._real_service()
        changed = service.ask(self.user, self.branch,
                              "/support /profile role=QA; level=beginner", request_id="profile-one")
        self.assertEqual(changed["status"], "support_control")
        self.assertEqual(len(calls), 0)
        self.assertEqual(self.store.get_profile(self.user)["role"], "QA")
        service, calls = self._real_service([
            {"query": "шейдер", "working_state": {}, "needs_clarification": False,
             "clarification": "", "answer_kind": "explanation"},
            {"keep": ["S1"], "reason": "yes"},
            {"answer": "Шейдер задаёт вид поверхности.", "source_ids": ["S1"]},
            {"accepted": True, "reason": "yes", "used_source_ids": ["S1"]},
        ], FakeRetriever([chunk("learning")]))
        answer = service.ask(self.user, self.branch, "Что такое шейдер?")
        self.assertEqual(answer["status"], "accepted", (answer, calls, self.store.get_turn(self.user, answer["turn_id"])))
        self.assertEqual(calls[0]["context"]["profile"]["role"], "QA")

    def test_real_mcp_faq_context_reaches_generator_and_same_validator(self):
        faq = (Path(__file__).resolve().parents[1] / "corpus" / "support-faq.md").read_text(encoding="utf-8")
        faq_chunk = chunk("service")
        faq_chunk.update({"source_id": "support-faq", "title": "Support FAQ", "text": faq[:1200]})
        service, calls = self._real_service([
            {"query": "поддержка сервис", "working_state": {}, "needs_clarification": False,
             "clarification": "", "answer_kind": "help"},
            {"keep": ["S1"], "reason": "FAQ"},
            {"answer": "Поддержка помогает разобраться с сервисом; обращение создаётся явной командой.",
             "source_ids": ["S1"]},
            {"accepted": True, "reason": "FAQ", "used_source_ids": ["S1"]},
        ], FakeRetriever([faq_chunk]))
        result = service.ask(self.user, self.branch, "/support Как создать обращение?", request_id="faq-one")
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(len([item for item in calls if item["stage"] == "validator"]), 1)
        for stage in ("generator", "validator"):
            payload = next(item for item in calls if item["stage"] == stage)
            self.assertEqual(payload["answer_kind"], "support")
            self.assertIn("support_user_get", payload["tool_results"])
            self.assertIn("support_diagnostics", payload["tool_results"])
            self.assertIn("Support FAQ", payload["sources"][0]["title"])
            self.assertIn("profile", payload["context"])
            self.assertIn("conditions", payload["context"])
        verdict_payload = next(item for item in calls if item["stage"] == "validator")
        self.assertEqual(verdict_payload["validation_mode"], "answer")
        self.assertIn("FAQ", verdict_payload["validation_criteria"])
        self.assertIn("квитанц", verdict_payload["validation_criteria"])
        self.assertIn("приват", verdict_payload["validation_criteria"])
        self.assertEqual(service.retriever.calls[-1][1], "service")

    def test_write_then_generator_failure_retains_receipt_and_replay(self):
        service, calls = self._real_service([
            {"query": "тикет", "working_state": {}, "needs_clarification": False,
             "clarification": "", "answer_kind": "help"},
            {"keep": ["S1"], "reason": "FAQ"},
            RuntimeError("provider failed"),
        ])
        text = "/support /ticket create Сбой | Сервис не отвечает\nЧто произошло?"
        result = service.ask(self.user, self.branch, text, request_id="create-one")
        self.assertEqual(result["status"], "error")
        self.assertIn("support_confirmation", result)
        receipt = result["support_confirmation"]["receipt"]
        self.assertEqual(receipt["operation"], "create")
        replay = service.ask(self.user, self.branch, text, request_id="create-one")
        self.assertEqual(replay["turn_id"], result["turn_id"])
        self.assertEqual(replay["support_confirmation"]["receipt"], receipt)
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(self.store.support_actions(self.user, self.branch, result["turn_id"])), 1)

    def test_ticket_get_with_question_passes_owned_ticket_to_generator_and_judge(self):
        service, _ = self._real_service()
        created = service.ask(self.user, self.branch,
                              "/support /ticket create Сбой | Исчез материал", request_id="new-ticket")
        ticket_id = created["support_confirmation"]["receipt"]["ticket_id"]
        service, calls = self._real_service([
            {"query": "обращение", "working_state": {}, "needs_clarification": False,
             "clarification": "", "answer_kind": "support"},
            {"keep": ["S1"], "reason": "FAQ"},
            {"answer": "Ваше обращение открыто; причина пока неизвестна.", "source_ids": ["S1"]},
            {"accepted": True, "reason": "Подтверждено", "used_source_ids": ["S1"]},
        ])
        result = service.ask(self.user, self.branch,
                             f"/support /ticket get {ticket_id}\nЧто известно о моём обращении?",
                             request_id="get-ticket")
        self.assertEqual(result["status"], "accepted")
        for stage in ("generator", "validator"):
            tool = next(item for item in calls if item["stage"] == stage)["tool_results"]["support_ticket_get"]
            self.assertIn(ticket_id, tool["preview"])
            self.assertIn("Исчез материал", tool["preview"])
        self.assertIn("FAQ", next(item for item in calls if item["stage"] == "validator")["validation_criteria"])

    def test_ticket_append_with_long_history_keeps_current_receipt_and_message_in_both_stages(self):
        service, _ = self._real_service()
        created = service.ask(self.user, self.branch,
                              "/support /ticket create История | " + "старое" * 600,
                              request_id="long-history")
        ticket_id = created["support_confirmation"]["receipt"]["ticket_id"]
        service, calls = self._real_service([
            {"query": "обращение", "working_state": {}, "needs_clarification": False,
             "clarification": "", "answer_kind": "support"},
            {"keep": ["S1"], "reason": "FAQ"},
            {"answer": "Новое сообщение добавлено.", "source_ids": ["S1"]},
            {"accepted": True, "reason": "Подтверждено", "used_source_ids": ["S1"]},
        ])
        result = service.ask(self.user, self.branch,
                             f"/support /ticket append {ticket_id} | Новый факт: зелёный артефакт.\nЧто известно теперь?",
                             request_id="append-long-history")
        self.assertEqual(result["status"], "accepted", result)
        self.assertEqual(len([item for item in calls if item["stage"] == "validator"]), 1)
        for stage in ("generator", "validator"):
            evidence = next(item for item in calls if item["stage"] == stage)["tool_results"]["support_ticket_append"]
            self.assertEqual(evidence["receipt"], result["support_confirmation"]["receipt"])
            self.assertEqual(evidence["ticket"]["id"], ticket_id)
            self.assertEqual(evidence["ticket"]["status"], "open")
            self.assertIn("Новый факт: зелёный артефакт.", evidence["latest_message"]["body_excerpt"])
            self.assertTrue(evidence["omitted"])
            self.assertLessEqual(len(json.dumps(evidence, ensure_ascii=False,
                                                separators=(",", ":")).encode("utf-8")), 1500)

    def test_ticket_create_with_long_description_keeps_receipt_and_current_excerpt(self):
        service, calls = self._real_service([
            {"query": "обращение", "working_state": {}, "needs_clarification": False,
             "clarification": "", "answer_kind": "support"},
            {"keep": ["S1"], "reason": "FAQ"},
            {"answer": "Обращение создано.", "source_ids": ["S1"]},
            {"accepted": True, "reason": "Подтверждено", "used_source_ids": ["S1"]},
        ])
        result = service.ask(self.user, self.branch,
                             "/support /ticket create Большое описание | Начало текущей проблемы. " + "подробность " * 350 +
                             "\nОбъясни состояние обращения.", request_id="create-long-description")
        self.assertEqual(result["status"], "accepted", result)
        for stage in ("generator", "validator"):
            evidence = next(item for item in calls if item["stage"] == stage)["tool_results"]["support_ticket_create"]
            self.assertEqual(evidence["receipt"], result["support_confirmation"]["receipt"])
            self.assertEqual(evidence["ticket"]["id"], evidence["receipt"]["ticket_id"])
            self.assertIn("Начало текущей проблемы.", evidence["latest_message"]["body_excerpt"])
            self.assertTrue(evidence["latest_message"]["omitted"])

    def test_write_then_semantic_rejection_hides_draft_and_retains_receipt(self):
        service, calls = self._real_service([
            {"query": "тикет", "working_state": {}, "needs_clarification": False,
             "clarification": "", "answer_kind": "support"},
            {"keep": ["S1"], "reason": "FAQ"},
            {"answer": "PRIVATE_DRAFT_SENTINEL", "source_ids": ["S1"]},
            {"accepted": False, "reason": "unsupported", "used_source_ids": []},
        ])
        result = service.ask(self.user, self.branch,
                             "/support /ticket create Ошибка | Шейдер не работает\nЧто дальше?",
                             request_id="semantic-one")
        self.assertEqual(result["status"], "rejected")
        self.assertIn("support_confirmation", result)
        self.assertEqual(len([item for item in calls if item["stage"] == "validator"]), 1)
        messages = self.store.get_branch(self.user, self.branch)["messages"]
        self.assertTrue(all("PRIVATE_DRAFT_SENTINEL" not in item["content"] for item in messages))

    def test_invalid_shared_verdict_after_write_keeps_receipt_and_hides_draft(self):
        service, calls = self._real_service([
            {"query": "тикет", "working_state": {}, "needs_clarification": False,
             "clarification": "", "answer_kind": "support"},
            {"keep": ["S1"], "reason": "FAQ"},
            {"answer": "PRIVATE_INVALID_DRAFT", "source_ids": ["S1"]},
            {"accepted": True, "reason": "ошибочная метка", "used_source_ids": ["X"]},
        ])
        text = "/support /ticket create Невердикт | Нет изображения\nЧто дальше?"
        result = service.ask(self.user, self.branch, text, request_id="invalid-verdict")
        self.assertEqual(result["status"], "rejected")
        receipt = result["support_confirmation"]["receipt"]
        self.assertEqual(receipt["operation"], "create")
        self.assertEqual(len([item for item in calls if item["stage"] == "validator"]), 1)
        self.assertEqual(service.ask(self.user, self.branch, text, request_id="invalid-verdict")["support_confirmation"]["receipt"], receipt)
        self.assertEqual(len(calls), 4)
        self.assertTrue(all("PRIVATE_INVALID_DRAFT" not in item["content"]
                            for item in self.store.get_branch(self.user, self.branch)["messages"]))

    def test_request_ownership_and_branch_namespace(self):
        second = self.store.create_chat(self.user)["branch_id"]
        other = self.store.create_user("different-owner", "different-password-123")
        with self.assertRaises(PermissionError):
            self.store.begin_support_turn(other, self.branch, "/support /status", "shared-id")
        first = self.store.begin_support_turn(self.user, self.branch, "/support /status", "shared-id")
        next_branch = self.store.begin_support_turn(self.user, second, "/support /status", "shared-id")
        self.assertNotEqual(first["id"], next_branch["id"])


    def test_lost_real_mcp_mutation_response_recovers_original_receipt_without_provider(self):
        import sqlite3
        for operation in ("create", "append", "close"):
            with self.subTest(operation=operation):
                branch = self.store.create_chat(self.user)["branch_id"]
                service, calls = self._real_service()
                ticket_id = None
                if operation != "create":
                    created = service.ask(self.user, branch,
                        "/support /ticket create Seed | Initial", request_id="seed")
                    ticket_id = created["support_confirmation"]["receipt"]["ticket_id"]
                text = ({"create": "/support /ticket create Lost | PRIVATE_TICKET_BODY",
                         "append": f"/support /ticket append {ticket_id} | PRIVATE_TICKET_BODY",
                         "close": f"/support /ticket close {ticket_id}"}[operation] +
                        "\nExplain the result; PRIVATE_DRAFT must never be generated")
                original_factory = service.support_client_factory
                committed = []
                dispatched = []
                class LostResponse:
                    def __init__(self, client):
                        self.client = client
                    def call(self, name, args):
                        dispatched.append(name)
                        result = self.client.call(name, args)
                        if name == "support_ticket_" + operation:
                            committed.append(result["data"]["receipt"])
                            raise TimeoutError("response lost after real MCP commit")
                        return result
                service.support_client_factory = lambda *args: LostResponse(original_factory(*args))
                first = service.ask(self.user, branch, text, request_id="lost-" + operation)
                self.assertEqual(first["status"], "error")
                before = self.store.get_turn(self.user, first["turn_id"])
                usage = first["usage"]
                service, retry_calls = self._real_service()
                service.store = Store(self.settings.db_path)
                retry_dispatched = []
                retry_factory = service.support_client_factory
                class ReceiptOnly:
                    def __init__(self, client):
                        self.client = client
                    def call(self, name, args):
                        retry_dispatched.append(name)
                        return self.client.call(name, args)
                service.support_client_factory = lambda *args: ReceiptOnly(retry_factory(*args))
                result = service.ask(self.user, branch, text, request_id="lost-" + operation)
                self.assertEqual(result["turn_id"], first["turn_id"])
                self.assertEqual(result["support_confirmation"]["receipt"], committed[0])
                self.assertEqual(self.store.support_actions(self.user, branch, first["turn_id"])[0]["status"], "confirmed")
                self.assertEqual(self.store.get_turn(self.user, first["turn_id"]), before)
                self.assertEqual(result["usage"], usage)
                self.assertEqual(calls + retry_calls, [])
                self.assertEqual(dispatched, ["support_ticket_" + operation])
                self.assertNotIn("PRIVATE_", json.dumps(result))
                with closing(sqlite3.connect(service.settings.data_dir / "support.sqlite3")) as db:
                    actual_id = committed[0]["ticket_id"]
                    self.assertEqual(db.execute("SELECT count(*) FROM support_operations WHERE ticket_id=? AND operation=?",
                                                (actual_id, operation)).fetchone()[0], 1)
                    self.assertEqual(db.execute("SELECT count(*) FROM support_messages WHERE ticket_id=?",
                                                (actual_id,)).fetchone()[0], 2 if operation == "append" else 1)
                    self.assertEqual(db.execute("SELECT status FROM support_tickets WHERE id=?",
                                                (actual_id,)).fetchone()[0], "closed_by_user" if operation == "close" else "open")
                again = service.ask(self.user, branch, text, request_id="lost-" + operation)
                self.assertEqual(again["support_confirmation"], result["support_confirmation"])
                conflict = service.ask(self.user, branch, text + " changed", request_id="lost-" + operation)
                self.assertEqual(conflict["status"], "input_rejected")
                self.assertEqual(retry_calls, [])
                self.assertEqual(retry_dispatched, ["support_ticket_receipt"])

    def test_unknown_retry_preserves_uncertainty_when_receipt_cannot_be_found(self):
        for storage in ("missing", "absent", "corrupt", "transport"):
            with self.subTest(storage=storage):
                service, calls = self._real_service()
                branch = self.store.create_chat(self.user)["branch_id"]
                database = service.settings.data_dir / "support.sqlite3"
                if storage == "absent":
                    service.ask(self.user, branch, "/support /ticket create Seed | Existing", request_id="seed")
                class LostBeforeCommit:
                    def call(self, name, args):
                        raise TimeoutError("request delivery unknown")
                original_factory = service.support_client_factory
                service.support_client_factory = lambda *_: LostBeforeCommit()
                text = "/support /ticket create Missing | PRIVATE_ABSENT_BODY"
                first = service.ask(self.user, branch, text, request_id="absent-" + storage)
                if storage == "corrupt":
                    database.write_bytes(b"incompatible support database")
                before_bytes = database.read_bytes() if database.exists() else None
                before_turn = self.store.get_turn(self.user, first["turn_id"])
                service.support_client_factory = (lambda *_: LostBeforeCommit()) if storage == "transport" else original_factory
                again = service.ask(self.user, branch, text, request_id="absent-" + storage)
                self.assertEqual(again["turn_id"], first["turn_id"])
                self.assertEqual(again["status"], "error")
                self.assertIn("неизвестен", again["message"])
                self.assertNotIn("support_confirmation", again)
                self.assertNotIn("PRIVATE_ABSENT_BODY", json.dumps(again))
                self.assertEqual(self.store.support_actions(self.user, branch, first["turn_id"])[0]["status"], "unknown")
                self.assertEqual(self.store.get_turn(self.user, first["turn_id"]), before_turn)
                self.assertEqual(database.read_bytes() if database.exists() else None, before_bytes)
                self.assertEqual(calls, [])
                if database.exists():
                    database.unlink()

    def test_pending_first_request_never_looks_up_or_repeats_a_mutation(self):
        from graphics.support_integration import operation_key
        service, calls = self._real_service()
        text = "/support /ticket create Pending | Waiting"
        turn = self.store.begin_support_turn(self.user, self.branch, text, "pending-id")["id"]
        self.store.move_turn(turn, "input_check")
        self.store.record_support_action(self.user, self.branch, turn,
            operation_key(self.user, self.branch, "pending-id"), "support_ticket_create", "unknown")
        with patch.object(service, "support_client_factory") as factory:
            result = service.ask(self.user, self.branch, text, request_id="pending-id")
            factory.assert_not_called()
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["turn_id"], turn)
        self.assertEqual(calls, [])

    def test_unknown_tool_timeout_is_not_replayed_as_a_new_write(self):
        class TimedOutClient:
            calls = 0
            def call(self, name, args):
                self.calls += 1
                raise TimeoutError("uncertain MCP outcome")
        client = TimedOutClient()
        service = ChatService(self.settings, self.store,
                              support_client_factory=lambda *_: client)
        text = "/support /ticket create Сбой | Нет ответа"
        first = service.ask(self.user, self.branch, text, request_id="timeout-id")
        self.assertEqual(first["status"], "error")
        self.assertIn("неизвестен", first["message"])
        self.assertEqual(self.store.support_actions(self.user, self.branch, first["turn_id"])[0]["status"], "unknown")
        again = service.ask(self.user, self.branch, text, request_id="timeout-id")
        self.assertEqual(again["turn_id"], first["turn_id"])
        self.assertEqual(client.calls, 2)
        self.assertIn("неизвестен", again["message"])


if __name__ == "__main__":
    unittest.main()

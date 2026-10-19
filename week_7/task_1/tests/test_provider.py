import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from graphics.config import Settings
from graphics.provider import GLMProvider, ProviderError
from graphics.store import Store


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.settings = Settings(root, root / "data", api_key="test secret")
        self.store = Store(root / "db.sqlite3")
        self.user = self.store.create_user("owner", "long password")
        self.branch = self.store.create_chat(self.user)["branch_id"]
        self.turn = self.store.begin_turn(self.user, self.branch, "question")["id"]
        for state in ("input_check", "context", "sources", "draft"):
            self.store.move_turn(self.turn, state)

    def test_call_before_input_check_never_reaches_transport(self):
        other_branch = self.store.create_chat(self.user)["branch_id"]
        new_turn = self.store.begin_turn(self.user, other_branch, "question")["id"]
        sent = []
        provider = GLMProvider(self.settings, self.store, lambda payload: sent.append(payload))
        with self.assertRaises(ProviderError):
            provider.call(self.user, other_branch, new_turn, "generator", [])
        self.store.move_turn(new_turn, "input_check")
        self.store.end_turn(new_turn, "input_rejected")
        with self.assertRaises(ProviderError):
            provider.call(self.user, other_branch, new_turn, "generator", [])
        self.assertEqual(sent, [])
        self.assertEqual(self.store.ledger(self.user)["calls"], [])

    def test_validator_cannot_run_before_semantic_check(self):
        sent = []
        provider = GLMProvider(self.settings, self.store, lambda payload: sent.append(payload))
        with self.assertRaises(ProviderError):
            provider.call(self.user, self.branch, self.turn, "validator", [])
        self.assertEqual(sent, [])
        self.assertEqual(self.store.ledger(self.user)["calls"], [])

    def test_success_payload_usage_and_reasoning_not_double_billed(self):
        payloads = []
        def transport(payload):
            payloads.append(payload)
            return {"model": "glm-5.3-flash", "choices": [{"message": {"content": "ok"}}],
                    "usage": {"prompt_tokens": 1000, "completion_tokens": 200,
                              "total_tokens": 1200,
                              "prompt_tokens_details": {"cached_tokens": 100},
                              "completion_tokens_details": {"reasoning_tokens": 50}}}
        result = GLMProvider(self.settings, self.store, transport).call(
            self.user, self.branch, self.turn, "generator", [{"role": "user", "content": "hi"}])
        self.assertEqual(result["content"], "ok")
        self.assertAlmostEqual(result["cost_estimated_usd"], 0.000238)
        self.assertEqual(result["usage"]["cached_input_tokens"], 100)
        self.assertEqual(result["usage"]["reasoning_tokens"], 50)
        self.assertEqual(payloads[0]["model"], "glm-5.3-flash")
        self.assertEqual(payloads[0]["thinking"], {"type": "enabled"})
        self.assertEqual(payloads[0]["reasoning_effort"], "low")
        self.assertEqual(payloads[0]["response_format"], {"type": "json_object"})
        self.assertEqual(payloads[0]["stream"], False)
        self.assertTrue(self.store.ledger(self.user)["complete"])
        receipt = self.store.ledger(self.user)["calls"][-1]["metadata"]
        self.assertGreater(receipt["request_bytes"], 0)
        self.assertEqual(receipt["request_options"]["reasoning_effort"], "low")
        self.assertEqual(receipt["operational_input_budget_bytes"], self.settings.input_budget)

    def test_timeout_leaves_durable_unknown_and_no_retry(self):
        attempts = []
        def timeout(payload):
            attempts.append(payload)
            raise TimeoutError("secret request body")
        provider = GLMProvider(self.settings, self.store, timeout)
        with self.assertRaises(ProviderError) as caught:
            provider.call(self.user, self.branch, self.turn, "generator", [{"role": "user", "content": "private"}])
        self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(len(attempts), 1)
        with self.assertRaises(ProviderError):
            GLMProvider(self.settings, Store(self.store.path), timeout).call(
                self.user, self.branch, self.turn, "validator", [])
        self.assertEqual(len(attempts), 1)

    def test_input_policy_is_durable_before_transport(self):
        observed = []
        def transport(payload):
            call = self.store.ledger(self.user)["calls"][-1]
            observed.append((call["input_policy"], call["request_dispatched"]))
            return {"model": "glm-5.3-flash", "choices": [{"message": {"content": "ok"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2,
                              "prompt_tokens_details": {"cached_tokens": 0},
                              "completion_tokens_details": {"reasoning_tokens": 0}}}
        GLMProvider(self.settings, self.store, transport).call(
            self.user, self.branch, self.turn, "generator", [{"role": "user", "content": "hi"}])
        self.assertEqual(observed, [({"ok": True, "code": "bounded_provider_payload"}, 1)])

    def test_missing_usage_blocks_next_paid_stage(self):
        provider = GLMProvider(self.settings, self.store, lambda _: {
            "choices": [{"message": {"content": "draft"}}]})
        with self.assertRaises(ProviderError):
            provider.call(self.user, self.branch, self.turn, "generator", [])
        self.assertFalse(self.store.ledger(self.user)["complete"])

    def test_missing_cached_counter_is_unknown_instead_of_zero(self):
        provider = GLMProvider(self.settings, self.store, lambda _: {
            "choices": [{"message": {"content": "draft"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}})
        with self.assertRaises(ProviderError):
            provider.call(self.user, self.branch, self.turn, "generator", [])
        self.assertIsNone(self.store.ledger(self.user)["cost_estimated_usd"])

    def test_complete_encoded_payload_must_fit_budget_before_receipt_or_transport(self):
        settings = replace(self.settings, input_budget=60)
        sent = []
        provider = GLMProvider(settings, self.store, lambda payload: sent.append(payload))
        messages = [{"role": "user", "content": "hi"}]
        for json_mode in (True, False):
            with self.assertRaisesRegex(ProviderError, "бюджет"):
                provider.call(self.user, self.branch, self.turn, "generator", messages,
                              json_mode=json_mode)
        self.assertEqual(sent, [])
        self.assertEqual(self.store.ledger(self.user)["calls"], [])


if __name__ == "__main__":
    unittest.main()

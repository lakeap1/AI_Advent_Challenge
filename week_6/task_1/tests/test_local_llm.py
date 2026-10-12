import json
import tempfile
import unittest
from pathlib import Path

from local_llm import LocalAgent


GOOD = {
    "model": "qwen3.5:9b",
    "message": {"role": "assistant", "content": "Roughness управляет шириной блика."},
    "done": True,
    "done_reason": "stop",
    "prompt_eval_count": 12,
    "eval_count": 8,
    "total_duration": 2_000_000_000,
    "load_duration": 100_000_000,
    "prompt_eval_duration": 200_000_000,
    "eval_duration": 1_700_000_000,
}


class Transport:
    def __init__(self, response=GOOD, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def __call__(self, method, path, payload):
        self.calls.append((method, path, payload))
        if self.error:
            raise self.error
        return self.response


class LocalAgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ledger = Path(self.temp.name) / "requests.jsonl"
        self.config = Path(__file__).resolve().parents[1] / "config.toml"

    def agent(self, transport):
        return LocalAgent(self.config, self.ledger, transport=transport)

    def entries(self):
        return [json.loads(line) for line in self.ledger.read_text(encoding="utf-8").splitlines()]

    def test_input_rejection_writes_receipt_without_call_or_text(self):
        transport = Transport()
        result = self.agent(transport).run("   ")
        self.assertEqual("input_rejected", result["status"])
        self.assertEqual([], transport.calls)
        entry = self.entries()[0]
        self.assertEqual("rejected", entry["input_policy"]["status"])
        self.assertEqual("not_run", entry["output_policy"]["status"])
        self.assertIsNone(entry["usage"]["total_tokens"])
        self.assertNotIn("prompt", entry)

    def test_overlong_input_rejected_before_api(self):
        transport = Transport()
        result = self.agent(transport).run("x" * 2001)
        self.assertEqual("input_rejected", result["status"])
        self.assertEqual("input_too_long", self.entries()[0]["input_policy"]["reason"])
        self.assertEqual([], transport.calls)

    def test_http_payload_and_complete_usage_receipt(self):
        transport = Transport()
        result = self.agent(transport).run("Что делает roughness?", scenario_id="simple")
        self.assertEqual("ok", result["status"])
        self.assertEqual("POST", transport.calls[0][0])
        self.assertEqual("/api/chat", transport.calls[0][1])
        payload = transport.calls[0][2]
        self.assertEqual("qwen3.5:9b", payload["model"])
        self.assertIs(payload["stream"], False)
        self.assertIs(payload["think"], False)
        self.assertEqual({"num_ctx": 8192, "num_predict": 1024, "temperature": .7, "top_p": .8, "top_k": 20}, payload["options"])
        self.assertEqual(["system", "user"], [item["role"] for item in payload["messages"]])
        self.assertIn("без Markdown", payload["messages"][0]["content"])
        entry = self.entries()[0]
        self.assertEqual(20, entry["usage"]["total_tokens"])
        self.assertIsNone(entry["usage"]["cached_input_tokens"])
        self.assertIsNone(entry["usage"]["reasoning_tokens"])
        self.assertEqual(0, entry["pricing"]["api_cost_usd"])
        self.assertIsNone(entry["pricing"]["infrastructure_cost_usd"])
        self.assertNotIn("response_text", entry)

    def test_truncated_output_rejected_but_usage_retained(self):
        response = dict(GOOD, done_reason="length")
        result = self.agent(Transport(response)).run("Объясни блики")
        self.assertEqual("output_rejected", result["status"])
        entry = self.entries()[0]
        self.assertEqual(20, entry["usage"]["total_tokens"])
        self.assertEqual("rejected", entry["output_policy"]["status"])
        self.assertEqual("length", entry["done_reason"])

    def test_empty_or_incomplete_output_is_not_success(self):
        for response in (dict(GOOD, message={"role": "assistant", "content": "  "}), dict(GOOD, done=False)):
            with self.subTest(response=response):
                agent = self.agent(Transport(response))
                result = agent.run("Объясни свет")
                self.assertEqual("output_rejected", result["status"])
                self.assertIsNone(result["answer"])

    def test_missing_usage_stays_unknown_and_stops_batch(self):
        response = dict(GOOD)
        del response["eval_count"]
        transport = Transport(response)
        agent = self.agent(transport)
        results = agent.run_batch([("simple", "Запрос один"), ("medium", "Запрос два")])
        self.assertEqual(1, len(results))
        self.assertEqual("usage_unknown", results[0]["status"])
        self.assertEqual(1, len(transport.calls))
        self.assertIsNone(self.entries()[0]["usage"]["total_tokens"])

    def test_reported_cached_tokens_are_preserved(self):
        response = dict(GOOD, prompt_eval_cached_count=5)
        self.agent(Transport(response)).run("Объясни свет")
        entry = self.entries()[0]
        self.assertEqual(5, entry["usage"]["cached_input_tokens"])
        self.assertEqual(5, entry["raw_usage"]["prompt_eval_cached_count"])

    def test_transport_error_writes_receipt_and_does_not_retry(self):
        transport = Transport(error=TimeoutError("timed out"))
        result = self.agent(transport).run("Объясни свет")
        self.assertEqual("api_error", result["status"])
        self.assertEqual(1, len(transport.calls))
        entry = self.entries()[0]
        self.assertEqual("not_run", entry["output_policy"]["status"])
        self.assertIsNone(entry["usage"]["input_tokens"])

    def test_explicit_text_evidence_and_cumulative_cost(self):
        agent = self.agent(Transport())
        agent.run("Первый запрос", save_text=True)
        agent.run("Второй запрос", save_text=False)
        first, second = self.entries()
        self.assertEqual("Первый запрос", first["prompt"])
        self.assertEqual(GOOD["message"]["content"], first["response_text"])
        self.assertNotIn("prompt", second)
        self.assertEqual(0, second["pricing"]["cumulative_api_cost_usd"])


if __name__ == "__main__":
    unittest.main()

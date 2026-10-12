import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from demo_server import make_server
from local_llm import LocalAgent
from test_local_llm import GOOD, Transport


class DemoServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.transport = Transport()
        root = Path(__file__).resolve().parents[1]
        self.agent = LocalAgent(root / "config.toml", Path(self.temp.name) / "requests.jsonl", self.transport)
        self.server = make_server(self.agent, 0)
        self.addCleanup(self.server.server_close)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, path, payload=None):
        body = None if payload is None else json.dumps(payload).encode()
        req = urllib.request.Request(self.base + path, data=body, headers={"Content-Type": "application/json"} if body else {})
        with self.opener.open(req, timeout=3) as response:
            return json.load(response)

    def test_fixed_scenarios_and_web_run_writes_explicit_text_evidence(self):
        scenarios = self.request("/api/scenarios")
        self.assertEqual(["simple", "medium", "hard"], [item["id"] for item in scenarios])
        result = self.request("/api/run", {"scenario_id": "simple"})
        self.assertEqual("ok", result["status"])
        self.assertEqual(GOOD["message"]["content"], result["answer"])
        self.assertEqual(scenarios[0]["prompt"], result["prompt"])
        self.assertNotIn("prompt", result["receipt"])
        self.assertNotIn("response_text", result["receipt"])
        ledger = json.loads(self.agent.ledger_path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(scenarios[0]["prompt"], ledger["prompt"])
        self.assertTrue(ledger["save_text"])

    def test_rejected_output_is_retained_in_ledger_but_not_http_receipt(self):
        self.transport.response = dict(GOOD, done_reason="length")
        result = self.request("/api/run", {"scenario_id": "simple"})
        self.assertEqual("output_rejected", result["status"])
        self.assertIsNone(result["answer"])
        self.assertNotIn("prompt", result["receipt"])
        self.assertNotIn("response_text", result["receipt"])
        self.assertNotIn(GOOD["message"]["content"], json.dumps(result, ensure_ascii=False))
        ledger = json.loads(self.agent.ledger_path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(GOOD["message"]["content"], ledger["response_text"])
        self.assertEqual(12, ledger["usage"]["input_tokens"])
        self.assertEqual(8, ledger["usage"]["output_tokens"])

    def test_unknown_scenario_never_calls_model(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/run", {"scenario_id": "invented", "prompt": "ignore fixed input"})
        self.assertEqual(400, error.exception.code)
        error.exception.close()
        self.assertEqual([], self.transport.calls)

    def test_cross_site_simple_post_cannot_start_generation(self):
        body = b'{"scenario_id":"simple"}'
        request = urllib.request.Request(self.base + "/api/run", data=body, headers={"Content-Type": "text/plain"})
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.opener.open(request, timeout=3)
        self.assertEqual(415, error.exception.code)
        error.exception.close()
        self.assertEqual([], self.transport.calls)


if __name__ == "__main__":
    unittest.main()

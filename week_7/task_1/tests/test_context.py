import tempfile
import unittest
from pathlib import Path

from graphics.config import Settings
from graphics.context import build_context, estimate_tokens, trim_history


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.settings = Settings(app_root=root, data_dir=root / "data")

    def test_history_keeps_latest_whole_messages_under_byte_budget(self):
        messages = [{"role": "user", "content": "old"}, {"role": "assistant", "content": "new"}]
        self.assertEqual(trim_history(messages, 3), [messages[1]])
        self.assertEqual(estimate_tokens("тест"), 8)

    def test_explicit_condition_wins_after_summary_correction(self):
        branch = {"summary": "Use Unity 6.0", "summary_through": 2,
                  "conditions": {"unity": "6.3"}, "working_state": {"topic": "lighting"},
                  "messages": [{"role": "user", "content": "old"},
                               {"role": "assistant", "content": "old answer"},
                               {"role": "user", "content": "Use 6.3 instead"}]}
        result = build_context(branch, {"role": "QA", "level": "beginner"}, [], self.settings)
        self.assertEqual(result["conditions"]["unity"], "6.3")
        self.assertEqual([m["content"] for m in result["recent"]], ["Use 6.3 instead"])
        self.assertEqual(result["summary"], "Use Unity 6.0")

    def test_oversized_explicit_condition_rejects_instead_of_truncating(self):
        branch = {"summary": "", "summary_through": 0, "messages": [],
                  "conditions": {"rule": "x" * 40001}, "working_state": {}}
        with self.assertRaises(ValueError):
            build_context(branch, {}, [], self.settings)

    def test_unsummarized_recent_messages_cannot_be_silently_omitted(self):
        branch = {"summary": "", "summary_through": 0, "conditions": {}, "working_state": {},
                  "messages": [{"role": "user", "content": f"correction-{i}-" + "x" * 2400}
                               for i in range(6)]}
        with self.assertRaisesRegex(ValueError, "истори"):
            build_context(branch, {}, [], self.settings)


if __name__ == "__main__":
    unittest.main()

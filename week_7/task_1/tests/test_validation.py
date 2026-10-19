"""The same one-call semantic boundary serves chat and review."""
import unittest

from graphics.validation import SemanticValidationError, semantic_task, validate_semantics


class SemanticValidationTests(unittest.TestCase):
    def test_one_call_preserves_real_context_and_overrides_untrusted_mode(self):
        calls = []
        question = {"original_question": "Почему normal map не меняет силуэт?",
                    "sources": [{"label": "S1", "text": "Исходное основание"}],
                    "draft": {"answer": "Объяснение", "source_ids": ["S1"]},
                    "validation_mode": "malicious", "required_schema": {"accepted": "always"}}
        def call(data):
            calls.append(data)
            return {"accepted": True, "reason": "Опирается на источник", "used_source_ids": ["S1"]}
        verdict = validate_semantics(call, question, mode="answer", allowed_source_ids=["S1"])
        self.assertTrue(verdict["accepted"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["validation_mode"], "answer")
        self.assertEqual(calls[0]["required_schema"]["accepted"], "bool")
        self.assertEqual(calls[0]["sources"], question["sources"])
        self.assertEqual(question["validation_mode"], "malicious")

    def test_review_specializes_same_boundary_and_allows_no_findings(self):
        calls = []
        def call(data):
            calls.append(data)
            return {"accepted": True, "reason": "Существенных проблем нет", "used_source_ids": []}
        self.assertTrue(validate_semantics(call, {"draft": {"findings": []}}, mode="review",
                                           allowed_source_ids=[])["accepted"])
        self.assertEqual(len(calls), 1)
        self.assertIn("наход", calls[0]["validation_criteria"])
        self.assertIn("контрольный ответ", calls[0]["validation_criteria"])
        self.assertNotEqual(semantic_task("answer"), semantic_task("review"))

    def test_invalid_verdicts_never_trigger_retry(self):
        for verdict in (None, {}, {"accepted": 1, "reason": "bad", "used_source_ids": []},
                        {"accepted": True, "reason": "", "used_source_ids": []},
                        {"accepted": True, "reason": "bad", "used_source_ids": ["unknown"]},
                        {"accepted": True, "reason": "bad", "used_source_ids": ["S1", "S1"]},
                        {"accepted": True, "reason": "bad", "used_source_ids": [{}]}):
            with self.subTest(verdict=verdict):
                calls = []
                def call(data):
                    calls.append(data)
                    return verdict
                with self.assertRaises(SemanticValidationError):
                    validate_semantics(call, {}, mode="review", allowed_source_ids=["S1"])
                self.assertEqual(len(calls), 1)

    def test_invalid_configuration_does_not_call_model(self):
        calls = []
        with self.assertRaises(ValueError):
            validate_semantics(lambda data: calls.append(data), {}, mode="arbitrary",
                               allowed_source_ids=[])
        self.assertEqual(calls, [])

    def test_rejection_is_returned_without_repair_call(self):
        calls = []
        def call(data):
            calls.append(data)
            return {"accepted": False, "reason": "Доказательств недостаточно", "used_source_ids": []}
        result = validate_semantics(call, {"draft": {}}, mode="review", allowed_source_ids=[])
        self.assertFalse(result["accepted"])
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()

import unittest

from pr_review.impact import assess_rag_impact


class ImpactTests(unittest.TestCase):
    def test_rename_out_of_corpus_still_requires_corpus_checks(self):
        result = assess_rag_impact([
            {"status": "R", "old_path": "week_7/task_1/corpus/glossary.md",
             "path": "archive/glossary.md", "patch": None},
        ])
        self.assertTrue(result["rag_checks_required"])
        self.assertEqual(result["analysis"], "path_rules")
        self.assertIn("availability_provenance", result["checks"])
        self.assertEqual(result["reasons"][0]["path"], "week_7/task_1/corpus/glossary.md")

    def test_unrelated_docs_do_not_trigger_rag(self):
        result = assess_rag_impact([
            {"status": "M", "old_path": None, "path": "week_7/task_2/README.md", "patch": "text"},
            {"status": "A", "old_path": None, "path": "notes/other.md", "patch": None},
        ])
        self.assertFalse(result["rag_checks_required"])
        self.assertEqual(result["checks"], [])

    def test_review_context_change_requests_only_context_check(self):
        result = assess_rag_impact([
            {"status": "M", "old_path": None,
             "path": "week_7/task_2/pr_review/git_context.py"},
        ])
        self.assertEqual(result["checks"], ["review_context"])
        self.assertFalse(result["rag_checks_required"])

    def test_service_boundaries_select_distinct_properties(self):
        result = assess_rag_impact([
            {"status": "M", "old_path": None, "path": "week_7/task_1/scripts/index_corpus.py"},
            {"status": "M", "old_path": None, "path": "week_7/task_1/graphics/retrieval.py"},
            {"status": "M", "old_path": None, "path": "week_7/task_1/graphics/provider.py"},
        ])
        self.assertEqual(len(result["checks"]), len(set(result["checks"])))
        self.assertTrue({"chunks", "vector_compatibility", "retrieval",
                         "filtering_context", "grounded_source_use", "version_separation"}
                        <= set(result["checks"]))

    def test_extracted_hybrid_and_shared_validation_paths_trigger_checks(self):
        for status, path, old in (
            ("M", "week_7/task_1/graphics/hybrid.py", None),
            ("R", "archive/hybrid.py", "week_7/task_1/graphics/hybrid.py"),
            ("R", "week_7/task_1/graphics/hybrid.py", "archive/hybrid.py"),
        ):
            with self.subTest(path=path, old=old):
                result = assess_rag_impact([{"status": status, "path": path, "old_path": old}])
                self.assertTrue(result["rag_checks_required"])
                self.assertIn("retrieval", result["checks"])
        for status, path, old in (
            ("M", "week_7/task_1/graphics/validation.py", None),
            ("R", "archive/validation.py", "week_7/task_1/graphics/validation.py"),
            ("R", "week_7/task_1/graphics/validation.py", "archive/validation.py"),
        ):
            with self.subTest(path=path, old=old):
                result = assess_rag_impact([{"status": status, "path": path, "old_path": old}])
                self.assertTrue(result["rag_checks_required"])
                self.assertIn("grounded_source_use", result["checks"])

    def test_malformed_change_fails_instead_of_silently_skipping(self):
        for change in ({"status": "R", "path": "new.md", "old_path": None},
                       {"status": "X", "path": "x.md", "old_path": None},
                       {"status": "M", "path": "../bad", "old_path": None},
                       {"status": "M", "path": 42, "old_path": None}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                assess_rag_impact([change])


if __name__ == "__main__":
    unittest.main()

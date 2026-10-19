import copy
import hashlib
from pathlib import Path
import tempfile
import unittest

from pr_review.findings import snapshot_digest, validate_findings


BASE = "a" * 40
HEAD = "b" * 40
PATH = "week_7/task_1/graphics/retrieval.py"
TEXT = "first line\nunsafe retrieval\n"


def fixture():
    data = TEXT.encode("utf-8")
    oid = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
    source_id = "git:" + hashlib.sha256((HEAD + "\0" + PATH + "\0" + oid).encode()).hexdigest()
    source = {"source_id": source_id, "revision": HEAD, "path": PATH, "blob_oid": oid,
              "start_line": 1, "end_line": 2, "text": TEXT,
              "content_sha256": hashlib.sha256(data).hexdigest(), "side": "head"}
    snapshot = {"schema_version": 1, "base_revision": BASE, "head_revision": HEAD,
                "merge_base": BASE, "changed_files": [], "sources": [source],
                "coverage": {"complete": True, "omissions": []}, "limits": {}}
    finding = {"id": "F1", "severity": "high", "kind": "defect",
               "title": "Retrieval can miss a match", "problem": "Lookup omits a hit",
               "conditions": "When the term is present", "consequence": "Answer lacks source",
               "recommendation": "Include the matching chunk",
               "location": {"revision": HEAD, "path": PATH, "start_line": 2, "end_line": 2},
               "evidence": [{"source_id": source_id, "quote": "unsafe retrieval"}]}
    payload = {"schema_version": 1, "base_revision": BASE, "head_revision": HEAD,
               "findings": [finding]}
    return snapshot, payload


class FindingsTests(unittest.TestCase):
    def test_valid_citation_is_mechanically_valid_but_not_accepted(self):
        snapshot, payload = fixture()
        result = validate_findings(payload, snapshot)
        self.assertTrue(result["valid"], result["errors"])
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["semantic_validation"], "not_run")
        self.assertFalse(result["review_accepted"])
        self.assertEqual(result["validation_scope"], "supplied_snapshot")

    def test_empty_findings_remain_unaccepted(self):
        snapshot, payload = fixture()
        payload["findings"] = []
        result = validate_findings(payload, snapshot)
        self.assertTrue(result["valid"], result["errors"])
        self.assertFalse(result["review_accepted"])

    def test_fabricated_location_and_evidence_are_rejected(self):
        changes = [
            ("revision", lambda f: f["location"].__setitem__("revision", "c" * 40)),
            ("path", lambda f: f["location"].__setitem__("path", "other.py")),
            ("line", lambda f: f["location"].__setitem__("start_line", 3)),
            ("bool_line", lambda f: f["location"].__setitem__("end_line", True)),
            ("source", lambda f: f["evidence"][0].__setitem__("source_id", "git:" + "0" * 64)),
            ("quote", lambda f: f["evidence"][0].__setitem__("quote", "made up")),
        ]
        for label, mutate in changes:
            snapshot, payload = fixture()
            mutate(payload["findings"][0])
            with self.subTest(label=label):
                self.assertFalse(validate_findings(payload, snapshot)["valid"])

    def test_duplicate_ids_and_wrong_report_revision_or_hash_fail(self):
        snapshot, payload = fixture()
        payload["findings"].append(copy.deepcopy(payload["findings"][0]))
        self.assertFalse(validate_findings(payload, snapshot)["valid"])
        snapshot, payload = fixture()
        payload["head_revision"] = "c" * 40
        self.assertFalse(validate_findings(payload, snapshot)["valid"])
        snapshot, payload = fixture()
        payload["snapshot_sha256"] = "0" * 64
        self.assertFalse(validate_findings(payload, snapshot)["valid"])

    def test_tampered_snapshot_content_and_source_identity_fail(self):
        for field, value in (("text", "different\n"), ("content_sha256", "0" * 64),
                             ("blob_oid", "0" * 40), ("source_id", "git:" + "0" * 64)):
            snapshot, payload = fixture()
            snapshot["sources"][0][field] = value
            with self.subTest(field=field):
                self.assertFalse(validate_findings(payload, snapshot)["valid"])

    def test_invalid_field_values_fail(self):
        for field, value in (("id", "  "), ("severity", "urgent"), ("kind", "suggestion"),
                             ("title", ""), ("conditions", 2), ("evidence", [])):
            snapshot, payload = fixture()
            payload["findings"][0][field] = value
            with self.subTest(field=field):
                self.assertFalse(validate_findings(payload, snapshot)["valid"])

    def test_source_text_is_data_and_never_executed(self):
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / "executed.txt"
            command_text = f"__import__('pathlib').Path({str(marker)!r}).write_text('bad')\n"
            snapshot, payload = fixture()
            source = snapshot["sources"][0]
            raw = command_text.encode("utf-8")
            oid = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
            sid = "git:" + hashlib.sha256((HEAD + "\0" + PATH + "\0" + oid).encode()).hexdigest()
            source.update({"text": command_text, "blob_oid": oid, "source_id": sid,
                           "content_sha256": hashlib.sha256(raw).hexdigest(), "end_line": 1})
            payload["findings"][0]["location"].update({"start_line": 1, "end_line": 1})
            payload["findings"][0]["evidence"] = [{"source_id": sid, "quote": command_text.strip()}]
            self.assertTrue(validate_findings(payload, snapshot)["valid"])
            self.assertFalse(marker.exists())

    def test_unhashable_untrusted_fields_return_errors(self):
        mutations = [
            lambda s, p: s["sources"][0].__setitem__("source_id", []),
            lambda s, p: s["sources"][0].__setitem__("revision", []),
            lambda s, p: p["findings"][0].__setitem__("severity", []),
            lambda s, p: p["findings"][0].__setitem__("kind", {}),
        ]
        for mutate in mutations:
            snapshot, payload = fixture()
            mutate(snapshot, payload)
            with self.subTest(mutate=mutate):
                self.assertFalse(validate_findings(payload, snapshot)["valid"])

    def test_location_quote_must_be_in_cited_lines(self):
        snapshot, payload = fixture()
        payload["findings"][0]["location"]["start_line"] = 1
        payload["findings"][0]["location"]["end_line"] = 1
        self.assertFalse(validate_findings(payload, snapshot)["valid"])

    def test_preparation_digest_tampering_is_rejected(self):
        snapshot, payload = fixture()
        snapshot["snapshot_sha256"] = "0" * 64
        self.assertFalse(validate_findings(payload, snapshot)["valid"])

    def test_deep_snapshot_canonicalization_returns_error(self):
        snapshot, payload = fixture()
        nested = None
        for _ in range(100000):
            nested = [nested]
        snapshot["limits"] = {"nested": nested}
        with self.assertRaisesRegex(ValueError, "snapshot_too_deep"):
            snapshot_digest(snapshot)
        self.assertFalse(validate_findings(payload, snapshot)["valid"])


if __name__ == "__main__":
    unittest.main()

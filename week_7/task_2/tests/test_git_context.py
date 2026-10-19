"""Integration tests for the pinned, read-only Git preparation boundary."""

import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from pr_review import git_context
from pr_review.git_context import Limits, PreparationError, prepare_snapshot
from pr_review.findings import validate_findings


class Repository(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.env = os.environ.copy()
        self.env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                         "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
                         "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
                         "GIT_TERMINAL_PROMPT": "0"})
        self.git("init", "-q")
        self.git("config", "core.autocrlf", "false")
        self.write("guide.txt", "first\n")
        self.commit("initial")
        self.initial = self.git("rev-parse", "HEAD").strip()

    def git(self, *args, ok=True):
        proc = subprocess.run(["git", *args], cwd=self.repo, env=self.env,
                              capture_output=True, text=True)
        if ok and proc.returncode:
            self.fail(f"git {args} failed: {proc.stderr}")
        return proc.stdout if ok else proc

    def write(self, path, content):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-qm", message)

    def test_add_modify_delete_rename_and_dirty_checkout_are_pinned(self):
        self.write("old name ü.txt", "renamed\n")
        self.write("gone.txt", "gone\n")
        self.commit("more base")
        base = self.git("rev-parse", "HEAD").strip()
        self.git("mv", "old name ü.txt", "new name ü.txt")
        self.write("guide.txt", "first\nsecond\n")
        self.write("added space ü.txt", "added\n")
        (self.repo / "gone.txt").unlink()
        self.commit("head")
        head = self.git("rev-parse", "HEAD").strip()
        self.write("guide.txt", "DIRTY CHECKOUT\n")
        self.git("add", "guide.txt")
        before = self.git("diff", "--cached", "--binary")
        result = prepare_snapshot(self.repo, base, head)
        self.assertEqual(result["merge_base"], base)
        self.assertEqual(result["base_revision"], base)
        self.assertEqual(result["head_revision"], head)
        self.assertTrue(result["coverage"]["complete"])
        files = {entry["path"]: entry for entry in result["changed_files"]}
        self.assertEqual({p: d["status"] for p, d in files.items()}, {
            "guide.txt": "M", "added space ü.txt": "A", "gone.txt": "D",
            "new name ü.txt": "R"})
        self.assertEqual(files["new name ü.txt"]["old_path"], "old name ü.txt")
        self.assertIn("second", files["guide.txt"]["patch"])
        self.assertNotIn("DIRTY CHECKOUT", str(result))
        self.assertEqual(before, self.git("diff", "--cached", "--binary"))

    def test_diverged_base_uses_merge_base_and_related_both_sides(self):
        self.write("related.txt", "common\n")
        self.commit("common")
        common = self.git("rev-parse", "HEAD").strip()
        self.git("branch", "topic")
        self.write("guide.txt", "base branch\n")
        self.commit("base ahead")
        base = self.git("rev-parse", "HEAD").strip()
        self.git("checkout", "-q", "topic")
        self.write("guide.txt", "topic branch\n")
        self.write("related.txt", "topic related\n")
        self.commit("topic ahead")
        result = prepare_snapshot(self.repo, base, "HEAD", related_paths=("related.txt", "missing.txt"))
        self.assertEqual(result["merge_base"], common)
        self.assertEqual(result["base_revision"], base)
        self.assertEqual(result["changed_files"][0]["path"], "guide.txt")
        related = {(s["side"], s["path"]): s for s in result["sources"] if s["path"] == "related.txt"}
        self.assertEqual(related[("base", "related.txt")]["revision"], common)
        self.assertEqual(related[("base", "related.txt")]["text"], "common\n")
        self.assertEqual(related[("head", "related.txt")]["text"], "topic related\n")
        self.assertTrue(any(o["reason"] == "missing" and o["path"] == "missing.txt"
                            for o in result["coverage"]["omissions"]))

    def test_bad_refs_paths_and_limits_fail_closed(self):
        for ref in ("--help", "no-such-ref", "HEAD^{tree}", "HEAD\x00oops"):
            with self.subTest(ref=ref), self.assertRaises(PreparationError):
                prepare_snapshot(self.repo, ref, "HEAD")
        for path in ("../secret", "/absolute", ".git/config", "C:/absolute"):
            with self.subTest(path=path), self.assertRaises(PreparationError):
                prepare_snapshot(self.repo, self.initial, "HEAD", related_paths=(path,))
        with self.assertRaises(PreparationError):
            prepare_snapshot(self.repo / ".git", self.initial, "HEAD")
        with self.assertRaises(PreparationError):
            prepare_snapshot(self.repo / "subdir", self.initial, "HEAD")
        with self.assertRaises(PreparationError):
            Limits(max_files=0)

    def test_explicit_repo_root_works_with_git_dubious_ownership(self):
        foreign_owner_env = git_context._safe_env()
        foreign_owner_env["GIT_TEST_ASSUME_DIFFERENT_OWNER"] = "1"
        control = subprocess.run(["git", "--no-pager", "rev-parse", "--show-toplevel"],
                                 cwd=self.repo, env=foreign_owner_env, capture_output=True)
        if control.returncode == 0:
            self.skipTest("Git lacks GIT_TEST_ASSUME_DIFFERENT_OWNER behavior")
        self.assertIn(b"dubious ownership", control.stderr)
        with patch.object(git_context, "_safe_env", return_value=foreign_owner_env):
            result = prepare_snapshot(self.repo, self.initial, "HEAD")
        self.assertEqual(result["base_revision"], self.initial)
        self.assertEqual(result["head_revision"], self.initial)

    def test_absent_merge_base_is_error(self):
        self.git("checkout", "--orphan", "orphan")
        self.write("orphan.txt", "orphan\n")
        self.commit("orphan")
        with self.assertRaises(PreparationError):
            prepare_snapshot(self.repo, self.initial, "HEAD")

    def test_sensitive_binary_symlink_and_size_omissions(self):
        self.write(".env.example", "PASSWORD=secret\n")
        self.write("notes/private.txt", "private\n")
        (self.repo / "binary.bin").write_bytes(b"\x00binary\n")
        self.write("large.txt", "x" * 80)
        self.commit("mixed")
        result = prepare_snapshot(self.repo, self.initial, "HEAD",
                                  limits=Limits(max_blob_bytes=32, max_patch_bytes=10000))
        self.assertFalse(result["coverage"]["complete"])
        self.assertIn(".env.example", {x["path"] for x in result["changed_files"]})
        self.assertNotIn("PASSWORD=secret", str(result))
        self.assertNotIn("private\n", str(result))
        self.assertNotIn("\x00binary", str(result))
        reasons = {(o["path"], o["reason"]) for o in result["coverage"]["omissions"]}
        self.assertIn((".env.example", "sensitive_path"), reasons)
        self.assertIn(("binary.bin", "binary"), reasons)
        self.assertIn(("large.txt", "oversized_blob"), reasons)
        if hasattr(os, "symlink"):
            try:
                os.symlink("guide.txt", self.repo / "link.txt")
            except OSError:
                pass
            else:
                self.commit("symlink")
                result = prepare_snapshot(self.repo, "HEAD^", "HEAD")
                self.assertTrue(any(o["path"] == "link.txt" and o["reason"] == "symlink"
                                    for o in result["coverage"]["omissions"]))
                self.assertIsNone(result["changed_files"][0]["patch"])

    def test_gitlink_is_not_extracted_or_patched(self):
        self.git("update-index", "--add", "--cacheinfo",
                 f"160000,{self.initial},nested")
        self.git("commit", "-qm", "gitlink")
        result = prepare_snapshot(self.repo, self.initial, "HEAD")
        self.assertEqual(result["changed_files"][0]["path"], "nested")
        self.assertIsNone(result["changed_files"][0]["patch"])
        self.assertTrue(any(o["path"] == "nested" and o["reason"] == "submodule"
                            for o in result["coverage"]["omissions"]))

    def test_malicious_diff_and_reviewed_code_are_not_executed(self):
        marker = self.root / "executed.txt"
        if os.name == "nt":
            command = self.root / "marker.cmd"
            command.write_text(f"@echo off\r\necho executed > \"{marker}\"\r\n", encoding="utf-8")
        else:
            command = self.root / "marker.sh"
            command.write_text(f"#!/bin/sh\nprintf executed > '{marker}'\n", encoding="utf-8")
            command.chmod(0o755)
        self.write(".gitattributes", "*.txt diff=evil\n")
        self.git("config", "diff.external", str(command))
        self.git("config", "diff.evil.textconv", str(command))
        self.write("guide.txt", "changed\n")
        self.write("reviewed.py", f"open({str(marker)!r}, 'w').write('executed')\n")
        self.commit("malicious configuration and code")
        result = prepare_snapshot(self.repo, self.initial, "HEAD")
        self.assertIn("changed", str(result))
        self.assertFalse(marker.exists(), "Git hook/filter or reviewed code executed")

    def test_secret_named_key_and_oversized_blob_have_no_patch(self):
        self.write("config/api_key.txt", "TOP_SECRET_VALUE\n")
        self.write("large.txt", "x" * 100)
        self.commit("key and large blob")
        result = prepare_snapshot(self.repo, self.initial, "HEAD",
                                  limits=Limits(max_blob_bytes=32))
        files = {item["path"]: item for item in result["changed_files"]}
        self.assertIsNone(files["config/api_key.txt"]["patch"])
        self.assertIsNone(files["large.txt"]["patch"])
        self.assertNotIn("TOP_SECRET_VALUE", str(result))
        self.assertTrue(any(o["path"] == "large.txt" and o["reason"] == "oversized_blob"
                            for o in result["coverage"]["omissions"]))

    def test_glob_shaped_literal_path_cannot_pull_secret_into_its_patch(self):
        self.write(".env", "OLD_SECRET\n")
        self.write(".[e]nv", "ordinary old\n")
        self.commit("both base paths")
        base = self.git("rev-parse", "HEAD").strip()
        self.write(".env", "TOP_SECRET_CHANGED\n")
        self.write(".[e]nv", "ordinary new\n")
        self.commit("both changed")
        result = prepare_snapshot(self.repo, base, "HEAD", related_paths=(".[e]nv",))
        files = {entry["path"]: entry for entry in result["changed_files"]}
        self.assertIsNone(files[".env"]["patch"])
        literal_patch = files[".[e]nv"]["patch"]
        self.assertIsNotNone(literal_patch)
        self.assertIn("ordinary new", literal_patch)
        self.assertNotIn("b/.env", literal_patch)
        self.assertNotIn("TOP_SECRET_CHANGED", str(result))

    def test_rename_from_sensitive_path_blocks_both_sources_and_related_alias(self):
        self.write(".env", "RENAME_SECRET_SENTINEL=1\n")
        self.commit("secret base")
        base = self.git("rev-parse", "HEAD").strip()
        self.git("mv", ".env", "ordinary.txt")
        self.commit("rename out of secret name")
        result = prepare_snapshot(self.repo, base, "HEAD", related_paths=("ordinary.txt",))
        change = result["changed_files"][0]
        self.assertEqual((change["status"], change["old_path"], change["path"]),
                         ("R", ".env", "ordinary.txt"))
        self.assertIsNone(change["patch"])
        self.assertNotIn("RENAME_SECRET_SENTINEL", str(result))
        self.assertFalse(any(s["path"] in (".env", "ordinary.txt") for s in result["sources"]))
        self.assertTrue(any(o["path"] == "ordinary.txt" and o["side"] == "head"
                            and o["reason"] == "sensitive_origin"
                            for o in result["coverage"]["omissions"]))

    def test_rename_into_sensitive_path_blocks_benign_base_and_related_alias(self):
        self.write("ordinary.txt", "MOVED_TO_SECRET_SENTINEL=1\n")
        self.commit("ordinary base")
        base = self.git("rev-parse", "HEAD").strip()
        self.git("mv", "ordinary.txt", ".env")
        self.commit("rename into secret name")
        result = prepare_snapshot(self.repo, base, "HEAD", related_paths=("ordinary.txt", ".env"))
        change = result["changed_files"][0]
        self.assertEqual((change["status"], change["old_path"], change["path"]),
                         ("R", "ordinary.txt", ".env"))
        self.assertIsNone(change["patch"])
        self.assertNotIn("MOVED_TO_SECRET_SENTINEL", str(result))
        self.assertFalse(any(s["path"] in ("ordinary.txt", ".env") for s in result["sources"]))
        self.assertTrue(any(o["path"] == "ordinary.txt" and o["side"] == "base"
                            and o["reason"] == "sensitive_origin"
                            for o in result["coverage"]["omissions"]))

    def test_newline_filename_is_parsed_if_filesystem_supports_it(self):
        path = "two\nlines.txt"
        try:
            self.write(path, "newline filename\n")
        except OSError:
            self.skipTest("filesystem does not accept newline filenames")
        self.commit("newline path")
        result = prepare_snapshot(self.repo, self.initial, "HEAD")
        self.assertEqual(result["changed_files"][0]["path"], path)
        self.assertEqual(result["sources"][0]["text"], "newline filename\n")

    def test_max_files_and_global_budget(self):
        self.write("a.txt", "aaa\n")
        self.write("b.txt", "bbb\n")
        self.commit("two")
        with self.assertRaises(PreparationError):
            prepare_snapshot(self.repo, self.initial, "HEAD", limits=Limits(max_files=1))
        result = prepare_snapshot(self.repo, self.initial, "HEAD",
                                  limits=Limits(max_total_source_bytes=3))
        self.assertFalse(result["coverage"]["complete"])
        self.assertTrue(any(o["reason"] == "source_budget" for o in result["coverage"]["omissions"]))
        result = prepare_snapshot(self.repo, self.initial, "HEAD",
                                  limits=Limits(max_total_patch_bytes=1))
        self.assertTrue(any(o["reason"] == "patch_budget" for o in result["coverage"]["omissions"]))
        result = prepare_snapshot(self.repo, self.initial, "HEAD",
                                  limits=Limits(max_patch_bytes=1))
        self.assertTrue(any(o["reason"] == "oversized_patch" for o in result["coverage"]["omissions"]))

    def test_invalid_utf8_is_not_decoded_as_source(self):
        (self.repo / "bad.txt").write_bytes(b"\xff\xfe\n")
        self.commit("invalid utf8")
        result = prepare_snapshot(self.repo, self.initial, "HEAD")
        self.assertFalse(any(s["path"] == "bad.txt" for s in result["sources"]))
        self.assertTrue(any(o["path"] == "bad.txt" and o["reason"] == "invalid_utf8"
                            for o in result["coverage"]["omissions"]))

    def test_source_ranges_and_limits_use_only_git_lf_boundaries(self):
        cases = [b"", b"\n", b"\n\n", b"one", b"one\n", b"one\r\nsecond\r\n",
                 "first\u2028second\u2029third\n".encode(), b"first\vsecond\fthird\rfourth\n"]
        for index, raw in enumerate(cases):
            (self.repo / f"lines{index}.txt").write_bytes(raw)
        self.commit("LF boundary cases")
        result = prepare_snapshot(self.repo, self.initial, "HEAD", limits=Limits(max_lines=2))
        sources = {source["path"]: source for source in result["sources"]}
        for index, raw in enumerate(cases):
            with self.subTest(raw=raw):
                self.assertIn(f"lines{index}.txt", sources,
                              "LF-bounded source was incorrectly omitted")
                source = sources[f"lines{index}.txt"]
                count = raw.count(b"\n") + int(bool(raw) and not raw.endswith(b"\n"))
                self.assertEqual(source["end_line"], count)
                self.assertEqual(source["start_line"], 1 if count else 0)
                self.assertEqual(source["text"].encode("utf-8"), raw)
                self.assertEqual(source["content_sha256"], hashlib.sha256(raw).hexdigest())
                oid = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
                self.assertEqual(source["blob_oid"], oid)

    def test_real_git_lf_source_rejects_nonexistent_line_and_accepts_exact_quotes(self):
        for index, text in enumerate(("first\u2028second\n", "first\vsecond\n",
                                      "first\r\nsecond\r\n")):
            (self.repo / f"quote{index}.txt").write_bytes(text.encode())
        self.commit("exact quote cases")
        snapshot = prepare_snapshot(self.repo, self.initial, "HEAD")
        for source in snapshot["sources"]:
            if not source["path"].startswith("quote"):
                continue
            finding = {"id": "F1", "severity": "low", "kind": "hypothesis",
                       "title": "Check source", "problem": "Content needs review",
                       "conditions": "When reading", "consequence": "May confuse",
                       "recommendation": "Verify content",
                       "location": {"revision": source["revision"], "path": source["path"],
                                    "start_line": 2, "end_line": 2},
                       "evidence": [{"source_id": source["source_id"], "quote": "second"}]}
            report = {"schema_version": 1, "base_revision": snapshot["base_revision"],
                      "head_revision": snapshot["head_revision"], "findings": [finding]}
            with self.subTest(path=source["path"]):
                if source["path"] != "quote2.txt":
                    self.assertFalse(validate_findings(report, snapshot)["valid"])
                    finding["location"].update(start_line=1, end_line=1)
                else:
                    finding["evidence"][0]["quote"] = "second\r\n"
                result = validate_findings(report, snapshot)
                self.assertTrue(result["valid"], result["errors"])

    def test_same_revision_related_source_has_one_stable_identity(self):
        snapshot = prepare_snapshot(self.repo, "HEAD", "HEAD", related_paths=("guide.txt",),
                                    limits=Limits(max_total_source_bytes=6))
        self.assertEqual(len(snapshot["sources"]), 1)
        source = snapshot["sources"][0]
        self.assertIn(source["side"], ("base", "head"))
        self.assertEqual(source["revision"], self.initial)
        expected = "git:" + hashlib.sha256((self.initial + "\0guide.txt\0" +
                                            source["blob_oid"]).encode()).hexdigest()
        self.assertEqual(source["source_id"], expected)
        self.assertTrue(snapshot["coverage"]["complete"])
        report = {"schema_version": 1, "base_revision": self.initial,
                  "head_revision": self.initial, "findings": []}
        result = validate_findings(report, snapshot)
        self.assertTrue(result["valid"], result["errors"])
        self.assertFalse(result["review_accepted"])

    def test_distinct_revisions_keep_shared_blob_sources_separate(self):
        self.write("other.txt", "unrelated change\n")
        self.commit("new revision with unchanged guide")
        snapshot = prepare_snapshot(self.repo, self.initial, "HEAD", related_paths=("guide.txt",))
        sources = [source for source in snapshot["sources"] if source["path"] == "guide.txt"]
        self.assertEqual(len(sources), 2)
        self.assertEqual(len({source["blob_oid"] for source in sources}), 1)
        self.assertEqual(len({source["source_id"] for source in sources}), 2)
        self.assertEqual({source["side"] for source in sources}, {"base", "head"})
        self.assertEqual({source["revision"] for source in sources},
                         {snapshot["merge_base"], snapshot["head_revision"]})

    def test_missing_promisor_blob_never_invokes_local_remote_helper(self):
        oid = self.git("rev-parse", "HEAD:guide.txt").strip()
        object_path = self.repo / ".git" / "objects" / oid[:2] / oid[2:]
        self.assertTrue(object_path.exists(), "fixture needs a loose missing blob")
        object_path.chmod(0o600)
        object_path.unlink()
        self.git("config", "remote.origin.url", "audit::local-only")
        self.git("config", "remote.origin.promisor", "true")
        self.git("config", "remote.origin.partialclonefilter", "blob:none")
        self.git("config", "protocol.audit.allow", "always")
        helpers = self.root / "helpers"
        helpers.mkdir()
        marker = self.root / "remote-called.txt"
        helper = helpers / "git-remote-audit"
        helper.write_text("#!/bin/sh\nprintf called >> '" + marker.as_posix() + "'\nexit 1\n",
                          encoding="utf-8", newline="\n")
        helper.chmod(0o755)
        env = {key: value for key, value in self.env.items()
               if not key.upper().startswith("GIT_")}
        env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_TERMINAL_PROMPT": "0", "GIT_NO_LAZY_FETCH": "0",
                    "GIT_ALLOW_PROTOCOL": "audit",
                    "PATH": str(helpers) + os.pathsep + os.environ["PATH"]})
        control = subprocess.run(["git", "-c", f"safe.directory={self.repo}",
                                  "cat-file", "blob", oid], cwd=self.repo, env=env,
                                 capture_output=True, timeout=10)
        self.assertNotEqual(control.returncode, 0)
        self.assertTrue(marker.exists(), "control must actually invoke the local helper")
        marker.unlink()
        objects = self.repo / ".git" / "objects"
        before = {path.relative_to(objects): path.read_bytes()
                  for path in objects.rglob("*") if path.is_file()}
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(PreparationError, "^blob_size_failed$"):
                prepare_snapshot(self.repo, "HEAD", "HEAD", related_paths=("guide.txt",))
        after = {path.relative_to(objects): path.read_bytes()
                 for path in objects.rglob("*") if path.is_file()}
        self.assertFalse(marker.exists(), "product Git invoked a promisor remote helper")
        self.assertFalse(object_path.exists())
        self.assertEqual(before, after, "product Git wrote object-store files")


if __name__ == "__main__":
    unittest.main()

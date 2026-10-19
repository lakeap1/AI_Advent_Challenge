import tempfile
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from learning_files.repository import FileRepository, _file_lock


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = FileRepository(self.root)
        self.repo.seed(1, {"sources/material.md": "Material source\n", "sources/shader.md": "Shader source\n"})

    def proposal(self, changes=None):
        sources = [self.repo.workspace(1).read("sources/material.md"), self.repo.workspace(1).read("sources/shader.md")]
        return self.repo.build(1, changes or [{"path": "outputs/guide.md", "content": "New guide\n"}], sources)

    def accepted(self, changes=None):
        run = self.repo.start(1, "branch-7", "request-1", "Explain material")["run"]
        return self.repo.accept(1, run["id"], self.proposal(changes), "Ready")

    def test_seed_preview_persistence_and_owner(self):
        self.assertEqual(self.repo.seed(1, {"sources/material.md": "new"}), [])
        self.assertEqual(self.repo.workspace(1).read("sources/material.md")["content"], "Material source\n")
        self.assertEqual(self.repo.seed(1, {"notes/exercise.md": "Known contradiction\n"}), ["notes/exercise.md"])
        self.assertEqual(self.repo.workspace(1).read("notes/exercise.md")["content"], "Known contradiction\n")
        with self.assertRaises(ValueError):
            self.repo.start(1, 7, "numeric-branch", "Goal")
        first = self.repo.start(1, "branch-7", "request-1", "Explain material")
        self.assertTrue(first["fresh"])
        self.assertFalse(FileRepository(self.root).start(1, "branch-7", "request-1", "Explain material")["fresh"])
        for branch, goal in (("branch-8", "Explain material"), ("branch-7", "different")):
            with self.assertRaises(ValueError):
                self.repo.start(1, branch, "request-1", goal)
        proposal = self.proposal()
        self.assertIn("+New guide", proposal["changes"][0]["diff"])
        self.assertEqual(proposal["changes"][0]["before"], None)
        self.assertFalse((self.root / "users" / "1" / "outputs" / "guide.md").exists())
        run = self.repo.accept(1, first["run"]["id"], proposal, "Ready")
        self.assertEqual(FileRepository(self.root).get(1, run["id"])["status"], "validated")
        self.assertEqual(self.repo.find_request(1, "request-1")["id"], run["id"])
        with self.assertRaises(PermissionError):
            self.repo.get(2, run["id"])
        with self.assertRaises(PermissionError):
            self.repo.apply(2, run["id"])

    def test_latest_is_durable_and_scoped_to_owner_and_branch(self):
        first = self.repo.start(1, "branch-7", "request-1", "First")['run']
        other_branch = self.repo.start(1, "branch-8", "request-2", "Other branch")['run']
        last = self.repo.start(1, "branch-7", "request-3", "Last")['run']
        restarted = FileRepository(self.root)
        self.assertEqual(restarted.latest(1, "branch-7")["id"], last["id"])
        self.assertEqual(restarted.latest(1, "branch-8")["id"], other_branch["id"])
        self.assertIsNone(restarted.latest(2, "branch-7"))
        self.assertIsNone(restarted.latest(1, "missing"))
        self.assertEqual(restarted.find_request(1, "request-1")["id"], first["id"])

    def test_unified_diff_marks_missing_final_lf_without_changing_content(self):
        target = self.repo.workspace(1)._checked_path("notes/noeol.md", writable=True, create=True)
        target.write_bytes(b"foo")
        changed = self.proposal([{"path": "notes/noeol.md", "content": "bar"}])["changes"][0]
        self.assertEqual(changed["before"], "foo")
        self.assertEqual(changed["content"], "bar")
        self.assertEqual(changed["diff"],
                         "--- notes/noeol.md\n+++ notes/noeol.md\n@@ -1 +1 @@\n"
                         "-foo\n\\ No newline at end of file\n"
                         "+bar\n\\ No newline at end of file\n")
        created = self.proposal([{"path": "outputs/new.md", "content": "bar"}])["changes"][0]
        self.assertIsNone(created["before"])
        self.assertEqual(created["diff"],
                         "--- /dev/null\n+++ outputs/new.md\n@@ -0,0 +1 @@\n"
                         "+bar\n\\ No newline at end of file\n")
        self.assertFalse((self.root / "users" / "1" / "outputs" / "new.md").exists())

    def test_failed_seed_does_not_leave_partial_source(self):
        with patch.object(os, "fsync", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                self.repo.seed(1, {"sources/failure.md": "Trusted example"})
        target = self.root / "users" / "1" / "sources" / "failure.md"
        self.assertFalse(target.exists())
        self.assertEqual(self.repo.seed(1, {"sources/failure.md": "Trusted example"}), ["sources/failure.md"])

    def test_conflict_is_before_all_writes(self):
        run = self.accepted([{"path": "notes/a.md", "content": "A"}, {"path": "outputs/b.md", "content": "B"}])
        self.repo.workspace(1).read("sources/material.md")
        (self.root / "users" / "1" / "sources" / "material.md").write_text("changed", encoding="utf-8")
        result = self.repo.apply(1, run["id"])
        self.assertEqual(result["status"], "conflict")
        self.assertEqual(result["saved"], [])
        self.assertFalse((self.root / "users" / "1" / "notes" / "a.md").exists())

    def test_target_conflict_prevents_first_write(self):
        run = self.accepted([{"path": "outputs/a.md", "content": "A"}, {"path": "outputs/b.md", "content": "B"}])
        target = self.repo.workspace(1)._checked_path("outputs/b.md", writable=True, create=True)
        target.write_bytes(b"user owns this")
        self.assertEqual(self.repo.apply(1, run["id"])["status"], "conflict")
        self.assertFalse((target.parent / "a.md").exists())
        self.assertEqual(target.read_bytes(), b"user owns this")

    def test_apply_rechecks_document_limit(self):
        run = self.accepted()
        sources = self.root / "users" / "1" / "sources"
        for number in range(22):
            (sources / f"extra{number}.md").write_bytes(b"data")
        self.assertEqual(len(self.repo.workspace(1).inventory()), 24)
        self.assertEqual(self.repo.apply(1, run["id"])["status"], "conflict")
        self.assertFalse((self.root / "users" / "1" / "outputs" / "guide.md").exists())

    def test_apply_restart_and_repeat_after_edit(self):
        run = self.accepted()
        result = self.repo.apply(1, run["id"])
        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["saved"][0]["sha256"], self.repo.workspace(1).read("outputs/guide.md")["sha256"])
        restarted = FileRepository(self.root)
        self.assertEqual(restarted.apply(1, run["id"])["status"], "applied")
        target = self.root / "users" / "1" / "outputs" / "guide.md"
        target.write_text("later user edit", encoding="utf-8")
        self.assertEqual(restarted.apply(1, run["id"])["status"], "conflict")
        self.assertEqual(target.read_text(encoding="utf-8"), "later user edit")

    def test_partial_write_records_exact_completed_list(self):
        run = self.accepted([{"path": "notes/a.md", "content": "A"}, {"path": "outputs/b.md", "content": "B"}])
        original = self.repo._write_change
        calls = 0

        def interrupted(workspace, change):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected")
            return original(workspace, change)

        with patch.object(self.repo, "_write_change", side_effect=interrupted):
            result = self.repo.apply(1, run["id"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual([x["path"] for x in result["saved"]], ["notes/a.md"])
        self.assertFalse((self.root / "users" / "1" / "outputs" / "b.md").exists())
        self.assertEqual(FileRepository(self.root).apply(1, run["id"])["status"], "partial")

    def test_rejected_and_forged_proposals_not_applicable(self):
        run = self.repo.start(1, "branch-7", "r", "Goal")["run"]
        self.repo.fail(1, run["id"], "rejected", "Validator rejected")
        self.assertEqual(self.repo.apply(1, run["id"])["status"], "rejected")
        with self.assertRaises(ValueError):
            self.repo.accept(1, run["id"], self.proposal(), "bad")
        fresh = self.repo.start(1, "branch-7", "r2", "Goal")["run"]
        forged = self.proposal()
        forged["changes"][0]["content"] = "tampered"
        with self.assertRaises(ValueError):
            self.repo.accept(1, fresh["id"], forged, "bad")

    def test_existing_note_can_be_source_and_target(self):
        note = self.repo.workspace(1)._checked_path("notes/normal.md", writable=True, create=True)
        note.write_bytes(b"incorrect note\n")
        sources = [self.repo.workspace(1).read("sources/material.md"), self.repo.workspace(1).read("notes/normal.md")]
        proposal = self.repo.build(1, [{"path": "notes/normal.md", "content": "corrected note\n"}], sources)
        self.assertIn("-incorrect note", proposal["changes"][0]["diff"])
        run = self.repo.start(1, "branch-7", "edit", "Correct note")["run"]
        self.repo.accept(1, run["id"], proposal, "Corrected")
        self.assertEqual(self.repo.apply(1, run["id"])["status"], "applied")
        self.assertEqual(note.read_bytes(), b"corrected note\n")

    def test_staged_proposal_requires_accepted_owned_turn_before_apply(self):
        run = self.repo.start(1, "branch-7", "staged", "Explain material")["run"]
        pending = self.repo.stage(1, run["id"], self.proposal(), "Ready", "turn-123")
        self.assertEqual(pending["status"], "pending_publish")
        self.assertEqual(pending["changes"], [])
        self.assertEqual(self.repo.apply(1, run["id"])["status"], "pending_publish")
        self.assertFalse((self.root / "users" / "1" / "outputs" / "guide.md").exists())

        class FakeStore:
            def __init__(self, status, question="Explain material"):
                self.status = status
                self.question = question

            def get_turn(self, user, turn_id):
                return {"id": turn_id, "branch_id": "branch-7", "question": self.question,
                        "status": self.status}

        self.assertEqual(self.repo.finalize(1, run["id"], FakeStore("semantic_check"))["status"], "pending_publish")
        with self.assertRaises(PermissionError):
            self.repo.finalize(1, run["id"], FakeStore("accepted", "Other goal"))
        self.assertEqual(FileRepository(self.root).finalize(1, run["id"], FakeStore("accepted"))["status"], "validated")
        self.assertEqual(self.repo.apply(1, run["id"])["status"], "applied")

    def test_crash_after_write_recovers_exact_partial_receipt(self):
        run = self.accepted([{"path": "notes/a.md", "content": "A"}, {"path": "outputs/b.md", "content": "B"}])
        original = self.repo._write_change

        def crash(workspace, change):
            original(workspace, change)
            raise KeyboardInterrupt("process stopped before journal")

        with patch.object(self.repo, "_write_change", side_effect=crash):
            with self.assertRaises(KeyboardInterrupt):
                self.repo.apply(1, run["id"])
        restarted = FileRepository(self.root)
        result = restarted.get(1, run["id"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual([item["path"] for item in result["saved"]], ["notes/a.md"])
        replay = restarted.start(1, "branch-7", "request-1", "Explain material")
        self.assertFalse(replay["fresh"])
        self.assertEqual(replay["run"]["saved"], result["saved"])
        self.assertEqual(restarted.apply(1, run["id"])["status"], "partial")
        self.assertEqual([item["path"] for item in restarted.workspace(1).inventory() if item["path"].startswith("notes/")], ["notes/a.md"])

    def test_orphan_staging_link_is_removed_before_crash_recovery(self):
        run = self.accepted()
        with self.repo._db() as db:
            db.execute("UPDATE runs SET status='partial' WHERE id=?", (run["id"],))
        staging = self.root / ".staging"
        staging.mkdir(exist_ok=True)
        orphan = staging / ".pending-orphan"
        orphan.write_bytes(b"New guide\n")
        target = self.repo.workspace(1)._checked_path("outputs/guide.md", writable=True, create=True)
        os.link(orphan, target)
        restarted = FileRepository(self.root)
        recovered = restarted.get(1, run["id"])
        self.assertEqual(recovered["status"], "partial")
        self.assertEqual([item["path"] for item in recovered["saved"]], ["outputs/guide.md"])
        self.assertFalse(orphan.exists())

    def test_partial_does_not_count_unchanged_existing_target_as_written(self):
        note = self.repo.workspace(1)._checked_path("notes/same.md", writable=True, create=True)
        note.write_bytes(b"B")
        run = self.accepted([{"path": "outputs/a.md", "content": "A"}, {"path": "notes/same.md", "content": "B"}])
        original = self.repo._write_change
        calls = 0

        def interrupted(workspace, change):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected")
            return original(workspace, change)

        with patch.object(self.repo, "_write_change", side_effect=interrupted):
            result = self.repo.apply(1, run["id"])
        self.assertEqual([item["path"] for item in result["saved"]], ["outputs/a.md"])

    def test_second_process_waits_for_repository_lock(self):
        script = "import sys; from learning_files.repository import FileRepository; print(FileRepository(sys.argv[1]).start(1, 'branch-7', 'child', 'Goal')['fresh'], flush=True)"
        with _file_lock(self.repo.lock_path):
            child = subprocess.Popen([sys.executable, "-B", "-c", script, str(self.root)],
                                     cwd=Path(__file__).resolve().parents[1],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                time.sleep(0.3)
                self.assertIsNone(child.poll(), "child bypassed the cross-process lock")
            except BaseException:
                child.kill()
                child.communicate()
                raise
        stdout, stderr = child.communicate(timeout=5)
        self.assertEqual(child.returncode, 0, stderr)
        self.assertEqual(stdout.strip(), "True")


if __name__ == "__main__":
    unittest.main()

import os
import tempfile
import unittest
from pathlib import Path

from learning_files.workspace import Workspace


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ws = Workspace(self.root, 1)
        (self.root / "users" / "1" / "sources").mkdir(parents=True)
        (self.root / "users" / "1" / "sources" / "shader.md").write_bytes(
            "Материал использует шейдер.\n".encode("utf-8")
        )

    def test_actual_inventory_search_read(self):
        item = self.ws.inventory()[0]
        self.assertEqual(item["path"], "sources/shader.md")
        self.assertEqual(item["bytes"], len("Материал использует шейдер.\n".encode()))
        self.assertEqual(self.ws.search("ШЕЙДЕР")[0]["path"], item["path"])
        self.assertEqual(self.ws.read(item["path"])["sha256"], item["sha256"])
        self.assertEqual(self.ws.search("несуществующее"), [])

    def test_search_matches_terms_of_long_goal_and_ranks_more_matches(self):
        folder = self.root / "users" / "1" / "sources"
        (folder / "texture.md").write_bytes("Текстура и материал используют шейдер.\n".encode("utf-8"))
        results = self.ws.search("Объясни материал, шейдер и текстуру на примере")
        self.assertEqual(results[0]["path"], "sources/texture.md")
        self.assertIn("sources/shader.md", [item["path"] for item in results])

    def test_rejects_paths_and_links(self):
        for value in ("../x.md", "sources/../shader.md", "C:/secret.md", "\\\\host\\x.md", "sources/a:b.md", "sources/CON.md", "sources/a.txt", "other/x.md", "sources\\shader.md"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.ws.read(value)
        outside = self.root / "outside.md"
        outside.write_text("secret", encoding="utf-8")
        link = self.root / "users" / "1" / "sources" / "link.md"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            pass
        else:
            with self.assertRaises(PermissionError):
                self.ws.read("sources/link.md")
        hardlink = self.root / "users" / "1" / "sources" / "hard.md"
        try:
            os.link(outside, hardlink)
        except OSError:
            pass
        else:
            with self.assertRaises(PermissionError):
                self.ws.read("sources/hard.md")

    def test_owner_and_size(self):
        for user in (0, -1, "1", True):
            with self.subTest(user=user), self.assertRaises(ValueError):
                Workspace(self.root, user)
        self.assertEqual(Workspace(self.root, 2).inventory(), [])
        (self.root / "users" / "1" / "sources" / "large.md").write_bytes(b"a" * 16385)
        with self.assertRaises(ValueError):
            self.ws.read("sources/large.md")

    def test_rejects_linked_user_directory_before_inventory(self):
        outside = self.root / "external"
        (outside / "sources").mkdir(parents=True)
        (outside / "sources" / "private.md").write_bytes(b"private")
        try:
            (self.root / "users" / "2").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("directory symlink unavailable")
        with self.assertRaises(PermissionError):
            Workspace(self.root, 2).inventory()


if __name__ == "__main__":
    unittest.main()

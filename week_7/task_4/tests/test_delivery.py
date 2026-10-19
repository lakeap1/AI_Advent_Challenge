"""Behavioral checks for the source-only deployment archive."""

import hashlib
import importlib.util
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "package_deployment.py"
SPEC = importlib.util.spec_from_file_location("package_deployment", MODULE_PATH)
package_deployment = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(package_deployment)


class DeliveryArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "task_4"
        self.root.mkdir()

    def put(self, relative, data):
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target

    def test_archive_contains_exact_allowed_bytes_and_matching_manifest(self):
        allowed = {
            "README.md": b"public guide\n",
            "requirements.txt": b"Flask==3.1.2\n",
            "server.py": b"print('start')\n",
            ".gitignore": b"data/\n",
            "learning_files/__init__.py": b"",
            "learning_files/static/files.js": b"const x = 1;\n",
            "learning_files/static/files.css": b".x {}\n",
            "examples/lesson.md": "Пример\n".encode("utf-8"),
            "tests/test_delivery.py": b"assert True\n",
            "scripts/package_deployment.py": b"def package(): pass\n",
        }
        for name, data in allowed.items():
            self.put(name, data)
        for name in (
            "data/secrets.sqlite3", "demo/secret.txt", "docs/internal.md",
            "notes/private.md", ".env", "private.key", "learning_files/private.json",
            "learning_files/__pycache__/module.py", "learning_files/DATA/private.py",
            "learning_files/static/token.txt",
            "examples/private.txt", "scripts/__pycache__/module.py",
        ):
            self.put(name, b"PRIVATE DO NOT EXPORT\n")

        output = self.root / "demo" / "bundle.tar.gz"
        metadata = package_deployment.package(self.root, output)

        with tarfile.open(output, "r:gz") as archive:
            members = archive.getmembers()
            contents = {member.name: archive.extractfile(member).read()
                        for member in members}
        self.assertEqual(contents, allowed)
        self.assertTrue(all(member.isfile() for member in members))
        expected_manifest = {name: hashlib.sha256(data).hexdigest()
                             for name, data in allowed.items()}
        self.assertEqual(metadata["manifest"], expected_manifest)
        self.assertEqual(metadata["paths"], sorted(allowed))
        self.assertEqual(metadata["files"], len(allowed))
        self.assertEqual(metadata["sha256"], hashlib.sha256(output.read_bytes()).hexdigest())
        self.assertNotIn(b"PRIVATE DO NOT EXPORT", output.read_bytes())

        next_output = self.root / "demo" / "bundle-next.tar.gz"
        next_metadata = package_deployment.package(self.root, next_output)
        self.assertEqual(next_metadata["paths"], sorted(allowed))
        with tarfile.open(next_output, "r:gz") as archive:
            self.assertEqual(sorted(archive.getnames()), sorted(allowed))

    def test_does_not_follow_file_or_directory_symlinks(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        private = outside / "secret.py"
        private.write_bytes(b"PRIVATE")
        public = self.put("server.py", b"app = None\n")
        self.put("README.md", b"guide\n")
        self.put("requirements.txt", b"Flask==3.1.2\n")
        try:
            (self.root / "scripts").symlink_to(outside, target_is_directory=True)
            (self.root / "learning_files").mkdir()
            (self.root / "learning_files" / "linked.py").symlink_to(private)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        output = self.root / "demo" / "bundle.tar.gz"
        metadata = package_deployment.package(self.root, output)
        self.assertEqual(metadata["paths"],
                         ["README.md", "requirements.txt", "server.py"])
        self.assertEqual(public.read_bytes(), b"app = None\n")
        with tarfile.open(output, "r:gz") as archive:
            self.assertEqual(archive.getnames(), metadata["paths"])

    def test_refuses_to_overwrite_source_or_existing_output(self):
        source = self.put("README.md", b"keep me")
        with self.assertRaises(ValueError):
            package_deployment.package(self.root, source)
        self.assertEqual(source.read_bytes(), b"keep me")

        output = self.root / "demo" / "bundle.tar.gz"
        output.parent.mkdir()
        output.write_bytes(b"existing")
        with self.assertRaises(FileExistsError):
            package_deployment.package(self.root, output)
        self.assertEqual(output.read_bytes(), b"existing")

    def test_each_source_is_read_once_for_archive_and_manifest(self):
        source = self.put("server.py", b"original bytes\n")
        self.put("README.md", b"guide\n")
        self.put("requirements.txt", b"Flask==3.1.2\n")
        original_read = Path.read_bytes
        reads = []

        def counting_read(path):
            if path == source:
                reads.append(path)
            return original_read(path)

        output = self.root / "demo" / "bundle.tar.gz"
        with mock.patch.object(Path, "read_bytes", counting_read):
            metadata = package_deployment.package(self.root, output)
        self.assertEqual(reads, [source])
        with tarfile.open(output, "r:gz") as archive:
            archived = archive.extractfile("server.py").read()
        self.assertEqual(archived, b"original bytes\n")
        self.assertEqual(metadata["manifest"]["server.py"],
                         hashlib.sha256(archived).hexdigest())

    def test_rejects_incomplete_application_tree(self):
        self.put("README.md", b"guide\n")
        output = self.root / "demo" / "bundle.tar.gz"
        with self.assertRaises(ValueError):
            package_deployment.package(self.root, output)
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()

"""The delivery archive contains only the source needed to run Day 35."""

import hashlib
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
import zipfile

from scripts.package_delivery import package


# Contract written independently of the packager's REQUIRED constant. A missing
# entry here would let a broken delivery pass unnoticed.
RUNTIME_FILES = {
    "task_1": (
        "README.md", "requirements.txt", "app.py", "server.py",
        "docs/usage.md", "docs/architecture.md", "docs/api.md",
        "corpus/manifest.json", "corpus/glossary.md", "corpus/support-faq.md",
        "templates/index.html", "static/app.js", "static/app.css",
        "scripts/package_deployment.py",
        *tuple(f"graphics/{name}.py" for name in (
            "__init__", "chunking", "cli", "config", "context", "hybrid",
            "mcp_client", "mcp_server", "policies", "provider", "retrieval",
            "service", "store", "support_integration", "validation", "web")),
    ),
    "task_3": (
        "README.md", "requirements.txt", "scripts/package_delivery.py",
        "scripts/live_check.py",
        *tuple(f"support/{name}.py" for name in (
            "__init__", "__main__", "domain", "mcp_client", "mcp_server",
            "ports", "service_bridge", "smoke", "tickets", "tools")),
    ),
    "task_4": (
        "README.md", "requirements.txt", "server.py",
        "scripts/package_deployment.py",
        *tuple(f"learning_files/{name}.py" for name in (
            "__init__", "agent", "repository", "web", "workspace")),
        "learning_files/static/files.js", "learning_files/static/files.css",
        *tuple(f"examples/{name}.md" for name in (
            "materials", "normal-maps", "normal-theory", "normal-unity",
            "shader-pipeline", "textures")),
    ),
    "task_5": (
        "README.md", "requirements.txt", "server.py",
        "learning_product/__init__.py", "learning_product/materials.py",
        "learning_product/web.py", "learning_product/static/materials.js",
        "learning_product/static/materials.css",
    ),
}


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        for task, files in {
            "task_1": ("README.md", "requirements.txt", "app.py", "server.py", ".env.example"),
            "task_3": ("requirements.txt",),
            "task_4": ("README.md", "requirements.txt", "server.py"),
            "task_5": ("README.md", "requirements.txt", "server.py"),
        }.items():
            for name in files:
                self.put(f"week_7/{task}/{name}", f"{task}/{name}\n")
        self.put("week_7/task_1/docs/usage.md", "Использование\n")
        self.put("week_7/task_1/docs/api.md", "API\n")
        self.put("week_7/task_1/docs/architecture.md", "Architecture\n")
        self.put("week_7/task_1/graphics/service.py", "service\n")
        self.put("week_7/task_1/graphics/__init__.py", "")
        self.put("week_7/task_1/graphics/web.py", "web\n")
        self.put("week_7/task_1/graphics/config.py", "config\n")
        self.put("week_7/task_1/graphics/store.py", "store\n")
        self.put("week_7/task_3/support/__init__.py", "")
        self.put("week_7/task_3/support/domain.py", "support\n")
        self.put("week_7/task_3/support/service_bridge.py", "bridge\n")
        self.put("week_7/task_3/tests/test_tickets.py", "test\n")
        self.put("week_7/task_4/learning_files/__init__.py", "")
        self.put("week_7/task_4/learning_files/web.py", "web\n")
        self.put("week_7/task_4/learning_files/agent.py", "agent\n")
        self.put("week_7/task_4/learning_files/repository.py", "repository\n")
        self.put("week_7/task_4/learning_files/workspace.py", "workspace\n")
        self.put("week_7/task_4/learning_files/static/files.js", "files\n")
        self.put("week_7/task_4/learning_files/static/files.css", "css\n")
        self.put("week_7/task_5/learning_product/__init__.py", "")
        self.put("week_7/task_5/learning_product/web.py", "web\n")
        self.put("week_7/task_5/learning_product/materials.py", "materials\n")
        self.put("week_7/task_5/learning_product/static/materials.js", "materials\n")
        self.put("week_7/task_5/learning_product/static/materials.css", "css\n")
        self.put("week_7/task_5/scripts/package_delivery.py", "package\n")
        self.put("week_7/task_1/corpus/manifest.json", "{}\n")
        self.put("week_7/task_3/knowledge/faq.md", "FAQ\n")
        self.put("week_7/task_3/knowledge/manifest.json", "{}\n")
        for task, names in RUNTIME_FILES.items():
            for name in names:
                path = self.root / "week_7" / task / name
                if not path.exists():
                    self.put(f"week_7/{task}/{name}", f"required {task}/{name}\n")

    def put(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def members(self, output):
        if output.suffix == ".zip":
            with zipfile.ZipFile(output) as archive:
                return {name: archive.read(name) for name in archive.namelist()}
        with tarfile.open(output, "r:gz") as archive:
            return {entry.name: archive.extractfile(entry).read() for entry in archive}

    def test_manifest_matches_archived_bytes_and_excludes_private_files(self):
        forbidden = (
            "week_7/task_1/docs/internal.md", "week_7/task_1/data/corpus/manual.md",
            "week_7/task_1/.env", "week_7/task_1/graphics/.hidden.py",
            "week_7/task_3/scripts/other.py",
            "week_7/task_4/docs/report.md", "week_7/task_4/examples/private/secret.md",
            "week_7/task_5/demo/recording.mp4", "week_7/task_5/notes/private.md",
            "week_7/task_5/tests/__pycache__/secret.py", "week_7/task_5/scripts/other.py",
        )
        for name in forbidden:
            self.put(name, "SECRET SENTINEL")
        for suffix in (".zip", ".tar.gz"):
            with self.subTest(suffix=suffix):
                output = Path(self.temp.name) / ("delivery" + suffix)
                package(self.root, output)
                members = self.members(output)
                manifest = json.loads(members.pop("delivery-manifest.json"))
                self.assertEqual(set(manifest), set(members))
                for name, data in members.items():
                    self.assertEqual(manifest[name], {
                        "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)
                    })
                self.assertIn("week_7/task_1/docs/usage.md", members)
                self.assertIn("week_7/task_5/learning_product/web.py", members)
                self.assertIn("week_7/task_4/learning_files/static/files.js", members)
                self.assertIn("week_7/task_5/learning_product/static/materials.js", members)
                self.assertIn("week_7/task_3/tests/test_tickets.py", members)
                self.assertIn("week_7/task_1/templates/index.html", members)
                self.assertIn("week_7/task_1/static/app.js", members)
                self.assertIn("week_7/task_1/static/app.css", members)
                self.assertIn("week_7/task_3/support/mcp_client.py", members)
                self.assertIn("week_7/task_4/examples/textures.md", members)
                self.assertTrue(set(forbidden).isdisjoint(members))
                self.assertNotIn(b"SECRET SENTINEL", b"".join(members.values()))

    def test_delivered_test_dependencies_survive_extract(self):
        output = Path(self.temp.name) / "testable.zip"
        package(self.root, output)
        extracted = Path(self.temp.name) / "extracted"
        with zipfile.ZipFile(output) as archive:
            archive.extractall(extracted)
        for task, name in (
            ("task_1", "scripts/package_deployment.py"),
            ("task_3", "README.md"),
            ("task_3", "scripts/package_delivery.py"),
            ("task_3", "scripts/live_check.py"),
            ("task_4", "scripts/package_deployment.py"),
        ):
            with self.subTest(task=task, name=name):
                relative = Path("week_7") / task / name
                self.assertTrue((extracted / relative).is_file(), str(relative))
                self.assertEqual((extracted / relative).read_bytes(),
                                 (self.root / relative).read_bytes())

    def test_existing_archive_is_never_overwritten(self):
        output = Path(self.temp.name) / "delivery.zip"
        output.write_bytes(b"keep")
        with self.assertRaises(FileExistsError):
            package(self.root, output)
        self.assertEqual(output.read_bytes(), b"keep")

    def test_source_symlink_and_hardlink_are_denied(self):
        source = self.root / "week_7/task_5/learning_product/web.py"
        source.unlink()
        target = self.put("week_7/task_5/private/target.py", "SECRET")
        try:
            source.symlink_to(target)
        except (OSError, NotImplementedError) as error:
            symlink_available = False
        else:
            symlink_available = True
        if symlink_available:
            with self.assertRaises(ValueError):
                package(self.root, Path(self.temp.name) / "linked.zip")
            source.unlink()
        os.link(target, source)
        with self.assertRaises(ValueError):
            package(self.root, Path(self.temp.name) / "hardlinked.zip")

    def test_missing_runtime_entrypoint_is_error(self):
        (self.root / "week_7/task_5/server.py").unlink()
        output = Path(self.temp.name) / "missing.tar.gz"
        with self.assertRaises(ValueError):
            package(self.root, output)
        self.assertFalse(output.exists())

    def test_missing_product_document_is_error(self):
        (self.root / "week_7/task_1/docs/usage.md").unlink()
        output = Path(self.temp.name) / "missing-doc.zip"
        with self.assertRaises(ValueError):
            package(self.root, output)
        self.assertFalse(output.exists())

    def test_missing_required_runtime_module_is_error(self):
        required = [f"week_7/{task}/{name}" for task, names in RUNTIME_FILES.items()
                    for name in names]
        for index, name in enumerate(required):
            with self.subTest(name=name):
                path = self.root / name
                data = path.read_bytes()
                path.unlink()
                output = Path(self.temp.name) / f"missing-runtime-{index}.zip"
                try:
                    with self.assertRaises(ValueError):
                        package(self.root, output)
                    self.assertFalse(output.exists())
                finally:
                    path.write_bytes(data)
                    output.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()

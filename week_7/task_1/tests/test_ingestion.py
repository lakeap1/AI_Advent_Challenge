"""Catch navigation pollution, lost source sections and unsafe corpus paths."""
import importlib.util
from pathlib import Path
import tempfile
import unittest


def loader():
    path = Path(__file__).resolve().parents[1] / "scripts" / "ingest_corpus.py"
    spec = importlib.util.spec_from_file_location("ingest_corpus", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class IngestionTests(unittest.TestCase):
    def test_original_sections_survive_but_navigation_and_quiz_do_not(self):
        module = loader()
        html = """<main><nav>Account menu</nav><h1>Normal maps</h1>
        <p>A normal map changes the normal used for lighting, not the mesh silhouette.</p>
        <h2>Limitations</h2><p>At grazing angles the unchanged geometry remains visible.</p>
        <ul><li>Use a tangent basis matching the mesh.</li></ul>
        <div class='quiz'>Wrong answer: changes all vertices.</div>
        <script>secret()</script><footer>Cookie preferences</footer></main>"""
        result = module.extract_document(html, {"selector": "main"})
        self.assertIn("# Normal maps", result)
        self.assertIn("## Limitations", result)
        self.assertIn("not the mesh silhouette", result)
        self.assertIn("- Use a tangent basis matching the mesh.", result)
        for pollution in ("Account menu", "Wrong answer", "secret()", "Cookie preferences"):
            self.assertNotIn(pollution, result)

    def test_missing_article_never_falls_back_to_whole_navigation(self):
        with self.assertRaises(ValueError):
            loader().extract_document("<nav>Menu only</nav>", {"selector": "article"})

    def test_manifest_cannot_pull_secrets_or_escape_task_root(self):
        module = loader()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertEqual(module.source_path(root, "data/corpus/material.md"), root / "data/corpus/material.md")
            for path in ("../outside.md", ".env", "data/../.env", "notes/private.md", "docs/internal.md"):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    module.source_path(root, path)

    def test_remote_targets_are_fixed_public_source_hosts(self):
        module = loader()
        self.assertTrue(module.allowed_url("https://docs.blender.org/manual/en/4.5/render/index.html"))
        for url in ("http://127.0.0.1/secret", "https://127.0.0.1/", "file:///etc/passwd", "https://docs.blender.org.evil.invalid/a"):
            self.assertFalse(module.allowed_url(url))


if __name__ == "__main__":
    unittest.main()

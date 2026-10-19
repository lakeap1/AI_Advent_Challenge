import importlib.util
from pathlib import Path
import tarfile
import tempfile
import unittest


class DeploymentPackageTests(unittest.TestCase):
    def test_only_public_source_allowlist_is_packaged(self):
        script = Path(__file__).resolve().parents[1] / 'scripts/package_deployment.py'
        spec = importlib.util.spec_from_file_location('package_deployment', script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            files = ['README.md', 'requirements.txt', 'graphics/core.py', 'templates/index.html',
                     'docs/usage.md', 'corpus/manifest.json', '.env', 'data/secret.txt',
                     'demo/private.mp4', 'docs/private-plan.md', 'notes/private.txt',
                     'graphics/__pycache__/cached.pyc', '.git/config',
                     'scripts/record-demo.cjs', 'scripts/quality-capture.cjs', 'scripts/check_browser.cjs']
            for name in files:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('fixture', encoding='utf-8')
            archive = root / 'data/deployment.tar.gz'
            report = module.package(root, archive)
            with tarfile.open(archive) as bundle:
                names = set(bundle.getnames())
            self.assertEqual(names, {'README.md', 'requirements.txt', 'graphics/core.py',
                                     'templates/index.html', 'docs/usage.md', 'corpus/manifest.json',
                                     'scripts/check_browser.cjs'})
            self.assertEqual(report['files'], len(names))

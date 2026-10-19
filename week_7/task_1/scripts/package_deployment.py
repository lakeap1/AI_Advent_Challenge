"""Create a source-only deployment archive from a fixed public-file allowlist."""
import argparse
import hashlib
import json
from pathlib import Path
import tarfile


FILES = {'README.md', 'requirements.txt', 'app.py', 'server.py', '.env.example',
         'docs/usage.md', 'docs/architecture.md', 'docs/api.md', 'deploy/README.md',
         'corpus/manifest.json', 'corpus/glossary.md', 'scripts/check_browser.cjs'}
TREES = {'graphics': {'.py'}, 'tests': {'.py'}, 'scripts': {'.py'},
         'templates': {'.html'}, 'static': {'.css', '.js', '.svg'},
         'deploy': {'.sh', '.service'}}


def package(root, output):
    root = Path(root).resolve()
    output = Path(output).resolve()
    candidates = [root / name for name in FILES]
    for name in TREES:
        tree = root / name
        if tree.is_dir() and not tree.is_symlink():
            candidates.extend(tree.rglob('*'))
    allowed = []
    for path in set(candidates):
        relative = path.relative_to(root)
        if path.is_symlink() or any(part.startswith('.') and part != '.env.example'
                                    for part in relative.parts):
            continue
        if not path.is_file() or path == output or '__pycache__' in relative.parts:
            continue
        name = relative.as_posix()
        if name in FILES or (len(relative.parts) > 1
                             and path.suffix in TREES.get(relative.parts[0], set())):
            allowed.append(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(output, 'w:gz') as bundle:
        for path in sorted(allowed):
            bundle.add(path, arcname=path.relative_to(root).as_posix(), recursive=False)
    return {'files': len(allowed), 'sha256': hashlib.sha256(output.read_bytes()).hexdigest(),
            'paths': [path.relative_to(root).as_posix() for path in sorted(allowed)]}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('data/deployment.tar.gz'))
    args = parser.parse_args()
    print(json.dumps(package(Path(__file__).resolve().parents[1], args.output), indent=2))

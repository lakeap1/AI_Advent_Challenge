"""Package an explicit production allowlist, excluding data and credentials."""

import argparse
import hashlib
import json
from pathlib import Path
import tarfile


ROOT = Path(__file__).resolve().parents[3]
TASK = ROOT / 'week_6/task_5'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    manifest_path = output.with_suffix(output.suffix + '.manifest.json')
    if output.exists() or manifest_path.exists():
        parser.error('Output or manifest already exists; choose another filename')
    files = [TASK / name for name in
             ('app.py', 'bootstrap.py', 'server.py', 'config.toml', 'requirements.txt')]
    for folder, pattern in (
            (TASK / 'private_service', '*.py'),
            (TASK / 'templates', '*.html'),
            (TASK / 'static', '*.css'), (TASK / 'static', '*.js'),
            (TASK / 'deploy', '*.sh'), (TASK / 'deploy', '*.service'),
            (ROOT / 'week_6/task_2/local_agent', '*.py')):
        files.extend(folder.glob(pattern))
    files.append(ROOT / 'week_6/task_2/local_agent/config.toml')
    files = sorted(set(files))
    if any(not file.is_file() or file.is_symlink() for file in files):
        parser.error('Production input missing or is a symlink')
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest = []
    with tarfile.open(output, 'x:gz') as archive:
        for file in files:
            name = file.relative_to(ROOT).as_posix()
            data = file.read_bytes()
            archive.add(file, arcname=name, recursive=False)
            manifest.append({'path': name, 'bytes': len(data),
                             'sha256': hashlib.sha256(data).hexdigest()})
    manifest_path.write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    print(f'{output}: {len(files)} production files; manifest {manifest_path}')


if __name__ == '__main__':
    main()

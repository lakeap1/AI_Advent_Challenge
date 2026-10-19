#!/usr/bin/env bash
# Separate service/model cache, with existing model weights reused by reference.
set -euo pipefail
graphics_base="$HOME/ai-advent-day31"
graphics_previous="$HOME/ai-advent-day30"
graphics_source="$graphics_base/repo/week_7/task_1"
python3 - "$graphics_base" "$graphics_previous" <<'PY'
from pathlib import Path
import shutil, sys
base, previous = map(Path, sys.argv[1:])
old = previous / '.models'
new = base / '.models'
new.mkdir(mode=0o700, exist_ok=True)
blobs = new / 'blobs'
if blobs.is_symlink():
    if blobs.resolve() != (old / 'blobs').resolve():
        raise SystemExit('Existing model blob link does not match the selected runtime')
elif not blobs.exists():
    blobs.symlink_to(old / 'blobs', target_is_directory=True)
else:
    raise SystemExit('Existing model blob directory is preserved; configure it explicitly')
copied = []
for category in ('manifests', 'manifests-v2', 'metadata'):
    folder = old / category
    if not folder.exists():
        continue
    for item in folder.rglob('*'):
        if item.is_file() and 'qwen3-embedding' in item.as_posix():
            destination = new / item.relative_to(old)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, destination)
            copied.append(str(destination.relative_to(new)))
if not copied:
    raise SystemExit('The selected Qwen embedding manifest was not found')
print('Embedding manifests prepared:', len(copied))
PY
mkdir -p "$HOME/.config/systemd/user"
install -m 600 "$graphics_source/deploy/ai-advent-day31-embedding.service" "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now ai-advent-day31-embedding.service
systemctl --user is-active ai-advent-day31-embedding.service
# end

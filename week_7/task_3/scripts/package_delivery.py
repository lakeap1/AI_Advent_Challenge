"""Build a source-only Day 33 archive without replacing an existing file."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tarfile


TASK1_FILES = {
    "README.md", "requirements.txt", "app.py", "server.py", ".env.example",
    "docs/usage.md", "docs/api.md", "docs/architecture.md", "deploy/README.md",
    "corpus/manifest.json", "corpus/glossary.md", "corpus/support-faq.md",
}
TASK1_TREES = {
    "graphics": {".py"}, "scripts": {".py", ".cjs"},
    "templates": {".html"}, "static": {".css", ".js", ".svg"},
    "deploy": {".sh", ".service"}, "tests": {".py"},
}
TASK3_FILES = {
    "README.md", "requirements.txt", "knowledge/faq.md", "knowledge/manifest.json",
}
TASK3_TREES = {"support": {".py"}, "scripts": {".py", ".cjs"}, "tests": {".py"}}
MANIFEST_NAME = "delivery-manifest.json"


def _ordinary_file(root: Path, relative: Path) -> Path:
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError("Unsafe relative source path")
    current = root
    for part in relative.parts:
        current = current / part
        attributes = current.lstat()
        if stat.S_ISLNK(attributes.st_mode) or (
            getattr(attributes, "st_file_attributes", 0) &
            getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        ):
            raise ValueError(f"Linked source is forbidden: {relative.as_posix()}")
    if not current.is_file():
        raise ValueError(f"Source is not an ordinary file: {relative.as_posix()}")
    return current


def _paths(root: Path) -> list[Path]:
    result = []
    for task, files, trees in (
        ("task_1", TASK1_FILES, TASK1_TREES),
        ("task_3", TASK3_FILES, TASK3_TREES),
    ):
        base = Path("week_7") / task
        result.extend(base / name for name in files)
        for tree, suffixes in trees.items():
            directory = root / base / tree
            if directory.is_symlink() or not directory.is_dir():
                raise ValueError(f"Missing or linked source tree: {base / tree}")
            for candidate in directory.rglob("*"):
                relative = candidate.relative_to(root)
                if candidate.is_symlink():
                    raise ValueError(f"Linked source is forbidden: {relative.as_posix()}")
                if candidate.is_file() and candidate.suffix in suffixes and all(
                    not part.startswith(".") and part not in {"data", "demo", "__pycache__"}
                    for part in relative.parts
                ):
                    result.append(relative)
    return sorted(set(result), key=lambda path: path.as_posix())


def package(root: Path, output: Path) -> dict:
    root = Path(root).absolute()
    output = Path(output).absolute()
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Source root must be an ordinary directory")
    if output.exists() or output.is_symlink():
        raise FileExistsError(output)
    payloads = {}
    for relative in _paths(root):
        name = relative.as_posix()
        payloads[name] = _ordinary_file(root, relative).read_bytes()
    manifest = {"format": 1, "files": {
        name: {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        for name, data in payloads.items()
    }}
    payloads[MANIFEST_NAME] = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
                               + "\n").encode("utf-8")
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            with tarfile.open(fileobj=stream, mode="w:gz") as archive:
                for name, data in sorted(payloads.items()):
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    info.mode = 0o644
                    archive.addfile(info, io.BytesIO(data))
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    return {"archive": str(output), "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
            "files": len(manifest["files"])}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(package(args.root, args.output), ensure_ascii=False))


if __name__ == "__main__":
    main()

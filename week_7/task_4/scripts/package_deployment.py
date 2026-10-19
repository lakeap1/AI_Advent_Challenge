"""Build a source-only tar.gz for the day 34 application.

Only paths explicitly admitted below are packaged. Runtime data, local demos,
documentation notes, separate credential files, links and generated files are
not selected.
"""

import argparse
import hashlib
import io
import os
from pathlib import Path
import stat
import tarfile


ROOT_FILES = {"README.md", "requirements.txt", "server.py", ".gitignore"}
REQUIRED_ROOT_FILES = {"README.md", "requirements.txt", "server.py"}
EXCLUDED_DIRECTORIES = {
    "__pycache__", "data", "demo", "docs", "notes", "private", "secrets"
}
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _linked(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        return bool(getattr(path.lstat(), "st_file_attributes", 0) & _REPARSE)
    except FileNotFoundError:
        return False


def _source_paths(root: Path) -> list[Path]:
    """Enumerate only real files beneath the known public source folders."""
    paths = []
    for name in sorted(ROOT_FILES):
        path = root / name
        if path.is_file() and not _linked(path):
            paths.append(path)

    def walk(directory: Path, parts: tuple[str, ...]) -> None:
        if not directory.is_dir() or _linked(directory):
            return
        with os.scandir(directory) as entries:
            for entry in sorted(entries, key=lambda item: item.name):
                name = entry.name
                if (entry.is_symlink()
                        or getattr(entry.stat(follow_symlinks=False),
                                   "st_file_attributes", 0) & _REPARSE
                        or name.startswith(".")):
                    continue
                relative = (*parts, name)
                if entry.is_dir(follow_symlinks=False):
                    if name.casefold() in EXCLUDED_DIRECTORIES:
                        continue
                    if (parts == ("learning_files",)
                            or parts[:1] == ("learning_files",) and "static" not in parts):
                        walk(Path(entry.path), relative)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                suffix = Path(name).suffix
                admitted = (
                    parts[:1] == ("learning_files",)
                    and "static" not in parts and suffix == ".py"
                    or parts == ("learning_files", "static")
                    and suffix in {".js", ".css"}
                    or parts == ("examples",) and suffix == ".md"
                    or parts == ("tests",) and suffix == ".py"
                    or parts == ("scripts",) and suffix == ".py"
                )
                if admitted:
                    paths.append(Path(entry.path))

    for folder in ("learning_files", "examples", "tests", "scripts"):
        walk(root / folder, (folder,))
    return sorted(paths, key=lambda path: path.relative_to(root).as_posix())


def package(root: Path | str, output: Path | str) -> dict:
    """Write a new archive and return {files, paths, manifest, sha256}.

    A source file is read once. Its archived bytes and manifest hash come from
    that same read, so concurrent source edits cannot create a false manifest.
    """
    root = Path(root).absolute()
    output = Path(output).absolute()
    if not root.is_dir() or _linked(root) or any(_linked(parent) for parent in root.parents):
        raise ValueError("root must be a real directory")
    if not output.name.endswith(".tar.gz"):
        raise ValueError("output must end in .tar.gz")
    if output.exists() or _linked(output):
        raise FileExistsError(output)
    if any(_linked(parent) for parent in output.parents):
        raise ValueError("output path crosses a symlink")
    sources = _source_paths(root)
    if not REQUIRED_ROOT_FILES.issubset({path.name for path in sources if path.parent == root}):
        raise ValueError("root lacks required application files")
    if output in sources:
        raise ValueError("output would overwrite a source")
    payload = []
    manifest = {}
    for source in sources:
        if _linked(source) or not stat.S_ISREG(source.lstat().st_mode):
            continue
        name = source.relative_to(root).as_posix()
        data = source.read_bytes()
        payload.append((name, data))
        manifest[name] = hashlib.sha256(data).hexdigest()

    output.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        with output.open("xb") as stream:
            created = True
            with tarfile.open(fileobj=stream, mode="w:gz") as archive:
                for name, data in payload:
                    member = tarfile.TarInfo(name)
                    member.size = len(data)
                    member.mode = 0o644
                    member.mtime = 0
                    archive.addfile(member, io.BytesIO(data))
    except Exception:
        if created and output.exists():
            output.unlink()
        raise

    digest = hashlib.sha256()
    with output.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    names = [name for name, _ in payload]
    return {"files": len(names), "paths": names,
            "manifest": manifest, "sha256": digest.hexdigest()}


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a day 34 source-only tar.gz")
    parser.add_argument("root", type=Path, help="week_7/task_4 directory")
    parser.add_argument("output", type=Path, help="new .tar.gz path")
    args = parser.parse_args()
    result = package(args.root, args.output)
    print(f"Packaged {result['files']} files; archive SHA-256 {result['sha256']}")


if __name__ == "__main__":
    main()

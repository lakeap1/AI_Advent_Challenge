"""Package the four Day 35 source trees with an archive-byte manifest."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tarfile
import zipfile


ROOT_FILES = {
    "task_1": {"README.md", "requirements.txt", "app.py", "server.py", ".env.example"},
    "task_3": {"README.md", "requirements.txt", "knowledge/manifest.json",
               "knowledge/faq.md"},
    "task_4": {"README.md", "requirements.txt", "server.py"},
    "task_5": {"README.md", "requirements.txt", "server.py", ".gitignore"},
}
REQUIRED = {
    "task_1": {"README.md", "requirements.txt", "app.py", "server.py",
               "docs/usage.md", "docs/architecture.md", "docs/api.md",
               "corpus/manifest.json", "corpus/glossary.md", "corpus/support-faq.md",
               "templates/index.html", "static/app.js", "static/app.css",
               "scripts/package_deployment.py",
               *{f"graphics/{name}.py" for name in (
                   "__init__", "chunking", "cli", "config", "context", "hybrid",
                   "mcp_client", "mcp_server", "policies", "provider", "retrieval",
                   "service", "store", "support_integration", "validation", "web")}},
    "task_3": {"README.md", "requirements.txt", "scripts/package_delivery.py",
               "scripts/live_check.py",
               *{f"support/{name}.py" for name in (
                   "__init__", "__main__", "domain", "mcp_client", "mcp_server",
                   "ports", "service_bridge", "smoke", "tickets", "tools")}},
    "task_4": {"README.md", "requirements.txt", "server.py",
               "scripts/package_deployment.py",
               "learning_files/__init__.py", "learning_files/agent.py",
               "learning_files/repository.py", "learning_files/workspace.py",
               "learning_files/web.py", "learning_files/static/files.js",
               "learning_files/static/files.css",
               *{f"examples/{name}.md" for name in (
                   "materials", "normal-maps", "normal-theory", "normal-unity",
                   "shader-pipeline", "textures")}},
    "task_5": {"README.md", "requirements.txt", "server.py",
               "learning_product/__init__.py", "learning_product/materials.py",
               "learning_product/web.py", "learning_product/static/materials.js",
               "learning_product/static/materials.css"},
}
TREES = {
    "task_1": {"graphics": {".py"}, "static": {".js", ".css", ".svg"},
               "templates": {".html"}, "tests": {".py"},
               "deploy": {".sh", ".service"}},
    "task_3": {"support": {".py"}, "tests": {".py"}},
    "task_4": {"learning_files": {".py", ".js", ".css"}, "tests": {".py"},
               "examples": {".md"}},
    "task_5": {"learning_product": {".py", ".js", ".css"}, "tests": {".py"}},
}
EXACT = {
    "task_1": {"docs/usage.md", "docs/architecture.md", "docs/api.md",
               "corpus/manifest.json", "corpus/glossary.md", "corpus/support-faq.md",
               "deploy/README.md", "scripts/ingest_corpus.py", "scripts/index_corpus.py",
               "scripts/package_deployment.py",
               "scripts/calibrate_context.py"},
    "task_3": {"scripts/package_delivery.py", "scripts/live_check.py"},
    "task_4": {"scripts/package_deployment.py"},
    "task_5": {"scripts/package_delivery.py", "scripts/record-day35.cjs",
               "scripts/quality-capture.cjs"},
}
FORBIDDEN = {"data", "notes", "demo", "private", ".recording-tmp", ".venv",
             "__pycache__", ".git", "secrets", "snapshots"}
REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _check_real(path: Path, *, file: bool = False) -> None:
    for item in (path, *path.parents):
        details = item.lstat()
        if stat.S_ISLNK(details.st_mode) or getattr(details, "st_file_attributes", 0) & REPARSE:
            raise ValueError(f"Linked path is forbidden: {item}")
    details = path.lstat()
    if file and (not stat.S_ISREG(details.st_mode) or details.st_nlink != 1):
        raise ValueError(f"Source must be an ordinary, unlinked file: {path}")
    if not file and not stat.S_ISDIR(details.st_mode):
        raise ValueError(f"Source must be a directory: {path}")


def _tree_files(directory: Path, suffixes: set[str], *, task: str, tree: str):
    if not directory.exists() and not directory.is_symlink():
        return
    _check_real(directory)
    for candidate in sorted(directory.rglob("*")):
        parts = candidate.relative_to(directory).parts
        if any(part.startswith(".") or part.casefold() in FORBIDDEN for part in parts):
            continue
        if candidate.suffix not in suffixes:
            continue
        if tree in {"examples", "tests", "static", "templates", "deploy"} and len(parts) != 1:
            continue
        if tree == "learning_files" and (len(parts) > 1 and parts[0] == "static"):
            if len(parts) != 2 or candidate.suffix not in {".js", ".css"}:
                continue
        elif tree == "learning_product" and (len(parts) > 1 and parts[0] == "static"):
            if len(parts) != 2 or candidate.suffix not in {".js", ".css"}:
                continue
        elif len(parts) != 1:
            continue
        elif tree in {"learning_files", "learning_product"} and candidate.suffix != ".py":
            continue
        _check_real(candidate, file=True)
        yield candidate


def _sources(root: Path) -> list[Path]:
    result = []
    for task in ("task_1", "task_3", "task_4", "task_5"):
        base = root / "week_7" / task
        _check_real(base)
        for name in sorted(REQUIRED[task]):
            if not (base / name).is_file():
                raise ValueError(f"Missing required runtime source: week_7/{task}/{name}")
        for name in sorted(ROOT_FILES[task] | EXACT[task]):
            path = base / name
            if path.exists() or path.is_symlink():
                _check_real(path, file=True)
                result.append(path)
        for tree, suffixes in TREES[task].items():
            result.extend(_tree_files(base / tree, suffixes, task=task, tree=tree))
    return sorted(set(result), key=lambda path: path.relative_to(root).as_posix())


def package(root: Path | str, output: Path | str) -> dict:
    """Create ZIP or tar.gz exclusively; manifest hashes the exact archived bytes."""
    root, output = Path(root).absolute(), Path(output).absolute()
    _check_real(root)
    if output.name.endswith(".tar.gz"):
        kind = "tar"
    elif output.suffix == ".zip":
        kind = "zip"
    else:
        raise ValueError("Output must end in .zip or .tar.gz")
    if output.exists() or output.is_symlink():
        raise FileExistsError(output)
    _check_real(output.parent)
    payloads = {}
    for source in _sources(root):
        name = source.relative_to(root).as_posix()
        payloads[name] = source.read_bytes()
    manifest = {name: {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
                for name, data in payloads.items()}
    payloads["delivery-manifest.json"] = (json.dumps(
        manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")

    descriptor = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            if kind == "zip":
                with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                    for name, data in sorted(payloads.items()):
                        archive.writestr(name, data)
            else:
                with tarfile.open(fileobj=stream, mode="w:gz") as archive:
                    for name, data in sorted(payloads.items()):
                        member = tarfile.TarInfo(name)
                        member.size = len(data)
                        member.mode = 0o644
                        member.mtime = 0
                        archive.addfile(member, io.BytesIO(data))
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    return {"archive": str(output), "files": len(manifest),
            "sha256": hashlib.sha256(output.read_bytes()).hexdigest()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(package(args.root, args.output), ensure_ascii=False))


if __name__ == "__main__":
    main()

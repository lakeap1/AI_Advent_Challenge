"""Read only declared local UTF-8 sources after path checks."""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePath
from urllib.parse import urlsplit


class CorpusError(ValueError):
    def __init__(self, message, code="unsafe_path"):
        super().__init__(message)
        self.metadata = {"policy_code": code}


@dataclass(frozen=True)
class Document:
    file: str
    source: str
    title: str
    text: str
    document_hash: str


@dataclass(frozen=True)
class Corpus:
    documents: tuple[Document, ...]
    summary: dict


_FORBIDDEN = {"data", "docs", "demo", "notes", ".venv", "tests", "tests_indexing", "__pycache__"}


def _safe_path(root, relative):
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise CorpusError("Unsafe corpus path")
    value = PurePath(relative)
    parts = value.parts
    if value.is_absolute() or not parts or any(part in (".", "..", "") or part.lower() in _FORBIDDEN or part.lower().startswith(".env") for part in parts):
        raise CorpusError("Unsafe corpus path")
    if value.suffix.lower() not in (".py", ".md") or ":" in relative:
        raise CorpusError("Unsafe corpus path")
    root = Path(root)
    path = root.joinpath(*parts)
    probe = root
    for part in parts:
        probe = probe / part
        try:
            stat = probe.lstat()
        except OSError as error:
            raise CorpusError("Corpus source missing or inaccessible") from error
        if probe.is_symlink() or getattr(stat, "st_file_attributes", 0) & 0x400:
            raise CorpusError("Corpus source is a link or reparse point")
    if not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
        raise CorpusError("Unsafe corpus path")
    return path


def read_corpus(task_root, manifest_path=None):
    root = Path(task_root)
    manifest = Path(manifest_path) if manifest_path is not None else root / "corpus" / "manifest.json"
    if manifest_path is not None and manifest != root / "corpus" / "manifest.json":
        raise CorpusError("Unsupported manifest path")
    for probe in (root, root / "corpus", manifest):
        try:
            stat = probe.lstat()
        except OSError as error:
            raise CorpusError("Corpus manifest missing or inaccessible") from error
        if probe.is_symlink() or getattr(stat, "st_file_attributes", 0) & 0x400:
            raise CorpusError("Corpus manifest uses a link or reparse point")
    try:
        raw = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CorpusError("Invalid corpus manifest") from error
    if not isinstance(raw, dict):
        raise CorpusError("Invalid corpus manifest")
    files = raw.get("files")
    if raw.get("version") != 1 or not isinstance(files, list):
        raise CorpusError("Invalid corpus manifest")
    documents = []
    seen = set()
    for entry in files:
        if isinstance(entry, str):
            relative, source, title = entry, entry, None
        elif isinstance(entry, dict) and set(entry) == {"file", "source", "title"}:
            relative, source, title = entry["file"], entry["source"], entry["title"]
            try:
                parsed = urlsplit(source) if isinstance(source, str) and not any(
                    ord(character) < 33 for character in source) else None
            except ValueError:
                parsed = None
            if (parsed is None or parsed.scheme != "https" or not parsed.netloc
                    or parsed.username or parsed.password or not parsed.path
                    or not isinstance(title, str) or not title.strip() or len(title) > 160
                    or any(ord(character) < 32 for character in title)):
                raise CorpusError("Invalid corpus source metadata", code="invalid_source")
        else:
            raise CorpusError("Invalid corpus manifest entry", code="invalid_source")
        path = _safe_path(root, relative)
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        try:
            value = data.decode("utf-8")
        except UnicodeError as error:
            raise CorpusError("Corpus source must be UTF-8") from error
        documents.append(Document(relative, source, title or path.name, value, digest))
    characters = sum(len(doc.text) for doc in documents)
    combined = hashlib.sha256(json.dumps([(d.file, d.document_hash, d.source, d.title)
        for d in documents], ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
    return Corpus(tuple(documents), {"documents": len(documents), "characters": characters,
                                     "files": [{"file": doc.file, "source": doc.source, "title": doc.title,
                                                "document_hash": doc.document_hash, "characters": len(doc.text)}
                                               for doc in documents],
                                     "lines": sum(d.text.count("\n") + (bool(d.text) and not d.text.endswith("\n")) for d in documents),
                                     "code_lines": sum(d.text.count("\n") + 1 for d in documents if d.file.endswith(".py")),
                                     "estimated_pages": characters / 1800, "page_characters": 1800,
                                     "corpus_hash": combined, "provenance": raw.get("provenance", "")})

"""Native read-only file tools for one authenticated user."""

import hashlib
import os
import re
import stat
from pathlib import Path

MAX_DOCUMENTS = 24
MAX_BYTES = 16384
FOLDERS = ("sources", "notes", "outputs")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\.md\Z")
_DEVICE = re.compile(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])\.md\Z")
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def valid_user(user_id):
    if type(user_id) is not int or user_id <= 0:
        raise ValueError("invalid user")
    return user_id


def valid_path(path, *, writable=False, seed=False):
    if not isinstance(path, str) or len(path) > 100 or path.count("/") != 1:
        raise ValueError("invalid path")
    folder, name = path.split("/")
    if folder not in FOLDERS or not _NAME.fullmatch(name) or _DEVICE.fullmatch(name):
        raise ValueError("invalid path")
    if (seed and folder not in ("sources", "notes")) or (writable and folder == "sources"):
        raise ValueError("path is not writable")
    return folder, name


def text_bytes(content):
    if not isinstance(content, str) or "\x00" in content:
        raise ValueError("invalid text")
    try:
        payload = content.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ValueError("invalid UTF-8") from exc
    if len(payload) > MAX_BYTES:
        raise ValueError("document too large")
    return payload


def digest(content):
    return hashlib.sha256(text_bytes(content)).hexdigest()


def _safe_directory(path):
    if not path.exists() and not path.is_symlink():
        return False
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & _REPARSE:
        raise PermissionError("unsafe directory")
    return True


def _safe_file(path):
    if not path.exists() and not path.is_symlink():
        return False
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or getattr(metadata, "st_file_attributes", 0) & _REPARSE:
        raise PermissionError("unsafe file")
    return True


class Workspace:
    def __init__(self, root, user_id):
        self.root = Path(root)
        self.user_id = valid_user(user_id)
        self.base = self.root / "users" / str(user_id)
        _safe_directory(self.root)

    def _checked_path(self, path, *, writable=False, seed=False, create=False):
        folder, name = valid_path(path, writable=writable, seed=seed)
        users = self.root / "users"
        for directory in (self.root, users, self.base, self.base / folder):
            if not _safe_directory(directory):
                if not create:
                    return self.base / folder / name
                directory.mkdir(mode=0o700)
        target = self.base / folder / name
        _safe_file(target)
        return target

    def read(self, path):
        target = self._checked_path(path)
        if not _safe_file(target):
            raise FileNotFoundError("document missing")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        handle = os.open(target, flags)
        try:
            metadata = os.fstat(handle)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise PermissionError("unsafe file")
            if metadata.st_size > MAX_BYTES:
                raise ValueError("document too large")
            data = b""
            while len(data) <= MAX_BYTES:
                chunk = os.read(handle, MAX_BYTES + 1 - len(data))
                if not chunk:
                    break
                data += chunk
            if len(data) > MAX_BYTES:
                raise ValueError("document too large")
            try:
                content = data.decode("utf-8", errors="strict")
            except UnicodeError as exc:
                raise ValueError("invalid UTF-8") from exc
            if "\x00" in content:
                raise ValueError("binary document")
            return {"path": path, "content": content, "sha256": hashlib.sha256(data).hexdigest()}
        finally:
            os.close(handle)

    def inventory(self):
        result = []
        for directory in (self.root, self.root / "users", self.base):
            if not _safe_directory(directory):
                return []
        for folder in FOLDERS:
            directory = self.base / folder
            if not _safe_directory(directory):
                continue
            for entry in sorted(directory.iterdir(), key=lambda p: p.name):
                path = f"{folder}/{entry.name}"
                valid_path(path)
                snapshot = self.read(path)
                result.append({"path": path, "sha256": snapshot["sha256"],
                               "bytes": len(snapshot["content"].encode("utf-8")),
                               "excerpt": snapshot["content"][:180]})
                if len(result) > MAX_DOCUMENTS:
                    raise ValueError("too many documents")
        return result

    def search(self, query):
        if not isinstance(query, str) or not query.strip() or len(query) > 200:
            raise ValueError("invalid query")
        terms = [word for word in re.findall(r"\w+", query.casefold()) if len(word) >= 3]
        if not terms:
            raise ValueError("invalid query")
        ranked = []
        for item in self.inventory():
            path_words = re.findall(r"\w+", item["path"].casefold())
            content_words = re.findall(r"\w+", self.read(item["path"])["content"].casefold())
            score = 0
            for term in set(terms):
                stem = term[:5] if len(term) >= 5 else term
                if any(word.startswith(stem) for word in path_words):
                    score += 2
                elif any(word.startswith(stem) for word in content_words):
                    score += 1
            if score:
                ranked.append((-score, item["path"], item))
        return [item for _, _, item in sorted(ranked)]

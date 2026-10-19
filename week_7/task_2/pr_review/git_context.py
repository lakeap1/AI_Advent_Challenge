"""Bounded, pinned Git context for a later review stage.

This module prepares evidence only. It never executes the code being reviewed.
"""

from dataclasses import asdict, dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import threading


class PreparationError(Exception):
    """Preparation failed; the message is a stable, safe reason code."""


@dataclass(frozen=True)
class Limits:
    max_files: int = 200
    max_related_paths: int = 50
    max_blob_bytes: int = 131072
    max_patch_bytes: int = 262144
    max_total_source_bytes: int = 2097152
    max_total_patch_bytes: int = 4194304
    max_command_output_bytes: int = 8388608
    timeout_seconds: int = 15
    max_lines: int = 4000

    def __post_init__(self):
        for name, value in asdict(self).items():
            if type(value) is not int or value <= 0:
                raise PreparationError(f"invalid_limit:{name}")


_OID = re.compile(rb"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_DATABASE_SUFFIXES = (".db", ".sqlite", ".sqlite3", ".mdb", ".rdb")
_SENSITIVE_NAMES = {
    "credentials", "credentials.json", "credentials.yaml", "credentials.yml",
    "secrets", "secrets.json", "secrets.yaml", "secrets.yml",
    "id_rsa", "id_ed25519", "id_ecdsa", "known_hosts", "authorized_keys",
    "private.key", "server.key", "token", "tokens.json", "passwords.txt",
}
_SENSITIVE_PARTS = {".git", "notes", "demo", "data", "private", "runtime"}


def _lf_lines(text):
    """Split Git lines at LF only, preserving every original character."""
    parts = text.split("\n")
    return [part + "\n" for part in parts[:-1]] + ([parts[-1]] if parts[-1] else [])


def _safe_env():
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_OPTIONAL_LOCKS": "0", "GIT_NO_REPLACE_OBJECTS": "1",
                "GIT_NO_LAZY_FETCH": "1", "GIT_ALLOW_PROTOCOL": "",
                "GIT_PAGER": "cat", "GIT_TERMINAL_PROMPT": "0",
                "GIT_ATTR_NOSYSTEM": "1"})
    return env


def _git(repo, args, limit, timeout):
    # The caller has selected this resolved root explicitly. Trust it for this
    # read-only process only; sanitized global/system Git config may not carry
    # the host's safe.directory entry (for example in a managed sandbox).
    command = ["git", "--no-pager", "--literal-pathspecs", "--no-lazy-fetch",
               "-c", f"safe.directory={os.fspath(repo)}",
               "-c", "core.fsmonitor=false", "-c", "diff.external=", *args]
    try:
        process = subprocess.Popen(command, cwd=repo, env=_safe_env(),
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, shell=False)
    except (OSError, ValueError) as exc:
        raise PreparationError("git_unavailable") from exc
    chunks = [[], []]
    sizes = [0, 0]
    exceeded = threading.Event()

    def drain(stream, index):
        while True:
            data = stream.read(65536)
            if not data:
                return
            sizes[index] += len(data)
            if sizes[index] > limit:
                exceeded.set()
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                return
            chunks[index].append(data)

    threads = [threading.Thread(target=drain, args=(process.stdout, 0), daemon=True),
               threading.Thread(target=drain, args=(process.stderr, 1), daemon=True)]
    for thread in threads:
        thread.start()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.wait()
        raise PreparationError("git_timeout") from exc
    finally:
        for thread in threads:
            thread.join(timeout=1)
        process.stdout.close()
        process.stderr.close()
    if exceeded.is_set():
        raise PreparationError("git_output_limit")
    return process.returncode, b"".join(chunks[0])


def _run(repo, args, limits, *, output_limit=None, failure="git_command_failed"):
    cap = limits.max_command_output_bytes
    if output_limit is not None:
        cap = min(cap, output_limit)
    code, output = _git(repo, args, cap,
                        limits.timeout_seconds)
    if code:
        raise PreparationError(failure)
    return output


def _valid_ref(ref):
    if not isinstance(ref, str) or not ref or ref.startswith("-") or "\x00" in ref or "\n" in ref or "\r" in ref:
        raise PreparationError("invalid_ref")
    return ref


def _resolve(repo, ref, limits):
    ref = _valid_ref(ref)
    output = _run(repo, ["rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"],
                  limits, failure="invalid_ref").strip()
    if not _OID.fullmatch(output):
        raise PreparationError("invalid_ref")
    return output.decode("ascii")


def _valid_path(path):
    if not isinstance(path, str) or not path or "\x00" in path or "\\" in path:
        raise PreparationError("invalid_path")
    pure = PurePosixPath(path)
    if (pure.is_absolute() or len(path) >= 2 and path[1] == ":" or
            any(part in ("", ".", "..") for part in path.split("/")) or
            any(part.casefold() == ".git" for part in pure.parts)):
        raise PreparationError("invalid_path")
    return path


def _sensitive(path):
    parts = [part.casefold() for part in PurePosixPath(path).parts]
    name = parts[-1]
    return (any(part in _SENSITIVE_PARTS for part in parts) or
            name.startswith(".env") or name in _SENSITIVE_NAMES or
            name.endswith(_DATABASE_SUFFIXES) or
            name.endswith((".pem", ".p12", ".pfx", ".key", ".kdbx")) or
            any(marker in name for marker in
                ("credential", "secret", "password", "passwd", "api_key",
                 "apikey", "access_key", "private_key", "client_key", "token")))


def _decode_path(raw):
    try:
        return raw.decode("utf-8", "surrogateescape")
    except UnicodeDecodeError as exc:
        raise PreparationError("invalid_git_path") from exc


def _changes(repo, base, head, limits):
    output = _run(repo, ["diff", "--no-ext-diff", "--no-textconv", "--find-renames",
                         "--name-status", "-z", base, head, "--"], limits,
                  failure="diff_failed")
    tokens = output.split(b"\x00")
    if tokens[-1] != b"":
        raise PreparationError("malformed_diff")
    tokens.pop()
    result = []
    pos = 0
    while pos < len(tokens):
        status = tokens[pos].decode("ascii", "strict")
        pos += 1
        kind = status[:1]
        if kind not in "AMDRTC" or pos >= len(tokens):
            raise PreparationError("unsupported_change")
        old_path = None
        if kind in "RC":
            if pos + 1 >= len(tokens):
                raise PreparationError("malformed_diff")
            old_path = _decode_path(tokens[pos])
            pos += 1
        path = _decode_path(tokens[pos])
        pos += 1
        result.append({"status": kind, "path": path, "old_path": old_path,
                       "patch": None})
        if len(result) > limits.max_files:
            raise PreparationError("too_many_files")
    return result


def _tree_entry(repo, revision, path, limits):
    output = _run(repo, ["ls-tree", "-z", revision, "--", path], limits,
                  failure="tree_lookup_failed")
    for record in output.split(b"\x00"):
        if not record:
            continue
        try:
            header, raw_path = record.split(b"\t", 1)
            mode, kind, oid = header.split(b" ")
        except ValueError as exc:
            raise PreparationError("malformed_tree") from exc
        if _decode_path(raw_path) == path:
            if not _OID.fullmatch(oid):
                raise PreparationError("malformed_tree")
            return mode, kind, oid.decode("ascii")
    return None


def _blob(repo, oid, size, limits):
    output = _run(repo, ["cat-file", "blob", oid], limits,
                  output_limit=size + 1, failure="blob_read_failed")
    if len(output) != size:
        raise PreparationError("blob_size_mismatch")
    return output


def _source(repo, revision, side, path, limits, budget, omissions):
    if _sensitive(path):
        omissions.append({"path": path, "side": side, "reason": "sensitive_path"})
        return None
    entry = _tree_entry(repo, revision, path, limits)
    if entry is None:
        omissions.append({"path": path, "side": side, "reason": "missing"})
        return None
    mode, kind, oid = entry
    if mode == b"120000":
        reason = "symlink"
    elif mode == b"160000" or kind == b"commit":
        reason = "submodule"
    elif kind != b"blob":
        reason = "unsupported_object"
    else:
        reason = None
    if reason:
        omissions.append({"path": path, "side": side, "reason": reason})
        return None
    raw_size = _run(repo, ["cat-file", "-s", oid], limits, failure="blob_size_failed").strip()
    if not raw_size.isdigit():
        raise PreparationError("blob_size_failed")
    size = int(raw_size)
    if size > limits.max_blob_bytes:
        reason = "oversized_blob"
    elif size > budget[0]:
        reason = "source_budget"
    else:
        reason = None
    if reason:
        omissions.append({"path": path, "side": side, "reason": reason})
        return None
    data = _blob(repo, oid, size, limits)
    if b"\x00" in data:
        reason = "binary"
    else:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            reason = "invalid_utf8"
    if not reason and len(_lf_lines(text)) > limits.max_lines:
        reason = "line_limit"
    if reason:
        omissions.append({"path": path, "side": side, "reason": reason})
        return None
    budget[0] -= size
    source_id = "git:" + hashlib.sha256(
        (revision + "\x00" + path + "\x00" + oid).encode("utf-8", "surrogateescape")
    ).hexdigest()
    lines = _lf_lines(text)
    return {"source_id": source_id, "revision": revision, "path": path,
            "blob_oid": oid, "start_line": 1 if lines else 0,
            "end_line": len(lines), "text": text,
            "content_sha256": hashlib.sha256(data).hexdigest(), "side": side}


def _patch(repo, base, head, change, limits, budget, omissions):
    path = change["path"]
    old = change["old_path"]
    if _sensitive(path) or old and _sensitive(old):
        omissions.append({"path": path, "side": "patch", "reason": "sensitive_path"})
        return None
    for revision, candidate in ((base, old or path), (head, path)):
        entry = _tree_entry(repo, revision, candidate, limits)
        if entry is not None and entry[0] in (b"120000", b"160000"):
            reason = "symlink" if entry[0] == b"120000" else "submodule"
            omissions.append({"path": path, "side": "patch", "reason": reason})
            return None
        if entry is not None and entry[1] == b"blob":
            raw_size = _run(repo, ["cat-file", "-s", entry[2]], limits,
                            failure="blob_size_failed").strip()
            if not raw_size.isdigit():
                raise PreparationError("blob_size_failed")
            if int(raw_size) > limits.max_blob_bytes:
                omissions.append({"path": path, "side": "patch", "reason": "oversized_blob"})
                return None
    args = ["diff", "--no-ext-diff", "--no-textconv", "--no-color", "--find-renames",
            "--unified=3", base, head, "--", *( [old, path] if old else [path] )]
    try:
        raw = _run(repo, args, limits, output_limit=limits.max_patch_bytes + 1,
                   failure="patch_failed")
    except PreparationError as exc:
        if str(exc) != "git_output_limit":
            raise
        reason = ("oversized_patch" if limits.max_patch_bytes + 1 <=
                  limits.max_command_output_bytes else "command_output_limit")
        omissions.append({"path": path, "side": "patch", "reason": reason})
        return None
    if len(raw) > limits.max_patch_bytes:
        reason = "oversized_patch"
    elif len(raw) > budget[0]:
        reason = "patch_budget"
    elif b"\x00" in raw or b"Binary files " in raw or b"GIT binary patch" in raw:
        reason = "binary"
    else:
        try:
            patch = raw.decode("utf-8")
        except UnicodeDecodeError:
            reason = "invalid_utf8"
        else:
            reason = None
    if reason:
        omissions.append({"path": path, "side": "patch", "reason": reason})
        return None
    budget[0] -= len(raw)
    return patch


def prepare_snapshot(repo, base, head, *, related_paths=(), limits=None) -> dict:
    """Read bounded evidence from immutable commits, without touching the index/worktree."""
    limits = Limits() if limits is None else limits
    if not isinstance(limits, Limits):
        raise PreparationError("invalid_limits")
    try:
        root = Path(repo).resolve(strict=True)
    except (OSError, TypeError, ValueError) as exc:
        raise PreparationError("invalid_repo") from exc
    if not root.is_dir() or root.name == ".git":
        raise PreparationError("invalid_repo")
    output = _run(root, ["rev-parse", "--show-toplevel"], limits, failure="invalid_repo").strip()
    try:
        actual_root = Path(os.fsdecode(output)).resolve(strict=True)
    except (OSError, ValueError) as exc:
        raise PreparationError("invalid_repo") from exc
    if root != actual_root:
        raise PreparationError("invalid_repo")
    if isinstance(related_paths, (str, bytes)):
        raise PreparationError("invalid_related_paths")
    try:
        related = tuple(dict.fromkeys(_valid_path(path) for path in related_paths))
    except TypeError as exc:
        raise PreparationError("invalid_related_paths") from exc
    if len(related) > limits.max_related_paths:
        raise PreparationError("too_many_related_paths")
    base_oid = _resolve(root, base, limits)
    head_oid = _resolve(root, head, limits)
    merge_base = _run(root, ["merge-base", base_oid, head_oid], limits,
                      failure="no_merge_base").strip()
    if not _OID.fullmatch(merge_base):
        raise PreparationError("no_merge_base")
    merge_oid = merge_base.decode("ascii")
    changed = _changes(root, merge_oid, head_oid, limits)
    if len(changed) + len(related) > limits.max_files:
        raise PreparationError("too_many_files")
    sensitive_origin_paths = set()
    for change in changed:
        old = change["old_path"]
        if change["status"] in ("R", "C") and old is not None:
            if _sensitive(old) or _sensitive(change["path"]):
                sensitive_origin_paths.update((old, change["path"]))
    omissions = []
    sources = []
    source_budget = [limits.max_total_source_bytes]
    patch_budget = [limits.max_total_patch_bytes]
    seen = set()

    def add_source(revision, side, path):
        # A pinned revision/path fixes the blob identity. The first observation
        # supplies a valid side even when base and head are the same revision.
        key = (revision, path)
        if key in seen:
            return
        seen.add(key)
        if path in sensitive_origin_paths:
            omissions.append({"path": path, "side": side, "reason": "sensitive_origin"})
            return
        source = _source(root, revision, side, path, limits, source_budget, omissions)
        if source is not None:
            sources.append(source)

    for change in changed:
        change["patch"] = _patch(root, merge_oid, head_oid, change, limits,
                                 patch_budget, omissions)
        if change["status"] != "A":
            add_source(merge_oid, "base", change["old_path"] or change["path"])
        if change["status"] != "D":
            add_source(head_oid, "head", change["path"])
    for path in related:
        add_source(merge_oid, "base", path)
        add_source(head_oid, "head", path)
    return {"schema_version": 1, "base_revision": base_oid,
            "head_revision": head_oid, "merge_base": merge_oid,
            "changed_files": changed, "sources": sources,
            "coverage": {"complete": not omissions, "omissions": omissions},
            "limits": asdict(limits)}

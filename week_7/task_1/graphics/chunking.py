"""Structural character slices adapted from week_5/task_5/indexing/chunking.py.

The original section and exact-offset approach is retained; this module has
no cross-assignment imports or embedding-provider assumptions.
"""
import hashlib
import json
import re


def _sections(text):
    starts = [(0, "Document")]
    headings = []
    fence = None
    offset = 0
    for line in text.splitlines(keepends=True):
        marker = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
        elif fence is None:
            match = re.match(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$", line.rstrip("\r\n"))
            if match:
                level = len(match.group(1))
                headings = headings[:level - 1] + [match.group(2)]
                label = " / ".join(headings)
                if offset == 0:
                    starts[0] = (0, label)
                else:
                    starts.append((offset, label))
        offset += len(line)
    return starts


def structural_chunks(text, source, chunk_size=2400, overlap=160):
    """Return bounded exact slices within Markdown sections and their provenance."""
    if not isinstance(text, str) or not isinstance(source, dict):
        raise TypeError("text and source metadata are required")
    if type(chunk_size) is not int or type(overlap) is not int or chunk_size <= 0 or not 0 <= overlap < chunk_size:
        raise ValueError("invalid chunk size or overlap")
    required = ("id", "title", "url", "domain", "version", "pipeline", "language", "license", "path")
    if any(not isinstance(source.get(key), str) or not source[key] for key in required):
        raise ValueError("incomplete source provenance")
    document_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    starts = _sections(text)
    chunks = []
    for index, (left, label) in enumerate(starts):
        right = starts[index + 1][0] if index + 1 < len(starts) else len(text)
        position = left
        while position < right:
            end = min(position + chunk_size, right)
            value = text[position:end]
            if value.strip():
                identity = json.dumps([source["id"], document_hash, chunk_size, overlap, position, end],
                                      ensure_ascii=False, separators=(",", ":"))
                chunks.append({"chunk_id": hashlib.sha256(identity.encode("utf-8")).hexdigest(),
                               "text": value, "title": source["title"], "section": label,
                               "url": source["url"], "version": source["version"],
                               "pipeline": source["pipeline"], "language": source["language"],
                               "domain": source["domain"], "license": source["license"],
                               "source_id": source["id"], "path": source["path"],
                               "document_hash": document_hash, "start": position, "end": end})
            if end == right:
                break
            position = end - overlap
    return chunks

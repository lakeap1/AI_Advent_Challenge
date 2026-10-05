"""Exact character-slice chunking with Markdown and Python boundaries."""
import ast
import hashlib
import json
import re


def _check(strategy, size, overlap):
    if strategy not in ("fixed", "structural"):
        raise ValueError("unknown chunking strategy")
    if type(size) is not int or size <= 0 or type(overlap) is not int or overlap < 0 or overlap >= size:
        raise ValueError("invalid chunk size or overlap")


def _markdown_sections(text):
    starts = [(0, "Document")]
    fence = None
    headings = []
    position = 0
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
                headings = headings[:level-1] + [match.group(2)]
                title = " / ".join(headings)
                if position == 0:
                    starts[0] = (0, title)
                else:
                    starts.append((position, title))
        position += len(line)
    return starts


def _python_sections(text):
    starts = [(0, "Preamble")]
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return starts
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        line = min([node.lineno] + [d.lineno for d in node.decorator_list])
        position = offsets[line - 1]
        if position == 0:
            starts[0] = (0, node.name)
        elif position > starts[-1][0]:
            starts.append((position, node.name))
        if isinstance(node, ast.ClassDef):
            for child in node.body:
                if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                method_line = min([child.lineno] + [d.lineno for d in child.decorator_list])
                method_position = offsets[method_line - 1]
                if method_position > starts[-1][0]:
                    starts.append((method_position, f"{node.name}.{child.name}"))
    return starts


def _windows(start, end, size, overlap):
    pos = start
    while pos < end:
        stop = min(pos + size, end)
        yield pos, stop
        if stop == end:
            break
        pos = stop - overlap


def chunk_document(document, strategy, size=1800, overlap=180):
    _check(strategy, size, overlap)
    content = document.text
    sections = _markdown_sections(content) if document.file.lower().endswith(".md") else _python_sections(content)
    segments = [(start, sections[index+1][0] if index+1 < len(sections) else len(content), label)
                for index, (start, label) in enumerate(sections)]
    spans = []
    if strategy == "fixed":
        spans = [(a, b, False) for a, b in _windows(0, len(content), size, overlap)]
    else:
        for start, end, _ in segments:
            if start == end:
                continue
            spans.extend((a, b, end-start > size) for a, b in _windows(start, end, size, overlap))
    chunks = []
    for part, (start, end, fallback) in enumerate(spans):
        value = content[start:end]
        if not value or not value.strip():
            continue
        labels = []
        for left, right, label in segments:
            if right > start and left < end and label not in labels:
                labels.append(label)
        if not labels:
            labels = ["Document"]
        identity = json.dumps(["text-embedding-3-small", document.file, document.document_hash, strategy, size, overlap, start, end, value], ensure_ascii=False, separators=(",", ":"))
        chunks.append({"chunk_id": hashlib.sha256(identity.encode("utf-8")).hexdigest(),
                       "source": document.source, "file": document.file, "title": document.title,
                       "section": labels, "strategy": strategy, "document_hash": document.document_hash,
                       "start": start, "end": end,
                       "line_start": content.count("\n", 0, start) + 1,
                       "line_end": content.count("\n", 0, end) + (0 if value.endswith("\n") else 1),
                       "text": value, "fallback": fallback, "part": part})
    return chunks

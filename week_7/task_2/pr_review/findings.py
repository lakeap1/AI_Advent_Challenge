"""Mechanical finding validation against one caller-supplied Git snapshot.

No model or semantic assessment happens here. Hash checks detect inconsistent
records, but do not authenticate a snapshot against Git.
"""

import hashlib
import json
import re

from .git_context import _lf_lines


_CORE = ("schema_version", "base_revision", "head_revision", "merge_base",
         "changed_files", "sources", "coverage", "limits")
_HEX40_64 = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_SEVERITIES = {"critical", "high", "medium", "low"}
_KINDS = {"defect", "hypothesis"}
_TEXT_FIELDS = ("id", "title", "problem", "conditions", "consequence", "recommendation")


def _nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def _oid(value):
    return isinstance(value, str) and bool(_HEX40_64.fullmatch(value))


def snapshot_digest(snapshot):
    """Digest the fixed Task 1 core, excluding CLI annotations."""
    core = {key: snapshot[key] for key in _CORE}
    try:
        raw = json.dumps(core, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True, allow_nan=False).encode("utf-8")
    except RecursionError as exc:
        raise ValueError("snapshot_too_deep") from exc
    return hashlib.sha256(raw).hexdigest()


def _source_errors(snapshot):
    errors = []
    sources = snapshot.get("sources")
    if not isinstance(sources, list):
        return ["snapshot.sources:invalid"], {}
    by_id = {}
    for index, source in enumerate(sources):
        prefix = f"snapshot.sources[{index}]"
        if not isinstance(source, dict):
            errors.append(prefix + ":invalid")
            continue
        sid = source.get("source_id")
        revision = source.get("revision")
        path = source.get("path")
        blob_oid = source.get("blob_oid")
        content_hash = source.get("content_sha256")
        text = source.get("text")
        side = source.get("side")
        start = source.get("start_line")
        end = source.get("end_line")
        if not _nonempty(sid) or (isinstance(sid, str) and sid in by_id):
            errors.append(prefix + ".source_id:invalid_or_duplicate")
        if not _oid(revision) or not _oid(blob_oid) or not _nonempty(path):
            errors.append(prefix + ":invalid_identity")
            continue
        if side not in ("base", "head") or (
                side == "base" and revision != snapshot.get("merge_base")) or (
                side == "head" and revision != snapshot.get("head_revision")):
            errors.append(prefix + ".side:invalid")
        if not isinstance(text, str):
            errors.append(prefix + ".text:invalid")
            continue
        try:
            raw = text.encode("utf-8")
            identity_raw = (revision + "\x00" + path + "\x00" + blob_oid).encode(
                "utf-8", "surrogateescape")
        except (AttributeError, UnicodeError):
            errors.append(prefix + ":invalid_encoding")
            continue
        digest = hashlib.sha1 if isinstance(blob_oid, str) and len(blob_oid) == 40 else hashlib.sha256
        actual_oid = digest(b"blob " + str(len(raw)).encode("ascii") + b"\x00" + raw).hexdigest()
        if blob_oid != actual_oid or content_hash != hashlib.sha256(raw).hexdigest():
            errors.append(prefix + ":hash_mismatch")
        expected_sid = "git:" + hashlib.sha256(identity_raw).hexdigest()
        if sid != expected_sid:
            errors.append(prefix + ".source_id:mismatch")
        lines = _lf_lines(text)
        if (type(start) is not int or type(end) is not int or
                start != (1 if lines else 0) or end != len(lines)):
            errors.append(prefix + ":line_range_mismatch")
        if isinstance(sid, str) and sid not in by_id:
            by_id[sid] = source
    return errors, by_id


def validate_findings(payload, snapshot) -> dict:
    """Return mechanical validity, always withholding semantic acceptance."""
    errors = []
    result = {"valid": False, "errors": errors, "semantic_validation": "not_run",
              "review_accepted": False, "validation_scope": "supplied_snapshot"}
    if not isinstance(snapshot, dict):
        errors.append("snapshot:invalid")
        return result
    if snapshot.get("schema_version") != 1 or type(snapshot.get("schema_version")) is not int:
        errors.append("snapshot.schema_version:invalid")
    for field in ("base_revision", "head_revision", "merge_base"):
        if not _oid(snapshot.get(field)):
            errors.append(f"snapshot.{field}:invalid")
    if not isinstance(snapshot.get("changed_files"), list):
        errors.append("snapshot.changed_files:invalid")
    if not isinstance(snapshot.get("coverage"), dict):
        errors.append("snapshot.coverage:invalid")
    if not isinstance(snapshot.get("limits"), dict):
        errors.append("snapshot.limits:invalid")
    try:
        computed_digest = snapshot_digest(snapshot)
    except (KeyError, TypeError, ValueError, UnicodeError, OverflowError):
        computed_digest = None
        errors.append("snapshot:invalid_core")
    claimed_digest = snapshot.get("snapshot_sha256")
    if claimed_digest is not None and claimed_digest != computed_digest:
        errors.append("snapshot.snapshot_sha256:mismatch")
    source_errors, sources = _source_errors(snapshot)
    errors.extend(source_errors)
    if not isinstance(payload, dict):
        errors.append("report:invalid")
        return result
    if payload.get("schema_version") != 1 or type(payload.get("schema_version")) is not int:
        errors.append("report.schema_version:invalid")
    for field in ("base_revision", "head_revision"):
        if payload.get(field) != snapshot.get(field) or not _oid(payload.get(field)):
            errors.append(f"report.{field}:mismatch")
    report_digest = payload.get("snapshot_sha256")
    if report_digest is not None and (not isinstance(report_digest, str) or
                                      not _HEX64.fullmatch(report_digest) or
                                      report_digest != computed_digest):
        errors.append("report.snapshot_sha256:mismatch")
    findings = payload.get("findings")
    if not isinstance(findings, list):
        errors.append("report.findings:invalid")
        return result
    seen = set()
    for index, finding in enumerate(findings):
        prefix = f"report.findings[{index}]"
        if not isinstance(finding, dict):
            errors.append(prefix + ":invalid")
            continue
        for field in _TEXT_FIELDS:
            if not _nonempty(finding.get(field)):
                errors.append(prefix + f".{field}:invalid")
        fid = finding.get("id")
        if isinstance(fid, str):
            if fid in seen:
                errors.append(prefix + ".id:duplicate")
            seen.add(fid)
        severity = finding.get("severity")
        if not isinstance(severity, str) or severity not in _SEVERITIES:
            errors.append(prefix + ".severity:invalid")
        kind = finding.get("kind")
        if not isinstance(kind, str) or kind not in _KINDS:
            errors.append(prefix + ".kind:invalid")
        location = finding.get("location")
        location_sources = []
        if not isinstance(location, dict):
            errors.append(prefix + ".location:invalid")
        else:
            rev = location.get("revision")
            path = location.get("path")
            start = location.get("start_line")
            end = location.get("end_line")
            if type(start) is not int or type(end) is not int or start < 1 or end < start:
                errors.append(prefix + ".location:invalid_lines")
            else:
                location_sources = [s for s in sources.values()
                                    if s.get("revision") == rev and s.get("path") == path
                                    and type(s.get("end_line")) is int and end <= s["end_line"]]
                if not location_sources:
                    errors.append(prefix + ".location:not_in_source")
        evidence = finding.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            errors.append(prefix + ".evidence:invalid")
            continue
        anchored = False
        for eindex, item in enumerate(evidence):
            eprefix = f"{prefix}.evidence[{eindex}]"
            if not isinstance(item, dict):
                errors.append(eprefix + ":invalid")
                continue
            sid = item.get("source_id")
            quote = item.get("quote")
            source = sources.get(sid) if isinstance(sid, str) else None
            if source is None:
                errors.append(eprefix + ".source_id:unknown")
            if not _nonempty(quote):
                errors.append(eprefix + ".quote:invalid")
            elif source is not None and quote not in source["text"]:
                errors.append(eprefix + ".quote:not_in_source")
            if (source is not None and source in location_sources and _nonempty(quote)
                    and isinstance(location, dict) and type(location.get("start_line")) is int
                    and type(location.get("end_line")) is int):
                excerpt = "".join(_lf_lines(source["text"])[
                    location["start_line"] - 1:location["end_line"]])
                if quote in excerpt:
                    anchored = True
        if location_sources and not anchored:
            errors.append(prefix + ".location:no_quoted_evidence")
    result["valid"] = not errors
    return result

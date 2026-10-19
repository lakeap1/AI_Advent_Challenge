"""Evidence-limited RAG checks for changed corpus and retrieval contracts."""
import hashlib
import json
from pathlib import PurePosixPath


_ALGORITHM_RULES = {"ingestion_index", "chunking", "retrieval", "config", "answer_pipeline"}
_MAX_MANIFEST_BYTES = 131072
_MAX_MANIFEST_SOURCES = 200
_CONTROL = {"id": "usage-accounting-v1",
            "query": "Где пользователь видит токены и стоимость выполненных этапов?",
            "path": "week_7/task_1/docs/usage.md",
            "marker": "В панели «Учёт»"}


def select_retrieval_control(snapshot):
    """Select only a fixed, versioned question with pinned expected evidence."""
    head = snapshot.get("head_revision")
    for source in snapshot.get("sources", []):
        if (isinstance(source, dict) and source.get("side") == "head" and
                source.get("revision") == head and source.get("path") == _CONTROL["path"] and
                isinstance(source.get("text"), str) and _CONTROL["marker"] in source["text"]):
            return {**_CONTROL, "source_id": source["source_id"], "revision": head}
    return None


def _valid_manifest_path(value):
    if (not isinstance(value, str) or not value or len(value.encode("utf-8")) > 256 or
            "\\" in value or "\x00" in value or PurePosixPath(value).is_absolute() or
            len(value) > 1 and value[1] == ":" or
            any(part in ("", ".", "..") for part in value.split("/"))):
        return False
    return True


def _manifest_audit(snapshot):
    """Use pinned snapshot sources only; unavailable referenced files stay unresolved."""
    changed = [item for item in snapshot.get("changed_files", []) if isinstance(item, dict) and
               (item.get("path", "").endswith("/corpus/manifest.json") or
                isinstance(item.get("old_path"), str) and item["old_path"].endswith("/corpus/manifest.json"))]
    if not changed:
        return None
    path = next((item["path"] for item in changed if item.get("path", "").endswith("/corpus/manifest.json")),
                changed[0].get("old_path"))
    if any(item.get("status") == "D" for item in changed):
        return "failed", "manifest_deleted", []
    head = snapshot.get("head_revision")
    manifest = next((s for s in snapshot.get("sources", []) if isinstance(s, dict) and
                     s.get("path") == path and s.get("revision") == head and s.get("side") == "head"), None)
    if manifest is None:
        return "pending", "manifest_head_unavailable", ["manifest_head_unavailable"]
    text = manifest.get("text")
    if not isinstance(text, str) or len(text.encode("utf-8")) > _MAX_MANIFEST_BYTES:
        return "failed", "manifest_invalid_or_oversized", []
    try:
        document = json.loads(text)
    except (ValueError, TypeError):
        return "failed", "manifest_invalid_json", []
    entries = document.get("sources") if isinstance(document, dict) and document.get("version") == 1 else None
    if not isinstance(entries, list) or not entries or len(entries) > _MAX_MANIFEST_SOURCES:
        return "failed", "manifest_invalid_sources", []
    prefix = path.removesuffix("corpus/manifest.json")
    unresolved = []
    for entry in entries:
        relative = entry.get("path") if isinstance(entry, dict) else None
        if not _valid_manifest_path(relative):
            return "failed", "manifest_invalid_reference", []
        expected = prefix + relative
        pinned = next((s for s in snapshot.get("sources", []) if isinstance(s, dict) and
                       s.get("side") == "head" and s.get("revision") == head and s.get("path") == expected), None)
        if pinned is None:
            omissions = snapshot.get("coverage", {}).get("omissions", [])
            if any(isinstance(o, dict) and o.get("path") == expected and o.get("reason") == "missing" for o in omissions):
                return "failed", "manifest_reference_missing:" + expected, []
            unresolved.append("manifest_reference_unverified:" + expected)
        elif (not isinstance(pinned.get("text"), str) or
              hashlib.sha256(pinned["text"].encode("utf-8")).hexdigest() != pinned.get("content_sha256")):
            return "failed", "manifest_reference_digest_mismatch:" + expected, []
    if unresolved:
        return "pending", "manifest_references_unverified", unresolved[:20]
    return "passed", "manifest_references_pinned", []


def assess_snapshot_rag(snapshot, impact, candidates, metadata, *, control=None):
    """Describe locally checked properties and behavior that cannot be proved here."""
    if not isinstance(impact, dict) or not isinstance(snapshot, dict):
        raise ValueError("invalid RAG check inputs")
    if not impact.get("rag_checks_required"):
        return {"status": "skipped", "checks": [], "pending": []}
    sources = snapshot.get("sources", [])
    coverage = snapshot.get("coverage", {})
    requested = impact.get("checks", [])
    checks = []
    pending = []

    def add(name, status, detail):
        checks.append({"name": name, "status": status, "detail": detail})

    changed = snapshot.get("changed_files", [])
    missing_corpus = any(item.get("reason") == "missing" and
                         (item.get("path", "").endswith("/corpus/manifest.json") or "/corpus/" in item.get("path", ""))
                         for item in coverage.get("omissions", []) if isinstance(item, dict))
    missing_corpus |= any(item.get("status") == "D" and
                          (item.get("path", "").endswith("/corpus/manifest.json") or "/corpus/" in item.get("path", ""))
                          for item in changed if isinstance(item, dict))
    manifest_audit = _manifest_audit(snapshot)
    if manifest_audit is not None:
        pending.extend(manifest_audit[2])
    if "availability_provenance" in requested:
        identities = [s.get("source_id") for s in sources if isinstance(s, dict)]
        valid = (bool(sources) and coverage.get("complete") is True and not missing_corpus and
                 len(identities) == len(set(identities)) and all(identities))
        status = "passed" if valid else "failed"
        detail = "pinned_sources_present" if valid else "missing_or_incomplete_source"
        if valid and manifest_audit is not None:
            status, detail, _ = manifest_audit
        add("availability_provenance", status, detail)
    elif manifest_audit is not None:
        status, detail, _ = manifest_audit
        add("manifest_references", status, detail)
    if "chunks" in requested:
        valid = bool(sources) and all(
            isinstance(s, dict) and isinstance(s.get("text"), str) and
            hashlib.sha256(s["text"].encode("utf-8")).hexdigest() == s.get("content_sha256")
            for s in sources)
        add("chunks", "passed" if valid else "failed", "source_hashes_match" if valid else "source_hash_or_chunk_missing")
    if "vector_compatibility" in requested:
        valid = bool(metadata.get("model") and metadata.get("model_digest") and
                     type(metadata.get("dimensions")) is int and metadata["dimensions"] > 0)
        add("vector_compatibility", "passed" if valid else "failed", "model_digest_dimensions_recorded" if valid else "vector_metadata_missing")
    if "retrieval" in requested or "grounded_source_use" in requested:
        if control is None:
            add("retrieval", "pending", "trusted_control_unavailable")
            pending.append("retrieval_control_unavailable")
        else:
            valid = any(isinstance(c, dict) and c.get("source_id") == control["source_id"] and
                        c.get("revision") == control["revision"] and c.get("path") == control["path"] and
                        isinstance(c.get("text"), str) and control["marker"] in c["text"]
                        for c in candidates[:8])
            add("retrieval", "passed" if valid else "failed",
                "expected_pinned_chunk_found" if valid else "expected_pinned_chunk_not_found")
    if "grounded_source_use" in requested:
        add("grounded_source_use", "pending", "generated_answer_not_checked")
        pending.append("grounded_source_use_unverified")
    if "version_separation" in requested:
        keys = [(s.get("source_id"), s.get("revision"), s.get("path")) for s in sources if isinstance(s, dict)]
        valid = bool(keys) and len(keys) == len(set(keys))
        add("version_separation", "passed" if valid else "failed",
            "source_revision_keys_distinct_only" if valid else "source_revision_collision")
        if valid:
            pending.append("version_semantics_unverified")
    for item in impact.get("reasons", []):
        if isinstance(item, dict) and item.get("rule") in _ALGORITHM_RULES:
            label = f"pr_algorithm_behavior:{item.get('path')}"
            if label not in pending:
                pending.append(label)
    if coverage.get("complete") is not True:
        pending.append("snapshot_coverage_incomplete")
    status = "incomplete" if pending or any(c["status"] != "passed" for c in checks) else "passed"
    return {"status": status, "checks": checks, "pending": pending,
            "control": {key: control[key] for key in ("id", "query", "path", "marker", "revision", "source_id")}
            if control is not None else None}

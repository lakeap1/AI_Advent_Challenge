"""Conservative file-level plan for Day 31 RAG regression checks.

The plan selects properties to check. It neither runs RAG nor judges results.
"""

from pathlib import PurePosixPath


_ROOT = "week_7/task_1/"
_RULES = (
    ("corpus", ("availability_provenance", "chunks", "version_separation")),
    ("service_docs", ("availability_provenance", "grounded_source_use")),
    ("ingestion_index", ("availability_provenance", "chunks", "vector_compatibility")),
    ("chunking", ("chunks", "availability_provenance")),
    ("retrieval", ("vector_compatibility", "retrieval", "filtering_context")),
    ("config", ("vector_compatibility", "version_separation")),
    ("answer_pipeline", ("filtering_context", "grounded_source_use", "version_separation")),
    ("review_context", ("review_context",)),
)

_DOCS = {_ROOT + name for name in (
    "README.md", "docs/usage.md", "docs/architecture.md", "docs/api.md")}
_SCRIPTS = {_ROOT + name for name in (
    "scripts/ingest_corpus.py", "scripts/index_corpus.py")}
_GRAPHICS = {_ROOT + name for name in (
    "graphics/service.py", "graphics/context.py", "graphics/policies.py",
    "graphics/provider.py")}
_REVIEW = {"week_7/task_2/pr_review/git_context.py",
           "week_7/task_2/pr_review/impact.py"}


def _path(value):
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise ValueError("invalid_change_path")
    parts = value.split("/")
    if (PurePosixPath(value).is_absolute() or len(value) > 1 and value[1] == ":"
            or any(part in ("", ".", "..") for part in parts)
            or any(part.casefold() == ".git" for part in parts)):
        raise ValueError("invalid_change_path")
    return value


def _matching_rules(path):
    if path.startswith(_ROOT + "corpus/"):
        return ("corpus",)
    if path in _DOCS:
        return ("service_docs",)
    if path in _SCRIPTS:
        return ("ingestion_index",)
    if path == _ROOT + "graphics/chunking.py":
        return ("chunking",)
    if path in (_ROOT + "graphics/retrieval.py", _ROOT + "graphics/hybrid.py"):
        return ("retrieval",)
    if path == _ROOT + "graphics/config.py":
        return ("config",)
    if path in _GRAPHICS or path == _ROOT + "graphics/validation.py":
        return ("answer_pipeline",)
    if path in _REVIEW:
        return ("review_context",)
    return ()


def assess_rag_impact(changed_files) -> dict:
    """Select stable review properties from Task 1 changed-file records."""
    if not isinstance(changed_files, (list, tuple)):
        raise ValueError("invalid_changed_files")
    reasons = []
    checks = []
    seen_reasons = set()
    seen_checks = set()
    properties = dict(_RULES)
    for change in changed_files:
        if not isinstance(change, dict):
            raise ValueError("invalid_change")
        status = change.get("status")
        if status not in ("A", "M", "D", "R", "C", "T"):
            raise ValueError("invalid_change_status")
        path = _path(change.get("path"))
        old = change.get("old_path")
        if status in ("R", "C"):
            old = _path(old)
        elif old is not None:
            raise ValueError("invalid_old_path")
        for candidate in (old, path) if old else (path,):
            for rule in _matching_rules(candidate):
                key = (candidate, rule)
                if key not in seen_reasons:
                    reasons.append({"path": candidate, "rule": rule})
                    seen_reasons.add(key)
                for check in properties[rule]:
                    if check not in seen_checks:
                        checks.append(check)
                        seen_checks.add(check)
    return {"analysis": "path_rules",
            "rag_checks_required": any(check != "review_context" for check in checks),
            "reasons": reasons, "checks": checks}

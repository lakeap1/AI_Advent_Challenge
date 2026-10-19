"""Bounded conversation context without mutating saved history."""

import json


def estimate_tokens(text: str) -> int:
    """UTF-8 bytes are a conservative upper bound for text token counts."""
    return len(text.encode("utf-8"))


def trim_history(messages: list[dict], budget: int) -> list[dict]:
    if budget < 0:
        raise ValueError("Invalid history budget")
    kept = []
    used = 0
    for message in reversed(messages):
        size = estimate_tokens(str(message.get("content", "")))
        if used + size > budget:
            break
        kept.append(message)
        used += size
    return list(reversed(kept))


def _size(value) -> int:
    return estimate_tokens(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


class ExplicitBudgetError(ValueError):
    """A proposed explicit configuration cannot fit without losing user facts."""


def validate_explicit(branch: dict, profile: dict, memories: list[dict], budget: int) -> int:
    explicit = {"conditions": branch.get("conditions") or {},
                "working_state": branch.get("working_state") or {},
                "profile": profile or {}, "memories": memories or []}
    size = _size(explicit)
    if size > budget:
        raise ExplicitBudgetError(
            f"Превышен общий бюджет памяти, профиля и условий ({budget} байт UTF-8). "
            "Сократите запись, профиль или условия либо удалите ненужные сведения; "
            "прежние настройки сохранены."
        )
    return size


def build_context(branch: dict, profile: dict, memories: list[dict], settings) -> dict:
    """Build JSON-safe context. Explicit user facts are never silently clipped."""
    conditions = branch.get("conditions") or {}
    working = branch.get("working_state") or {}
    explicit_bytes = validate_explicit(branch, profile, memories, settings.explicit_budget)
    summary = branch.get("summary") or ""
    if estimate_tokens(summary) > settings.summary_budget:
        raise ValueError("Сводка превышает бюджет контекста")
    messages = branch.get("messages") or []
    boundary = branch.get("summary_through", 0)
    if not isinstance(boundary, int) or not 0 <= boundary <= len(messages):
        raise ValueError("Некорректная граница сводки")
    recent = trim_history(messages[boundary:], settings.history_budget)
    if len(recent) != len(messages[boundary:]):
        raise ValueError("Несжатая история не помещается в бюджет контекста")
    total = explicit_bytes + estimate_tokens(summary) + sum(
        _size({"role": m.get("role"), "content": m.get("content")}) for m in recent)
    if total > settings.input_budget - settings.sources_budget - settings.tools_budget:
        raise ValueError("Контекст превышает операционный бюджет")
    return {"summary": summary, "recent": recent,
            "working_state": working, "conditions": conditions,
            "profile": profile or {}, "memories": memories or [],
            "diagnostics": {"explicit_bytes": explicit_bytes, "context_bytes": total,
                            "history_messages": len(recent),
                            "omitted_history_messages": len(messages[boundary:]) - len(recent),
                            "summary_through": boundary}}

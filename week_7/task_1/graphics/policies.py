"""Explicit input and output policies for the reply engine."""

import re


PLAIN_TEXT_RULE = (
    "Поле answer всегда содержит обычный русский текст с абзацами: без Markdown, "
    "заголовков #, таблиц, обратных кавычек и ограждений кода. "
    "Исключение только при явной просьбе пользователя о другом формате. "
    "Машинный ответ целиком остаётся JSON. Не придумывай URL или идентификаторы источников."
)


def check_input(text, *, max_bytes=10000):
    if not isinstance(text, str) or not text.strip():
        return {"ok": False, "code": "empty_input"}
    if len(text) > 12000:
        return {"ok": False, "code": "input_too_long"}
    if len(text.strip().encode("utf-8")) > max_bytes:
        return {"ok": False, "code": "input_history_budget"}
    return {"ok": True, "code": "accepted"}


def wants_markdown(text):
    return bool(re.search(r"\b(markdown|md[- ]формат|формат markdown)\b", text, re.I))


def check_answer(answer, source_ids, allowed_ids, question, *, allowed_urls=(), max_chars=8000):
    if not isinstance(answer, str) or not answer.strip() or len(answer) > max_chars:
        return {"ok": False, "code": "answer_empty_or_long"}
    if not isinstance(source_ids, list) or not source_ids or len(source_ids) != len(set(source_ids)):
        return {"ok": False, "code": "source_ids_missing"}
    if any(not isinstance(item, str) or item not in allowed_ids for item in source_ids):
        return {"ok": False, "code": "source_ids_invalid"}
    if any(url.rstrip(".,);]") not in allowed_urls
           for url in re.findall(r"https?://[^\s<>]+", answer)):
        return {"ok": False, "code": "unknown_url"}
    if not wants_markdown(question) and re.search(r"(?m)^\s*(?:#{1,6}\s|[-*+]\s|\d+\.\s|\|)|```|(?<![\w)\]])\*\*(?=\S)[^*\n]*\S\*\*(?![\w(\[])|`", answer):
        return {"ok": False, "code": "markdown_disallowed"}
    return {"ok": True, "code": "accepted"}

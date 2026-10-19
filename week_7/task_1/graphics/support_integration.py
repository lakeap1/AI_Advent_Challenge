"""Explicit support grammar, trusted MCP binding, and bounded evidence."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path


_ID = r"[0-9a-f]{32}"
_MUTATIONS = {"support_ticket_create", "support_ticket_append", "support_ticket_close",
              "support_profile_update", "support_memory_set", "support_memory_delete"}


def parse_support(text: str) -> dict:
    if not isinstance(text, str) or not text.startswith("/support") or (
            len(text) > 8 and text[8] not in " \t\r\n"):
        raise ValueError("Формат: /support вопрос или /support /status")
    first, _, rest = text.partition("\n")
    command = first[8:].strip()
    question = rest.strip()
    if not command:
        raise ValueError("После /support нужен вопрос или команда")
    if not command.startswith("/"):
        return {"tool": None, "args": {}, "question": text[8:].strip(), "mutation": False}
    if command in {"/me", "/status"}:
        tool = "support_user_get" if command == "/me" else "support_status"
        args = {}
    elif command == "/ticket list":
        tool, args = "support_ticket_list", {}
    elif re.fullmatch(rf"/ticket get {_ID}", command):
        tool, args = "support_ticket_get", {"ticket_id": command.rsplit(" ", 1)[1]}
    elif re.fullmatch(rf"/ticket close {_ID}", command):
        tool, args = "support_ticket_close", {"ticket_id": command.rsplit(" ", 1)[1]}
    elif command.startswith("/ticket create "):
        title, separator, description = command[len("/ticket create "):].partition(" | ")
        if (not separator or not title.strip() or not description.strip() or
                len(title.strip()) > 160 or len(description.strip()) > 8000):
            raise ValueError("Формат: /support /ticket create НАЗВАНИЕ | СООБЩЕНИЕ")
        tool, args = "support_ticket_create", {"title": title.strip(), "description": description.strip()}
    elif command.startswith("/ticket append "):
        match = re.fullmatch(rf"/ticket append ({_ID}) \| (.+)", command)
        if not match or not match.group(2).strip() or len(match.group(2).strip()) > 8000:
            raise ValueError("Формат: /support /ticket append ID | СООБЩЕНИЕ")
        tool, args = "support_ticket_append", {"ticket_id": match.group(1), "message": match.group(2).strip()}
    elif command.startswith("/profile "):
        changes = {}
        for entry in command[len("/profile "):].split(";"):
            match = re.fullmatch(r"\s*(role|level|goal|depth)=(.*)", entry)
            if (not match or match.group(1) in changes or len(match.group(2).strip()) > 500 or
                    (match.group(2) and not match.group(2).strip())):
                raise ValueError("Формат: /support /profile role=QA; level=beginner; depth=short")
            changes[match.group(1)] = match.group(2).strip()
        tool, args = "support_profile_update", {"changes": changes}
    elif command.startswith("/remember "):
        key, separator, value = command[len("/remember "):].partition(":")
        if (not separator or not key.strip() or not value.strip() or
                len(key.strip()) > 500 or len(value.strip()) > 500):
            raise ValueError("Формат: /support /remember ключ: значение")
        tool, args = "support_memory_set", {"key": key.strip(), "value": value.strip()}
    elif command.startswith("/forget "):
        key = command[len("/forget "):].strip()
        if not key or len(key) > 500:
            raise ValueError("Формат: /support /forget ключ")
        tool, args = "support_memory_delete", {"key": key}
    else:
        raise ValueError("Неизвестная команда поддержки")
    return {"tool": tool, "args": args, "question": question, "mutation": tool in _MUTATIONS}


def operation_key(user_id: int, branch_id: str, request_id: str) -> str:
    return hashlib.sha256(f"{user_id}\0{branch_id}\0{request_id}".encode("utf-8")).hexdigest()


def compact_evidence(data, *, limit: int = 1500) -> dict:
    """A labelled excerpt; never claim the preview is the whole tool result."""
    encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    prefix = encoded[:max(0, limit - 100)].decode("utf-8", errors="ignore")
    result = {"kind": "tool_result_excerpt", "preview": prefix,
              "omitted": len(encoded) > len(prefix.encode("utf-8"))}
    while len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > limit:
        prefix = prefix[:-16]
        result["preview"] = prefix
        result["omitted"] = True
    return result


def compact_ticket_action(data: dict, *, limit: int = 1400) -> dict:
    """Keep committed action facts before any bounded historical context."""
    ticket = data["ticket"]
    messages = ticket.get("messages", [])
    latest = messages[-1] if messages else None
    history = messages[:-1] if messages else []
    result = {
        "kind": "ticket_action_projection",
        "receipt": data["receipt"],
        "ticket": {"id": ticket["id"], "status": ticket["status"], "title": ticket["title"]},
        "latest_message": ({"id": latest["id"], "created_at": latest["created_at"],
                            "body_excerpt": "", "omitted": bool(latest["body"])} if latest else None),
        "history_excerpt": {"older_message_count": len(history), "preview": "", "omitted": bool(history)},
        "omitted": True,
    }

    def size() -> int:
        return len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    # The title is helpful but not part of the mandatory receipt / ID / status contract.
    if size() > limit:
        del result["ticket"]["title"]
    if size() > limit:
        raise ValueError("Подтверждение обращения превышает бюджет контекста")

    def fit_excerpt(target: dict, field: str, value: str, cap: int | None = None) -> None:
        if cap is not None:
            value = value.encode("utf-8")[:cap].decode("utf-8", errors="ignore")
        low, high = 0, len(value)
        while low < high:
            middle = (low + high + 1) // 2
            target[field] = value[:middle]
            if size() <= limit:
                low = middle
            else:
                high = middle - 1
        target[field] = value[:low]

    if latest:
        fit_excerpt(result["latest_message"], "body_excerpt", latest["body"], cap=500)
        result["latest_message"]["omitted"] = (
            result["latest_message"]["body_excerpt"] != latest["body"])
    if history:
        encoded_history = json.dumps(history, ensure_ascii=False, separators=(",", ":"))
        fit_excerpt(result["history_excerpt"], "preview", encoded_history)
        result["history_excerpt"]["omitted"] = (
            result["history_excerpt"]["preview"] != encoded_history)
    # Changing an omitted flag from true to false can add one UTF-8 byte.
    while size() > limit:
        if result["history_excerpt"]["preview"]:
            result["history_excerpt"]["preview"] = result["history_excerpt"]["preview"][:-1]
            result["history_excerpt"]["omitted"] = True
        elif "title" in result["ticket"]:
            del result["ticket"]["title"]
        elif latest and result["latest_message"]["body_excerpt"]:
            result["latest_message"]["body_excerpt"] = result["latest_message"]["body_excerpt"][:-1]
            result["latest_message"]["omitted"] = True
        else:
            raise ValueError("Подтверждение обращения превышает бюджет контекста")
    return result


def support_control_notice(tool: str, data, confirmation: str | None = None) -> str:
    """Explicit own-data presentation; raw MCP envelopes never enter HTTP history."""
    if confirmation is not None:
        return confirmation
    if tool == "support_status":
        fields = ("storage", "main_storage", "main_service", "llm", "rag")
        parts = [f"{field}: {data[field].get('status', 'unknown')}" for field in fields
                 if isinstance(data, dict) and isinstance(data.get(field), dict)]
        message = "Наблюдаемый статус: " + "; ".join(parts)
    elif tool == "support_user_get":
        profile = data.get("profile", {}) if isinstance(data, dict) else {}
        memories = data.get("memories", []) if isinstance(data, dict) else []
        profile_text = "; ".join(f"{key}={profile[key]}" for key in ("role", "level", "goal", "depth")
                                 if isinstance(profile, dict) and key in profile)
        memory_text = "; ".join(f"{item['key']}: {item['value']}" for item in memories
                                if isinstance(item, dict) and "key" in item and "value" in item)
        message = "Ваш профиль: " + (profile_text or "пуст") + ". Сохранённые сведения: " + (memory_text or "нет")
    elif tool == "support_ticket_list":
        tickets = data if isinstance(data, list) else []
        message = "Ваши обращения: " + ("; ".join(
            f"{item['id']} — {item['title']} ({item['status']})" for item in tickets
            if isinstance(item, dict) and {"id", "title", "status"} <= item.keys()) or "нет")
    elif tool == "support_ticket_get" and isinstance(data, dict):
        messages = data.get("messages", [])
        body = "; ".join(str(item["body"]) for item in messages
                         if isinstance(item, dict) and "body" in item)
        message = (f"Обращение {data.get('id', '')}: {data.get('title', '')}; "
                   f"статус {data.get('status', 'unknown')}. Сообщения: {body}")
    else:
        message = "Данные поддержки получены."
    encoded = message.encode("utf-8")
    if len(encoded) <= 1600:
        return message
    return encoded[:1530].decode("utf-8", errors="ignore") + " … Часть данных опущена."


def support_client(settings, user_id: int, branch_id: str, turn_id: str):
    """Load the one explicitly configured sibling package; identity comes from auth."""
    support_root = Path(settings.app_root).resolve().parent / "task_3"
    if not (support_root / "support" / "mcp_client.py").is_file():
        raise RuntimeError("Модуль поддержки недоступен")
    if str(support_root) not in sys.path:
        sys.path.insert(0, str(support_root))
    from support.mcp_client import SupportMCPClient
    binding = {"app_root": str(Path(settings.app_root).resolve()),
               "db_path": str(Path(settings.db_path).resolve()),
               "branch_id": branch_id, "turn_id": turn_id,
               "explicit_budget": settings.explicit_budget}
    return SupportMCPClient(Path(settings.data_dir) / "support.sqlite3", str(user_id),
                            service_binding=binding)

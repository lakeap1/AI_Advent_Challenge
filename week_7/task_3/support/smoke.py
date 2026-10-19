"""Synthetic end-to-end support smoke over real MCP stdio processes."""

from __future__ import annotations

from pathlib import Path

from .mcp_client import SupportMCPClient
from .tools import TOOL_SCHEMAS


def _require(condition: bool, description: str) -> None:
    if not condition:
        raise RuntimeError("Support smoke failed: " + description)


def run_smoke(db_path: str | Path) -> dict:
    target = Path(db_path).absolute()
    if target.exists() or target.is_symlink():
        raise FileExistsError("Smoke DB path already exists")
    if not target.parent.is_dir():
        raise FileNotFoundError("Smoke DB parent directory does not exist")
    create_args = {"title": "Synthetic access question", "description": "Synthetic support request only", "request_key": "smoke-create"}
    with SupportMCPClient(target, "synthetic_alice") as alice:
        listed = alice.list_tools()
        _require(set(listed) == set(TOOL_SCHEMAS), "tool discovery")
        created = alice.call("support_ticket_create", create_args)
        _require(created["ok"], "create")
        ticket_id = created["data"]["ticket"]["id"]
        appended = alice.call("support_ticket_append", {"ticket_id": ticket_id, "message": "Synthetic follow-up", "request_key": "smoke-append"})
        _require(appended["ok"], "append")
        fetched = alice.call("support_ticket_get", {"ticket_id": ticket_id})
        _require(fetched["ok"] and len(fetched["data"]["messages"]) == 2, "get")
        first_evidence = list(alice.evidence)
    with SupportMCPClient(target, "synthetic_alice") as restarted:
        replay = restarted.call("support_ticket_create", create_args)
        _require(replay == created, "receipt replay after restart")
        closed = restarted.call("support_ticket_close", {"ticket_id": ticket_id, "request_key": "smoke-close"})
        _require(closed["ok"] and closed["data"]["ticket"]["status"] == "closed_by_user", "close")
        status = restarted.call("support_status", {})
        _require(status["ok"] and status["data"]["storage"]["status"] == "ok", "storage status")
        _require(all(status["data"][part]["status"] == "unknown" for part in ("main_service", "llm", "rag")), "unknown integrations")
        _require(status["accounting"] == {"llm_calls": 0, "usage": None, "cost_usd": None, "reason": "no_request"}, "accounting")
        restarted_evidence = list(restarted.evidence)
    with SupportMCPClient(target, "synthetic_bob") as bob:
        denied = bob.call("support_ticket_get", {"ticket_id": ticket_id})
        _require(not denied["ok"] and denied["error"]["code"] == "not_found", "owner denial")
        empty = bob.call("support_ticket_list", {})
        _require(empty["ok"] and empty["data"] == [], "owner list")
        bob_evidence = list(bob.evidence)
    events = first_evidence + restarted_evidence + bob_evidence
    _require(all(any(item["event"] == event for item in events) for event in ("initialize", "list_tools", "call_tool")), "protocol evidence")
    return {
        "result": "integration_required",
        "protocol": {"scope": "completed_sdk_results", "initialize": sum(item["event"] == "initialize" for item in events), "list_tools": sum(item["event"] == "list_tools" for item in events), "call_tool": sum(item["event"] == "call_tool" for item in events), "call_tool_errors": sum(item["event"] == "call_tool" and item["is_error"] for item in events)},
        "checks": {"persistence": True, "replay": True, "append_get_close": True, "owner_denial": True, "storage_read": True},
        "accounting": status["accounting"],
        "not_run": ["common_chat", "profile_memory_mcp", "web_auth", "fsm_validator", "rag", "glm", "live_site", "video"],
    }

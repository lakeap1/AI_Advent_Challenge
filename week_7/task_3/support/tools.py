"""Strict support tool contracts and the actor-bound gateway."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from .domain import Actor, SupportError
from .tickets import TicketStore


def _object(properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


# Exactly the whitespace code points removed by Python str.strip(). Avoid \s here:
# ECMA-262 and Python disagree on some whitespace characters.
_TRIM_WHITESPACE = r"\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000"


def _text(maximum: int) -> dict:
    whitespace = f"[{_TRIM_WHITESPACE}]"
    nonwhitespace = f"[^{_TRIM_WHITESPACE}]"
    # The first and last nonwhitespace code points bound the stripped core.
    # Surrounding whitespace is unbounded and does not count toward maximum.
    pattern = f"^{whitespace}*{nonwhitespace}(?:[\\s\\S]{{0,{maximum - 2}}}{nonwhitespace})?{whitespace}*$"
    return {
        "type": "string",
        "minLength": 1,
        "pattern": pattern,
        "description": f"1 to {maximum} characters after stripping surrounding whitespace; original input is retained for idempotency",
    }


_KEY = {"type": "string", "pattern": r"^[A-Za-z0-9_.:-]{1,128}(?![\s\S])"}
_ID = {"type": "string", "pattern": r"^[0-9a-f]{32}(?![\s\S])"}
TOOL_SCHEMAS = {
    "support_ticket_create": _object({"title": _text(160), "description": _text(8000), "request_key": _KEY}, ["title", "description", "request_key"]),
    "support_ticket_get": _object({"ticket_id": _ID}, ["ticket_id"]),
    "support_ticket_list": _object({"limit": {"type": "integer", "minimum": 1, "maximum": 50}}, []),
    "support_ticket_append": _object({"ticket_id": _ID, "message": _text(8000), "request_key": _KEY}, ["ticket_id", "message", "request_key"]),
    "support_ticket_close": _object({"ticket_id": _ID, "request_key": _KEY}, ["ticket_id", "request_key"]),
    "support_status": _object({}, []),
}

_FIELD_500 = {**_text(500), "maxLength": 500}
_PROFILE_FIELD = {"anyOf": [{"const": ""}, _FIELD_500]}
INTEGRATED_TOOL_SCHEMAS = {
    "support_ticket_receipt": _object({
        "tool": {"enum": ["support_ticket_create", "support_ticket_append", "support_ticket_close"]},
        "arguments": {"type": "object"},
    }, ["tool", "arguments"]) | {"oneOf": [
        {"properties": {"tool": {"const": name}, "arguments": TOOL_SCHEMAS[name]}}
        for name in ("support_ticket_create", "support_ticket_append", "support_ticket_close")]},
    "support_user_get": _object({}, []),
    "support_profile_update": _object({
        "changes": {"type": "object", "properties": {
            field: _PROFILE_FIELD for field in ("role", "level", "goal", "depth")},
            "minProperties": 1, "additionalProperties": False},
        "request_key": _KEY,
    }, ["changes", "request_key"]),
    "support_memory_set": _object({"key": _FIELD_500, "value": _FIELD_500,
                                   "request_key": _KEY}, ["key", "value", "request_key"]),
    "support_memory_delete": _object({"memory_id": _ID, "request_key": _KEY},
                                     ["memory_id", "request_key"]),
    "support_diagnostics": _object({}, []),
}

ACCOUNTING = {"llm_calls": 0, "usage": None, "cost_usd": None, "reason": "no_request"}


def envelope(data=None, error: SupportError | None = None, *, integrated: bool = False) -> dict:
    base = {"ok": error is None, "accounting": ACCOUNTING.copy(),
            "integration_status": "integrated" if integrated else "integration_required"}
    if error is None:
        base["data"] = data
    else:
        base["error"] = {"code": error.code, "message": error.message}
    return base


def validate_arguments(name: str, arguments: dict, schemas: dict | None = None) -> None:
    schemas = TOOL_SCHEMAS if schemas is None else schemas
    if name not in schemas:
        raise SupportError("invalid_argument", "Unknown support tool")
    if not isinstance(arguments, dict):
        raise SupportError("invalid_argument", "Tool arguments must be an object")
    schema = schemas[name]
    if set(arguments) - set(schema["properties"]):
        raise SupportError("invalid_argument", "Unexpected tool argument")
    if set(schema["required"]) - set(arguments):
        raise SupportError("invalid_argument", "Missing tool argument")
    import jsonschema
    try:
        jsonschema.validate(arguments, schema)
    except jsonschema.ValidationError as error:
        raise SupportError("invalid_argument", "Invalid tool argument") from error
    if name == "support_ticket_receipt":
        validate_arguments(arguments["tool"], arguments["arguments"])
    limits = {
        "support_ticket_create": {"title": 160, "description": 8000},
        "support_ticket_append": {"message": 8000},
    }
    for field, maximum in limits.get(name, {}).items():
        value = arguments[field]
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as error:
            raise SupportError("invalid_argument", "Invalid tool argument") from error
        if not 1 <= len(value.strip()) <= maximum:
            raise SupportError("invalid_argument", "Invalid tool argument")
    if name == "support_profile_update":
        values = arguments["changes"].values()
    elif name == "support_memory_set":
        values = (arguments["key"], arguments["value"])
    else:
        values = ()
    for value in values:
        if name == "support_profile_update" and value == "":
            continue
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as error:
            raise SupportError("invalid_argument", "Invalid tool argument") from error
        if not 1 <= len(value.strip()) <= 500:
            raise SupportError("invalid_argument", "Invalid tool argument")


class ToolGateway:
    def __init__(self, store: TicketStore, actor: Actor, service_bridge=None):
        self.store = store
        self.actor = actor
        self.service_bridge = service_bridge
        self.schemas = TOOL_SCHEMAS if service_bridge is None else TOOL_SCHEMAS | INTEGRATED_TOOL_SCHEMAS

    def call(self, name: str, arguments: dict) -> dict:
        try:
            validate_arguments(name, arguments, self.schemas)
            if self.service_bridge is not None:
                self.service_bridge._check_owner()
            ticket_arguments = arguments
            ticket_mutation = name in {"support_ticket_create", "support_ticket_append",
                                       "support_ticket_close"}
            if self.service_bridge is not None and ticket_mutation:
                ticket_arguments = dict(arguments)
                namespace = self.service_bridge.branch_id + "\0" + arguments["request_key"]
                ticket_arguments["request_key"] = hashlib.sha256(namespace.encode("utf-8")).hexdigest()
            if name == "support_ticket_receipt":
                original = arguments["arguments"]
                actions = self.service_bridge.store.support_actions(
                    self.service_bridge.user_id, self.service_bridge.branch_id,
                    self.service_bridge.turn_id)
                if not any(action["request_key"] == original["request_key"] and
                           action["tool"] == arguments["tool"] and
                           action["status"] in {"unknown", "confirmed"} for action in actions):
                    raise SupportError("not_found", "Original owned operation is unavailable")
                namespace = self.service_bridge.branch_id + "\0" + original["request_key"]
                lookup = {**original, "request_key": hashlib.sha256(namespace.encode("utf-8")).hexdigest()}
                receipt = self.store.lookup_receipt(
                    self.actor, arguments["tool"].removeprefix("support_ticket_"), lookup)
                result = {"receipt": ({**receipt, "request_key": original["request_key"]}
                                      if receipt is not None else None)}
            elif name == "support_ticket_create":
                result = self.store.create(self.actor, **ticket_arguments)
            elif name == "support_ticket_get":
                result = self.store.get(self.actor, **arguments)
            elif name == "support_ticket_list":
                result = self.store.list(self.actor, **arguments)
            elif name == "support_ticket_append":
                result = self.store.append(self.actor, **ticket_arguments)
            elif name == "support_ticket_close":
                result = self.store.close(self.actor, **ticket_arguments)
            elif name == "support_status":
                try:
                    health = self.store.health()
                except SupportError as error:
                    if error.code != "storage_unavailable":
                        raise
                    health = {"storage": "unavailable", "checked_at": datetime.now(
                        timezone.utc).isoformat(timespec="seconds")}
                result = {
                    "storage": {"status": health["storage"], "checked_at": health["checked_at"]},
                    "main_service": {"status": "unknown", "integration_status": "integration_required"},
                    "llm": {"status": "unknown", "integration_status": "integration_required"},
                    "rag": {"status": "unknown", "integration_status": "integration_required"},
                }
                if self.service_bridge is not None:
                    facts = self.service_bridge.diagnostics()
                    result["main_service"] = {"status": "unknown"}
                    result["main_storage"] = facts["main_storage"]
                    result["llm"] = facts["llm"]
                    result["rag"] = facts["rag"]
            else:
                result = self.service_bridge.call(name, arguments)
            if self.service_bridge is not None and ticket_mutation:
                result = {**result, "receipt": {**result["receipt"],
                          "request_key": arguments["request_key"]}}
            return envelope(result, integrated=self.service_bridge is not None)
        except SupportError as error:
            return envelope(error=error, integrated=self.service_bridge is not None)
        except PermissionError:
            return envelope(error=SupportError("not_found", "Owned context is unavailable"),
                            integrated=self.service_bridge is not None)
        except Exception:
            return envelope(error=SupportError("storage_unavailable", "Support tool is unavailable"),
                            integrated=self.service_bridge is not None)

"""Explicit external HTTPS support check against two pre-created synthetic users."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import http.cookiejar
import json
import os
from pathlib import Path
import secrets
import ssl
import urllib.error
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def private_new(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def private_update(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    try:
        private_new(temporary, value)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class Site:
    def __init__(self, base_url: str):
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.path not in {"", "/"}:
            raise ValueError("--base-url must be an HTTPS origin")
        self.origin = base_url.rstrip("/")
        self.csrf = ""
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
            NoRedirect(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()))

    def call(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("Unsafe API path")
        headers = {"Accept": "application/json"}
        payload = None
        if method in {"POST", "PATCH", "DELETE"}:
            headers.update({"Origin": self.origin, "X-CSRF-Token": self.csrf,
                            "Content-Type": "application/json"})
            payload = json.dumps(body if body is not None else {}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(self.origin + path, payload, headers, method=method)
        try:
            response = self.opener.open(request, timeout=90)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            raw = response.read(256_000)
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise RuntimeError("Unexpected API object")
            return response.status, result

    def login(self, actor: dict) -> None:
        status, start = self.call("GET", "/api/csrf")
        if status != 200:
            raise RuntimeError("CSRF preparation failed")
        self.csrf = start["csrf_token"]
        status, signed = self.call("POST", "/api/login", actor)
        if status != 200:
            raise RuntimeError(f"Synthetic account login failed ({status})")
        self.csrf = signed["csrf_token"]

    def required(self, method: str, path: str, body: dict | None = None) -> dict:
        status, result = self.call(method, path, body)
        if status not in {200, 201}:
            raise RuntimeError(f"{method} {path} failed ({status})")
        return result

    def ask(self, branch: str, question: str, request_id: str | None = None) -> dict:
        return self.required("POST", f"/api/branches/{branch}/ask",
                             {"question": question, "request_id": request_id or secrets.token_hex(16)})


def observed_control(site: Site, branch: str, question: str, request_id: str,
                     journal: dict, field: str, path: Path) -> dict:
    entry = {"question": question, "request_id": request_id, "status": "intent"}
    journal[field].append(entry)
    private_update(path, journal)
    try:
        result = site.ask(branch, question, request_id=request_id)
    except Exception as error:
        entry.update(status="unknown", error=type(error).__name__)
        private_update(path, journal)
        raise
    entry.update(status=result.get("status"), message=result.get("message"),
                 confirmation=result.get("support_confirmation"),
                 answer=result.get("answer"), usage=result.get("usage"))
    private_update(path, journal)
    if result.get("status") != "support_control":
        raise RuntimeError("Expected support_control for deterministic operation")
    return result


def access(path: Path) -> dict:
    if path.is_symlink():
        raise ValueError("Linked access file is forbidden")
    if os.name != "nt" and path.stat().st_mode & 0o077:
        raise ValueError("Access file must be private (0600)")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or set(value) != {"primary", "other"}:
        raise ValueError("Private access file needs primary and other actors")
    for actor in value.values():
        if not isinstance(actor, dict) or set(actor) != {"username", "password"} or any(
            not isinstance(item, str) or not item for item in actor.values()
        ):
            raise ValueError("Invalid private actor credentials")
    if value["primary"] == value["other"]:
        raise ValueError("Synthetic actors must differ")
    return value


def prepare(site: Site, actors: dict, state_path: Path) -> dict:
    if state_path.exists():
        raise FileExistsError(state_path)
    site.login(actors["primary"])
    chat = site.required("POST", "/api/chats", {"title": "День 33 — синтетическая проверка"})
    branch = chat["branch_id"]
    controls = [
        "/support /profile role=QA; level=beginner; goal=понять Unity URP; depth=short",
        "/support /remember day33-example: использовать пример шероховатого шара",
        "/support /remember day33-example: использовать пример матового шара",
        "/support /remember day33-temp: удалить после проверки",
        "/support /forget day33-temp",
        "/support /me", "/support /status",
        "/support /ticket create Синтетическая жалоба на источник | Синтетический случай: не удалось открыть источник FAQ; причина пока неизвестна",
    ]
    state = {"format": 1, "prepared_at": datetime.now(timezone.utc).isoformat(),
             "origin": site.origin, "chat_id": chat["id"], "branch_id": branch,
             "ticket_id": None, "complete": False,
             "request_ids": [secrets.token_hex(16) for _ in range(len(controls) + 2)],
             "observed": []}
    # Intent IDs are durable before any support mutation; a timeout is never retried here.
    private_new(state_path, state)
    ticket_id = None
    for index, question in enumerate(controls):
        result = observed_control(site, branch, question, state["request_ids"][index],
                                  state, "observed", state_path)
        receipt = (result.get("support_confirmation") or {}).get("receipt") or {}
        if receipt.get("operation") == "create":
            ticket_id = receipt.get("ticket_id")
    if not ticket_id:
        raise RuntimeError("No confirmed ticket ID")
    state["ticket_id"] = ticket_id
    private_update(state_path, state)
    for index, question in enumerate((f"/support /ticket append {ticket_id} | Уточнение: вопрос касается панели источников сервиса",
                                      f"/support /ticket get {ticket_id}"), start=len(controls)):
        observed_control(site, branch, question, state["request_ids"][index],
                         state, "observed", state_path)
    state["complete"] = True
    private_update(state_path, state)
    return {"phase": "prepare", "chat_id": chat["id"], "branch_id": branch,
            "ticket_id": ticket_id, "controls": len(state["observed"])}


def verify(site: Site, actors: dict, state_path: Path, receipt_path: Path) -> dict:
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state.get("complete") is not True:
        raise RuntimeError("Prepare phase is incomplete; inspect private intent and receipts")
    if state.get("origin") != site.origin:
        raise ValueError("State belongs to another HTTPS origin")
    branch = state["branch_id"]
    ticket = state["ticket_id"]
    report = {"phase": "verify", "started_at": datetime.now(timezone.utc).isoformat(),
              "origin": site.origin, "branch_id": branch, "ticket_id": ticket,
              "controls": [], "paid_attempts": [], "complete": False,
              "actual_bundle_billing": "unknown"}
    # Exclusive creation rejects an existing receipt before login or any paid request.
    private_new(receipt_path, report)
    site.login(actors["primary"])
    owned = site.required("GET", f"/api/branches/{branch}")
    if owned.get("id") != branch:
        raise RuntimeError("Prepared branch did not survive restart")
    me = observed_control(site, branch, "/support /me", secrets.token_hex(16),
                          report, "controls", receipt_path)
    if ("QA" not in me.get("message", "") or "матового шара" not in me.get("message", "") or
            "day33-temp" in me.get("message", "")):
        raise RuntimeError("Shared profile/memory correction not observed")
    status = observed_control(site, branch, "/support /status", secrets.token_hex(16),
                              report, "controls", receipt_path)
    if "storage" not in status.get("message", ""):
        raise RuntimeError("Observed status missing")
    ticket_read = observed_control(site, branch, f"/support /ticket get {ticket}",
                                   secrets.token_hex(16), report, "controls", receipt_path)
    if ticket not in ticket_read.get("message", "") or "панели источников сервиса" not in ticket_read.get("message", ""):
        raise RuntimeError("Ticket continuation not observed")
    other = Site(site.origin)
    other.login(actors["other"])
    code, denial = other.call("GET", f"/api/branches/{branch}")
    if code not in {403, 404}:
        raise RuntimeError("Other actor could read branch")
    own_chat = other.required("POST", "/api/chats", {"title": "Синтетическая проверка изоляции"})
    probe = {"actor": "other", "branch_id": own_chat["branch_id"],
             "question": f"/support /ticket get {ticket}",
             "request_id": secrets.token_hex(16), "status": "intent"}
    report["other_branch_http"] = code
    report["other_ticket"] = probe
    private_update(receipt_path, report)
    try:
        other_ticket = other.required("POST", f"/api/branches/{probe['branch_id']}/ask",
                                      {"question": probe["question"],
                                       "request_id": probe["request_id"]})
    except Exception as error:
        probe.update(status="unknown", error_type=type(error).__name__)
        private_update(receipt_path, report)
        raise
    probe.update(status=other_ticket.get("status"), message=other_ticket.get("message"),
                 answer=other_ticket.get("answer"), usage=other_ticket.get("usage"),
                 sources=other_ticket.get("sources"),
                 confirmation=other_ticket.get("support_confirmation"))
    private_update(receipt_path, report)
    if other_ticket.get("status") in {"accepted", "support_control"}:
        raise RuntimeError("Other actor ticket read was accepted")
    if "не удалось открыть источник FAQ" in json.dumps(other_ticket, ensure_ascii=False):
        raise RuntimeError("Other actor could read ticket")
    ledger_before = site.required("GET", "/api/ledger")
    report["ledger_before"] = ledger_before
    private_update(receipt_path, report)
    if (ledger_before.get("complete") is not True or
            not isinstance(ledger_before.get("cost_estimated_usd"), (int, float)) or
            ledger_before["cost_estimated_usd"] > 1):
        raise RuntimeError("Unknown accounting blocks paid verification")
    for question in (
        "/support Как устроены сохранённые сведения и ветки на этом сайте? Ответь по документации сервиса и укажи источники.",
        "Почему у матового шара в Unity URP блик шире? Объясни с учётом моего уровня и цели.",
    ):
        entry = {"question": question, "request_id": secrets.token_hex(16), "status": "intent"}
        report["paid_attempts"].append(entry)
        private_update(receipt_path, report)
        try:
            response = site.ask(branch, question, request_id=entry["request_id"])
        except Exception as error:
            entry.update(status="unknown", error=type(error).__name__)
            private_update(receipt_path, report)
            raise
        entry.update(status=response.get("status"), answer=response.get("answer"),
                     message=response.get("message"), sources=response.get("sources"),
                     usage=response.get("usage"))
        private_update(receipt_path, report)
        if response.get("status") != "accepted":
            raise RuntimeError(f"Generated answer not accepted: {response.get('status')}")
        checkpoint = site.required("GET", "/api/ledger")
        entry["ledger_after"] = checkpoint
        private_update(receipt_path, report)
        if checkpoint.get("complete") is not True or checkpoint.get("cost_estimated_usd") is None:
            raise RuntimeError("Unknown accounting blocks next paid stage")
    ledger_after = site.required("GET", "/api/ledger")
    report["ledger_after"] = ledger_after
    private_update(receipt_path, report)
    closed = observed_control(site, branch, f"/support /ticket close {ticket}",
                              secrets.token_hex(16), report, "controls", receipt_path)
    if (closed.get("support_confirmation") or {}).get("receipt", {}).get("operation") != "close":
        raise RuntimeError("User close receipt missing")
    closed_read = observed_control(site, branch, f"/support /ticket get {ticket}",
                                   secrets.token_hex(16), report, "controls", receipt_path)
    if "closed_by_user" not in closed_read.get("message", ""):
        raise RuntimeError("User-closed status not observed")
    report.update(verified_at=datetime.now(timezone.utc).isoformat(),
                  profile_memory=me.get("message"), status=status.get("message"),
                  ticket=ticket_read.get("message"), closed_ticket=closed_read.get("message"),
                  close_confirmation=closed.get("support_confirmation"), complete=True)
    private_update(receipt_path, report)
    return {"phase": "verify", "answers": len(report["paid_attempts"]), "receipt_file": str(receipt_path),
            "ledger_complete": ledger_after.get("complete")}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-live", action="store_true")
    parser.add_argument("--phase", choices=("prepare", "verify"), required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--access-file", type=Path, required=True)
    parser.add_argument("--state-file", type=Path, required=True)
    parser.add_argument("--receipt-file", type=Path)
    args = parser.parse_args(argv)
    if not args.run_live:
        parser.error("No network or account action: explicit --run-live required")
    if args.phase == "verify" and args.receipt_file is None:
        parser.error("--receipt-file is required for verify")
    site = Site(args.base_url)
    actors = access(args.access_file)
    try:
        result = (prepare(site, actors, args.state_file) if args.phase == "prepare" else
                  verify(site, actors, args.state_file, args.receipt_file))
    except Exception as error:
        # No raw HTTP bodies or credential-bearing exceptions on stdout/stderr.
        print(f"Live check stopped: {type(error).__name__}")
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

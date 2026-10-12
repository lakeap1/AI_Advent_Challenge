"""Bounded real-network probe of the private API; never retries an inference."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


PROMPTS = (
    "Что делает ключевой свет в портрете? Ответь одним коротким предложением.",
    "Как roughness меняет блик в PBR? Ответь одним коротким предложением.",
    "Зачем используют ambient occlusion? Ответь одним коротким предложением.",
    "Что такое нормаль поверхности в 3D? Ответь одним коротким предложением.",
    "Когда полезен контровой свет? Ответь одним коротким предложением.",
    "Чем отличается альбедо от освещения? Ответь одним коротким предложением.",
)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        raise RuntimeError(f"Redirect refused: HTTP {code}")


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def call(base_url: str, token: str, method: str, path: str, payload=None):
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Authorization": f"Bearer {token}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = Request(base_url + path, data=data, headers=headers, method=method)
    started = time.monotonic()
    try:
        # Never allow environment or system proxy settings to carry the Bearer
        # token away from the loopback SSH tunnel.
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=390) as response:
            status = response.status
            raw = response.read()
            response_headers = response.headers
    except HTTPError as error:
        status = error.code
        raw = error.read()
        response_headers = error.headers
    body = json.loads(raw.decode("utf-8"))
    return {
        "method": method, "path": path, "request": payload,
        "http_status": status, "elapsed_seconds": round(time.monotonic() - started, 3),
        "retry_after": response_headers.get("Retry-After"), "body": body,
    }


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def require_safe_state(event):
    require(event["http_status"] == 200, "State endpoint did not return HTTP 200")
    state = event["body"]
    require(not state.get("blocked"), "Ledger is blocked; no more paid/inference requests")
    ledger = state.get("ledger") or {}
    require(ledger.get("complete") is True and not ledger.get("unknown_usage"),
            "Ledger is incomplete; no more inference requests")
    return state


def require_answer(event, *, isolated):
    require(event["http_status"] == 200, f"Ask returned HTTP {event['http_status']}; stopping")
    body = event["body"]
    receipt = body.get("receipt") or {}
    usage = receipt.get("usage") or {}
    require(body.get("status") == "ok" and isinstance(body.get("answer"), str)
            and body["answer"].strip(), "No completed answer; stopping")
    require(all(type(usage.get(key)) is int for key in
                ("input_tokens", "output_tokens", "total_tokens")),
            "Token usage unknown; stopping")
    require(usage["input_tokens"] + usage["output_tokens"] == usage["total_tokens"],
            "Token totals inconsistent; stopping")
    require(receipt.get("cost_usd") is not None, "Request cost unknown; stopping")
    require(receipt.get("status") == "ok" and receipt.get("output_policy") == "accepted"
            and receipt.get("usage_status") == "known", "Receipt not durably accepted; stopping")
    if isolated:
        state = body.get("state") or {}
        require(not state.get("blocked") and (state.get("ledger") or {}).get("complete") is True,
                "Answer left ledger blocked or incomplete; stopping")


def check_remaining(report, record, *, counter_before, prior_initial,
                    inference_calls_added):
    context = record("context_rejection", "POST", "/api/private/ask",
                     {"prompt": "Ж" * 1800})
    require(context["http_status"] == 400, "Expected context rejection HTTP 400")
    require((context["body"].get("receipt") or {}).get("usage_status") == "not_requested",
            "Context rejection lacks not_requested receipt")

    # CPU inference may take longer than the 60-second rate window. Fill a
    # fresh window with cheap invalid attempts, stopping at the first 429.
    limited = None
    for index in range(11):
        event = record(f"rate_attempt_{index+1}", "POST", "/api/private/ask", {"prompt": ""})
        if event["http_status"] == 429:
            limited = event
            break
        require(event["http_status"] == 400, "Expected invalid input HTTP 400 before limit")
    require(limited is not None and limited["body"].get("code") == "rate_limited"
            and limited["retry_after"] is not None, "Expected rate limit HTTP 429/Retry-After")
    final = require_safe_state(record("final_state", "GET", "/api/private/state"))
    require(final["counters"]["inference_calls"] == counter_before,
            "Rejected requests reached model inference")
    report["final_state"] = final
    report["summary"] = {
        "sequential_successes": 3, "concurrent_successes": 3,
        "context_http_status": context["http_status"],
        "rate_http_status": limited["http_status"],
        "inference_calls_added": inference_calls_added,
        "receipts_added": len(final["receipts"]) - len(prior_initial["receipts"]),
        "total_cost_usd": final["ledger"]["total_cost_usd"],
    }


def run_probe(base_url, token, report):
    events = report["events"]

    def record(label, method, path, payload=None):
        event = call(base_url, token, method, path, payload)
        event["label"] = label
        events.append(event)
        return event

    initial = require_safe_state(record("initial_state", "GET", "/api/private/state"))
    runtime = record("initial_runtime", "GET", "/api/private/runtime")
    require(runtime["http_status"] == 200 and runtime["body"].get("status") == "ready",
            "Runtime/model not ready; no inference attempted")
    require(runtime["body"].get("model") == "qwen3.5:4b", "Unexpected model")
    report["initial_state"] = initial
    report["runtime"] = runtime["body"]
    unauthorized = call(base_url, "invalid", "GET", "/api/private/state")
    unauthorized["label"] = "unauthorized_state"
    events.append(unauthorized)
    require(unauthorized["http_status"] == 401, "Unauthenticated state did not return HTTP 401")

    for index, prompt in enumerate(PROMPTS[:3], 1):
        require_answer(record(f"sequential_{index}", "POST", "/api/private/ask",
                              {"prompt": prompt}), isolated=True)

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(call, base_url, token, "POST", "/api/private/ask",
                               {"prompt": prompt}) for prompt in PROMPTS[3:]]
        concurrent = []
        failures = []
        for index, future in enumerate(futures, 1):
            try:
                event = future.result()
                event["label"] = f"concurrent_{index}"
                events.append(event)
                concurrent.append(event)
            except Exception as error:
                failures.append(f"concurrent_{index}: {type(error).__name__}: {error}")
                events.append({"label": f"concurrent_{index}", "transport_error": failures[-1]})
    require(not failures, "; ".join(failures))
    for event in concurrent:
        # A sibling may still have a pending receipt when this response's
        # state projection is assembled; the joined state is checked below.
        require_answer(event, isolated=False)

    after_answers = require_safe_state(record("after_answers", "GET", "/api/private/state"))
    observed_runtime = record("runtime_after_inference", "GET", "/api/private/runtime")
    require(observed_runtime["http_status"] == 200, "Post-inference runtime unavailable")
    report["runtime_after_inference"] = observed_runtime["body"]
    require(observed_runtime["body"].get("processor") == "CPU",
            "Ollama API did not confirm CPU placement after inference")
    count = after_answers["counters"]["inference_calls"]
    require(count - initial["counters"]["inference_calls"] == 6,
            "Inference counter did not increase by six")
    require(len(after_answers["receipts"]) - len(initial["receipts"]) == 6,
            "Durable receipt count did not increase by six")

    check_remaining(report, record, counter_before=count,
                    prior_initial=initial, inference_calls_added=6)


def resume_checks(base_url, token, previous, report):
    prior_bytes = Path(previous).read_bytes()
    prior = json.loads(prior_bytes.decode("utf-8"))
    require(prior.get("mode") == "probe" and prior.get("status") == "fail",
            "Resume requires a failed original probe")
    require(prior.get("base_url") == base_url, "Resume target differs from prior probe")
    prior_events = prior.get("events") or []
    labels = {event.get("label"): event for event in prior_events}
    require(all(label not in labels for label in
                ("context_rejection", "rate_rejection", "final_state")) and
            not any(label.startswith("rate_attempt_") for label in labels),
            "Prior probe already started the remaining checks")
    original_initial = prior["initial_state"]
    old_after_event = labels["after_answers"]
    old_after = require_safe_state(old_after_event)
    old_receipts = {receipt["id"]: receipt for receipt in old_after["receipts"]}
    ids = []
    for index, prompt in enumerate(PROMPTS):
        label = f"sequential_{index + 1}" if index < 3 else f"concurrent_{index - 2}"
        event = labels[label]
        require(event.get("request") == {"prompt": prompt},
                f"Prior {label} prompt differs")
        require_answer(event, isolated=False)
        receipt = event["body"]["receipt"]
        require(old_receipts.get(receipt["id"]) == receipt,
                f"Prior {label} receipt differs from saved after_answers state")
        ids.append(receipt["id"])
    require(len(set(ids)) == 6, "Prior six answers do not have distinct receipt IDs")
    require(old_after["counters"]["inference_calls"] -
            original_initial["counters"]["inference_calls"] == 6,
            "Original inference count did not increase by six")
    require(len(old_after["receipts"]) - len(original_initial["receipts"]) == 6,
            "Original durable receipt count did not increase by six")

    report["events"] = list(prior_events)
    report["initial_state"] = original_initial
    report["runtime"] = prior["runtime"]
    report["resumed_from"] = {
        "path": str(previous), "sha256": hashlib.sha256(prior_bytes).hexdigest(),
        "started_utc": prior.get("started_utc"), "status": prior["status"],
        "error": prior.get("error"), "original_event_count": len(prior_events),
    }

    def record(label, method, path, payload=None):
        event = call(base_url, token, method, path, payload)
        event["label"] = label
        report["events"].append(event)
        return event

    current = require_safe_state(record("resume_state_before_checks", "GET", "/api/private/state"))
    require(current["receipts"] == old_after["receipts"],
            "Durable receipts changed or grew since the six-answer probe")
    require(current["messages"] == old_after["messages"],
            "Chat history changed or grew since the six-answer probe")
    require(current["ledger"] == old_after["ledger"],
            "Ledger changed since the six-answer probe")
    report["resume_state_before_checks"] = current
    runtime = record("resume_runtime", "GET", "/api/private/runtime")
    require(runtime["http_status"] == 200 and runtime["body"].get("status") == "ready"
            and runtime["body"].get("model") == "qwen3.5:4b",
            "Runtime/model unavailable during resume")
    report["runtime_after_inference"] = runtime["body"]
    require(runtime["body"].get("processor") == "CPU",
            "Updated runtime did not report configured CPU placement")
    unauthorized = call(base_url, "invalid", "GET", "/api/private/state")
    unauthorized["label"] = "resume_unauthorized_state"
    report["events"].append(unauthorized)
    require(unauthorized["http_status"] == 401, "Unauthenticated state did not return HTTP 401")
    counter_before = current["counters"]["inference_calls"]
    check_remaining(report, record, counter_before=counter_before,
                    prior_initial=original_initial, inference_calls_added=6)
    report["summary"]["inference_count_source"] = "original_probe_events_before_restart"
    report["summary"]["inference_calls_during_resume"] = 0


def verify_restart(base_url, token, previous, report):
    prior = json.loads(Path(previous).read_text(encoding="utf-8"))
    old_state = prior["final_state"]
    state_event = call(base_url, token, "GET", "/api/private/state")
    state_event["label"] = "state_after_restart"
    report["events"].append(state_event)
    state = require_safe_state(state_event)
    old_receipts = old_state["receipts"]
    require(state["receipts"][:len(old_receipts)] == old_receipts,
            "Receipts changed or disappeared after restart")
    require(state["ledger"] == old_state["ledger"],
            "Ledger changed after restart")
    report["summary"] = {"receipts_preserved": len(old_receipts),
                         "ledger_preserved": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:5030")
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--verify-restart", type=Path,
                        help="Read-only check against a completed prior probe after an operator restart")
    modes.add_argument("--resume-checks", type=Path,
                       help="Finish only no-inference checks from a failed six-answer probe")
    args = parser.parse_args()
    previous = args.verify_restart or args.resume_checks
    if previous and args.output.resolve() == previous.resolve():
        parser.error("output must differ from the prior report to preserve evidence")
    parsed = urlparse(args.base_url)
    if (parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost")
            or parsed.username or parsed.password or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment):
        parser.error("base URL must be local HTTP loopback; use the SSH tunnel")
    token = args.token_file.read_text(encoding="ascii").strip()
    if len(token) < 32:
        parser.error("token file contains no valid private key")
    report = {"started_utc": utc_now(), "base_url": args.base_url.rstrip("/"),
              "mode": "verify_restart" if args.verify_restart else
              "resume_checks" if args.resume_checks else "probe",
              "events": [], "status": "incomplete"}
    try:
        if args.verify_restart:
            verify_restart(args.base_url.rstrip("/"), token, args.verify_restart, report)
        elif args.resume_checks:
            resume_checks(args.base_url.rstrip("/"), token, args.resume_checks, report)
        else:
            run_probe(args.base_url.rstrip("/"), token, report)
        report["status"] = "pass"
    except Exception as error:
        report["status"] = "fail"
        report["error"] = f"{type(error).__name__}: {error}"
        try:
            state = call(args.base_url.rstrip("/"), token, "GET", "/api/private/state")
            state["label"] = "state_after_failure"
            report["events"].append(state)
        except Exception as state_error:
            report["state_error"] = f"{type(state_error).__name__}: {state_error}"
    finally:
        report["finished_utc"] = utc_now()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8")
    print(f"{report['status']}: {args.output}")
    if report["status"] != "pass":
        print(report["error"], file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

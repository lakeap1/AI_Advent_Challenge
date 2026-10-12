"""Process-local no-cloud probe against an existing Day 28 index."""

import argparse
import ipaddress
import json
import os
import socket
import sys
from contextlib import contextmanager
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
QUESTION = "В Unity 6 URP как уменьшить слишком резкий блик материала через Smoothness?"


class ExternalNetworkBlocked(OSError):
    pass


def _loopback(host):
    if isinstance(host, bytes):
        host = host.decode("ascii", errors="ignore")
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except (ValueError, TypeError):
        return False


@contextmanager
def loopback_guard():
    """Deny non-loopback DNS/socket operations in this process; restore hooks."""
    originals = {name: getattr(socket, name) for name in
                 ("getaddrinfo", "gethostbyname", "gethostbyname_ex",
                  "gethostbyaddr", "getnameinfo")}
    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex
    sendto = socket.socket.sendto
    audit = {"blocked_external_attempts": 0, "allowed_loopback_attempts": 0}

    def check(host):
        if not _loopback(host):
            audit["blocked_external_attempts"] += 1
            raise ExternalNetworkBlocked("External DNS/socket access blocked by process-local probe")
        audit["allowed_loopback_attempts"] += 1

    def guarded_connect(sock, address):
        if sock.family != getattr(socket, "AF_UNIX", None):
            check(address[0])
        return connect(sock, address)

    def guarded_connect_ex(sock, address):
        if sock.family != getattr(socket, "AF_UNIX", None):
            check(address[0])
        return connect_ex(sock, address)

    def guarded_sendto(sock, *args):
        if sock.family != getattr(socket, "AF_UNIX", None):
            address = args[-1]
            check(address[0])
        return sendto(sock, *args)

    def guarded_lookup(host, *args, **kwargs):
        check(host)
        return originals["getaddrinfo"](host, *args, **kwargs)

    def guarded_name(host):
        check(host)
        return originals["gethostbyname"](host)

    def guarded_name_ex(host):
        check(host)
        return originals["gethostbyname_ex"](host)

    def guarded_reverse(host):
        check(host)
        return originals["gethostbyaddr"](host)

    def guarded_nameinfo(sockaddr, flags):
        check(sockaddr[0])
        return originals["getnameinfo"](sockaddr, flags)

    try:
        socket.socket.connect = guarded_connect
        socket.socket.connect_ex = guarded_connect_ex
        socket.socket.sendto = guarded_sendto
        socket.getaddrinfo = guarded_lookup
        socket.gethostbyname = guarded_name
        socket.gethostbyname_ex = guarded_name_ex
        socket.gethostbyaddr = guarded_reverse
        socket.getnameinfo = guarded_nameinfo
        yield audit
    finally:
        socket.socket.connect = connect
        socket.socket.connect_ex = connect_ex
        socket.socket.sendto = sendto
        for name, original in originals.items():
            setattr(socket, name, original)


def probe(data_dir, question=QUESTION):
    from app import create_app

    original_key = os.environ.pop("OPENAI_API_KEY", None)
    report = {"scope": "Python process DNS/socket hooks only; not a machine firewall or airgap",
              "data_dir": str(data_dir), "question": question}
    try:
        with loopback_guard() as audit:
            # Negative control proves the hooks are active without contacting the network.
            try:
                socket.getaddrinfo("example.com", 443)
            except ExternalNetworkBlocked:
                pass
            else:
                raise AssertionError("External DNS negative control was not blocked")
            report["negative_control_blocked"] = True
            os.environ.pop("OPENAI_API_KEY", None)
            app = create_app(data_dir=data_dir)
            # Legacy app creation may load a dotenv; strip again before state and ask.
            os.environ.pop("OPENAI_API_KEY", None)
            with app.test_client() as client:
                state_response = client.get("/api/local-rag/state")
                state = state_response.get_json()
                report["index_status"] = state.get("index", {}).get("status")
                report["cloud_available"] = state.get("cloud_available")
                if state_response.status_code != 200 or report["index_status"] != "ready":
                    report["status"] = "index_not_ready"
                elif state.get("blocked"):
                    report["status"] = "blocked_receipt"
                else:
                    response = client.post("/api/local-rag/ask", json={"prompt": question, "mode": "local"})
                    body = response.get_json()
                    report["http_status"] = response.status_code
                    report["result_status"] = body.get("status")
                    report["answer"] = next((item.get("answer") for item in body.get("results", [])
                                             if item.get("provider") == "local"), None)
                    report["sources"] = body.get("retrieval", {}).get("sources", []) if body.get("retrieval") else []
                    report["context_sha256"] = (body.get("retrieval") or {}).get("context_sha256")
                    report["request_sha256"] = (body.get("retrieval") or {}).get("request_sha256")
                    report["receipts"] = [item.get("receipt") for item in body.get("results", [])]
                    report["ledger"] = body.get("ledger")
                    report["status"] = "pass" if response.status_code == 200 and body.get("status") == "ok" else "ask_failed"
            report["network"] = dict(audit)
            if audit["blocked_external_attempts"] != 1:
                report["status"] = "external_attempt_blocked"
    finally:
        os.environ.pop("OPENAI_API_KEY", None)
        if original_key is not None:
            os.environ["OPENAI_API_KEY"] = original_key
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--question", default=QUESTION)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.output and args.output.exists():
        parser.error("refusing to overwrite existing probe evidence")
    result = probe(args.data_dir, args.question)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())

"""Real Day 27 application probe with external socket connections forbidden."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent


def install_local_network_guard():
    """Process-only audit guard; does not alter machine networking or Ollama."""
    events = []

    def check_host(host):
        if host in ("localhost", b"localhost"):
            return True
        try:
            return ipaddress.ip_address(host.decode() if isinstance(host, bytes) else host).is_loopback
        except (ValueError, TypeError):
            return False

    def audit(event, args):
        if event == "socket.connect":
            address = args[1]
            if not isinstance(address, tuple) or not check_host(address[0]):
                events.append({"event": event, "allowed": False, "address": repr(address)})
                raise PermissionError("Проверка дня 27 запрещает внешние соединения процесса приложения")
            events.append({"event": event, "allowed": True, "address": list(address)})
        elif event == "socket.getaddrinfo":
            host = args[0]
            if host is not None and not check_host(host):
                events.append({"event": event, "allowed": False, "host": str(host)})
                raise PermissionError("Проверка дня 27 запрещает внешний DNS")
    sys.addaudithook(audit)
    return events


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true", help="Run the same app with process network guard")
    parser.add_argument("--port", type=int, default=5027)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    # This script deliberately removes the cloud credential only from its own process.
    os.environ.pop("OPENAI_API_KEY", None)
    network_events = install_local_network_guard()
    from app import create_app

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    data_dir = args.data_dir or ROOT / "data" / ("chat" if args.serve else "checks" / Path(stamp))
    app = create_app(data_dir=data_dir)
    if os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("Для проверки уберите OPENAI_API_KEY из локального .env")
    if args.serve:
        print("Day 27: external process connections forbidden; cloud credential absent; http://127.0.0.1:" + str(args.port), flush=True)
        app.run(host="127.0.0.1", port=args.port, threaded=True, use_reloader=False)
        return 0

    output = args.output or ROOT / "data" / ("local-check-" + stamp + ".json")
    if output.exists():
        raise FileExistsError("Проверочный результат не перезаписывается: " + str(output))
    client = app.test_client()
    prompts = [
        "Работаю в Blender 4.4. У пластикового материала roughness 0.23, metalness 0. "
        "Хочу более широкий мягкий блик. Объясни кратко, что изменит увеличение roughness; не путай его с размером источника света.",
        "Какое исходное значение roughness я указал и в какую сторону его менять для моей цели? Ответь двумя короткими абзацами.",
    ]
    report = {"status": "running", "cloud_key_present": False,
              "network_guard": "Python process: external connect/DNS denied; loopback allowed",
              "physical_internet_disconnected": False, "data_dir": str(data_dir),
              "runtime": client.get("/api/local/runtime").get_json(), "turns": []}
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        for prompt in prompts:
            response = client.post("/api/local/ask", json={"prompt": prompt})
            result = response.get_json()
            report["turns"].append({"prompt": prompt, "http_status": response.status_code, **result})
            if response.status_code != 200 or result.get("status") != "ok":
                raise RuntimeError("Локальный ход не прошёл: " + str(result.get("status")))
        answer = report["turns"][-1]["answer"]
        if not any(value in answer for value in ("0.23", "0,23")):
            raise RuntimeError("Модель не восстановила исходное значение roughness")
        before = client.get("/api/local/state").get_json()
        restored = create_app(data_dir=data_dir).test_client().get("/api/local/state").get_json()
        # GET returns the state object; history and receipts must survive a new app instance.
        report["restart_equal"] = before == restored
        if not report["restart_equal"]:
            raise RuntimeError("Состояние локального чата не восстановилось")
        report["network_events"] = network_events
        if any(not item["allowed"] for item in network_events):
            raise RuntimeError("Приложение пыталось обратиться во внешнюю сеть")
        report["status"] = "passed"
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = str(exc)
        report["network_events"] = network_events
        raise
    finally:
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(str(output), flush=True)
    print("PASS: 2 real local turns; context/restart; cloud credential absent; external process connections denied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

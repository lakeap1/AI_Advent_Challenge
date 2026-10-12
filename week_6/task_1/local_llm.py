"""Bounded Ollama client and durable metadata ledger for Day 26."""

from __future__ import annotations

import json
import tomllib
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parent
SYSTEM_INSTRUCTION = (
    "Ты помощник по компьютерной графике и художественной работе. "
    "Отвечай по-русски обычным текстом с абзацами, без Markdown: "
    "без заголовков, списков с разметкой, таблиц, звёздочек и обратных кавычек. "
    "Не выдумывай факты; если причина зависит от инструмента, обозначь допущение."
)


class OllamaTransport:
    def __init__(self, endpoint: str, timeout_seconds: int):
        if endpoint != "http://127.0.0.1:11434":
            raise ValueError("Разрешён только http://127.0.0.1:11434")
        self.endpoint = endpoint
        self.timeout_seconds = timeout_seconds
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def __call__(self, method: str, path: str, payload: dict | None = None) -> dict:
        if path not in {"/api/chat", "/api/version", "/api/tags", "/api/show", "/api/ps"}:
            raise ValueError("Недопустимый путь API")
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint + path,
            data=body,
            headers={"Content-Type": "application/json"} if body is not None else {},
            method=method,
        )
        with self.opener.open(request, timeout=self.timeout_seconds) as response:
            result = json.load(response)
        if not isinstance(result, dict):
            raise ValueError("API вернул не JSON-объект")
        return result


def _count(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


class LocalAgent:
    def __init__(
        self,
        config_path: Path = ROOT / "config.toml",
        ledger_path: Path = ROOT / "data" / "requests.jsonl",
        transport: Callable[[str, str, dict | None], dict] | None = None,
    ):
        self.config = tomllib.loads(Path(config_path).read_text(encoding="utf-8"))
        runtime = self.config["runtime"]
        if runtime["endpoint"] != "http://127.0.0.1:11434" or runtime["model"] != "qwen3.5:9b":
            raise ValueError("Конфигурация должна использовать локальную qwen3.5:9b на фиксированном loopback")
        self.transport = transport or OllamaTransport(runtime["endpoint"], runtime["timeout_seconds"])
        self.ledger_path = Path(ledger_path)

    def _cumulative_cost(self) -> float:
        if not self.ledger_path.exists():
            return 0.0
        total = 0.0
        for line in self.ledger_path.read_text(encoding="utf-8").splitlines():
            if line:
                amount = json.loads(line)["pricing"]["api_cost_usd"]
                if amount is not None:
                    total += amount
        return total

    def _write(self, receipt: dict) -> None:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        receipt["pricing"]["cumulative_api_cost_usd"] = self._cumulative_cost() + (
            receipt["pricing"]["api_cost_usd"] or 0.0
        )
        with self.ledger_path.open("a", encoding="utf-8", newline="\n") as file:
            file.write(json.dumps(receipt, ensure_ascii=False) + "\n")

    def _receipt(self, scenario_id: str | None, save_text: bool) -> dict:
        pricing = self.config["pricing"]
        return {
            "request_id": str(uuid.uuid4()),
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "scenario_id": scenario_id,
            "model": self.config["runtime"]["model"],
            "endpoint": self.config["runtime"]["endpoint"],
            "parameters": {"stream": False, "think": False, **self.config["generation"]},
            "save_text": save_text,
            "api_called": False,
            "input_policy": {"status": "not_run", "reason": None},
            "output_policy": {"status": "not_run", "reason": None},
            "usage": {
                "input_tokens": None,
                "output_tokens": None,
                "total_tokens": None,
                "cached_input_tokens": None,
                "reasoning_tokens": None,
                "source": "Ollama prompt_eval_count/eval_count; total computed only when both present",
            },
            "raw_usage": {"prompt_eval_count": None, "eval_count": None, "prompt_eval_cached_count": None},
            "durations_ns": {
                "total_duration": None,
                "load_duration": None,
                "prompt_eval_duration": None,
                "eval_duration": None,
            },
            "done": None,
            "done_reason": None,
            "pricing": {
                "api_cost_usd": 0,
                "cumulative_api_cost_usd": None,
                "infrastructure_cost_usd": None,
                "service_mode": pricing["service_mode"],
                "source": pricing["source"],
                "checked_date": pricing["checked_date"],
                "basis": pricing["basis"],
            },
            "status": "pending",
            "error": None,
        }

    def run(self, prompt: str, scenario_id: str | None = None, save_text: bool = False) -> dict:
        receipt = self._receipt(scenario_id, save_text)
        max_characters = self.config["input_policy"]["max_characters"]
        if not isinstance(prompt, str) or not prompt.strip():
            receipt["input_policy"] = {"status": "rejected", "reason": "empty_input"}
            receipt["status"] = "input_rejected"
        elif len(prompt) > max_characters:
            receipt["input_policy"] = {"status": "rejected", "reason": "input_too_long"}
            receipt["status"] = "input_rejected"
        else:
            receipt["input_policy"] = {"status": "passed", "reason": None}
            if save_text:
                receipt["prompt"] = prompt
            payload = {
                "model": self.config["runtime"]["model"],
                "messages": [
                    {"role": "system", "content": SYSTEM_INSTRUCTION},
                    {"role": "user", "content": prompt},
                ],
                "stream": False,
                "think": False,
                "options": dict(self.config["generation"]),
            }
            receipt["api_called"] = True
            try:
                response = self.transport("POST", "/api/chat", payload)
                if not isinstance(response, dict):
                    raise ValueError("API вернул не JSON-объект")
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                receipt["status"] = "api_error"
                receipt["error"] = f"{type(exc).__name__}: {exc}"[:400]
            else:
                receipt["raw_usage"] = {
                    "prompt_eval_count": response.get("prompt_eval_count"),
                    "eval_count": response.get("eval_count"),
                    "prompt_eval_cached_count": response.get("prompt_eval_cached_count"),
                }
                counts = receipt["usage"]
                counts["input_tokens"] = _count(response.get("prompt_eval_count"))
                counts["output_tokens"] = _count(response.get("eval_count"))
                counts["cached_input_tokens"] = _count(response.get("prompt_eval_cached_count"))
                if counts["input_tokens"] is not None and counts["output_tokens"] is not None:
                    counts["total_tokens"] = counts["input_tokens"] + counts["output_tokens"]
                for key in receipt["durations_ns"]:
                    receipt["durations_ns"][key] = response.get(key)
                receipt["done"] = response.get("done")
                receipt["done_reason"] = response.get("done_reason")
                message = response.get("message")
                answer = message.get("content") if isinstance(message, dict) else None
                if save_text and isinstance(answer, str):
                    receipt["response_text"] = answer
                reason = None
                output_policy = self.config["output_policy"]
                if response.get("error"):
                    reason = "response_error"
                elif output_policy["require_done"] and response.get("done") is not True:
                    reason = "incomplete_response"
                elif output_policy["reject_length"] and response.get("done_reason") == "length":
                    reason = "length"
                elif output_policy["require_nonempty"] and (not isinstance(answer, str) or not answer.strip()):
                    reason = "empty_output"
                if reason:
                    receipt["output_policy"] = {"status": "rejected", "reason": reason}
                    receipt["status"] = "output_rejected"
                else:
                    receipt["output_policy"] = {"status": "passed", "reason": None}
                    receipt["status"] = "usage_unknown" if counts["total_tokens"] is None else "ok"
        self._write(receipt)
        return {
            "status": receipt["status"],
            "answer": answer if receipt["status"] == "ok" else None,
            "receipt": receipt,
        }

    def run_batch(self, scenarios: list[tuple[str, str]], save_text: bool = False) -> list[dict]:
        results = []
        for scenario_id, prompt in scenarios:
            result = self.run(prompt, scenario_id=scenario_id, save_text=save_text)
            results.append(result)
            if result["status"] != "ok":
                break
        return results


def load_scenarios(path: Path = ROOT / "scenarios.json") -> list[dict]:
    scenarios = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(scenarios, list) or len(scenarios) != 3:
        raise ValueError("Ожидались ровно три фиксированных сценария")
    return scenarios


def runtime_status(agent: LocalAgent) -> dict:
    result: dict[str, Any] = {"endpoint": agent.config["runtime"]["endpoint"], "model": agent.config["runtime"]["model"]}
    for label, method, path, payload in [
        ("version", "GET", "/api/version", None),
        ("tags", "GET", "/api/tags", None),
        ("ps", "GET", "/api/ps", None),
        ("show", "POST", "/api/show", {"model": agent.config["runtime"]["model"]}),
    ]:
        try:
            data = agent.transport(method, path, payload)
            if label == "version":
                result[label] = data.get("version")
            elif label == "tags":
                result[label] = [item.get("name") for item in data.get("models", []) if isinstance(item, dict)]
            elif label == "ps":
                result[label] = [{"name": item.get("name"), "size_vram": item.get("size_vram"), "size": item.get("size")} for item in data.get("models", []) if isinstance(item, dict)]
            else:
                details = data.get("details") or {}
                result[label] = {"format": details.get("format"), "family": details.get("family"), "parameter_size": details.get("parameter_size"), "quantization_level": details.get("quantization_level"), "model_info": {key: value for key, value in (data.get("model_info") or {}).items() if "context_length" in key}}
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            result[label] = {"error": f"{type(exc).__name__}: {exc}"[:400]}
    return result

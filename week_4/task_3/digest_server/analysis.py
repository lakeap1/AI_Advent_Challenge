"""One bounded Responses API analysis of a published daily digest."""

from __future__ import annotations

import asyncio
import json
import re
import tomllib
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx


CONFIG_PATH = Path(__file__).with_name("analysis.toml")
API_URL = "https://api.openai.com/v1/responses"


def _counter(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _usage(response: object) -> dict[str, int | None] | None:
    raw = response.get("usage") if isinstance(response, dict) else None
    if not isinstance(raw, dict):
        return None
    incoming, outgoing, total = (_counter(raw.get(key)) for key in
                                  ("input_tokens", "output_tokens", "total_tokens"))
    if incoming is None or outgoing is None or total != incoming + outgoing:
        return None
    in_details = raw.get("input_tokens_details")
    out_details = raw.get("output_tokens_details")
    cached = _counter(in_details.get("cached_tokens")) if isinstance(in_details, dict) else None
    written = _counter(in_details.get("cache_write_tokens")) if isinstance(in_details, dict) else None
    reasoning = _counter(out_details.get("reasoning_tokens")) if isinstance(out_details, dict) else None
    if cached is not None and cached > incoming:
        cached = None
    if written is not None and (written > incoming or cached is not None and written + cached > incoming):
        written = None
    if reasoning is not None and reasoning > outgoing:
        reasoning = None
    return dict(input_tokens=incoming, output_tokens=outgoing, total_tokens=total,
                cached_input_tokens=cached, cache_write_input_tokens=written,
                reasoning_tokens=reasoning)


def _cost(response: object, usage: dict[str, int | None] | None,
          config: dict[str, Any]) -> str | None:
    if not isinstance(response, dict) or usage is None:
        return None
    tariff = config["pricing"]
    if (response.get("model") != config["model"] or response.get("service_tier") != "default"
            or tariff["model"] != config["model"] or usage["input_tokens"] > tariff["max_input_tokens"]):
        return None
    cached, written = usage["cached_input_tokens"], usage["cache_write_input_tokens"]
    if cached is None or written is None or written and tariff.get("cache_write_usd_per_million") is None:
        return None
    details = response.get("usage", {}).get("input_tokens_details", {})
    if not isinstance(details, dict) or any(value for key, value in details.items()
                                               if key != "cache_write_tokens" and ("writ" in key or "creat" in key)):
        return None
    try:
        amount = ((usage["input_tokens"] - cached - written) * Decimal(tariff["input_usd_per_million"])
                  + cached * Decimal(tariff["cached_input_usd_per_million"])
                  + written * Decimal(tariff.get("cache_write_usd_per_million", "0"))
                  + usage["output_tokens"] * Decimal(tariff["output_usd_per_million"])) / Decimal(1_000_000)
        return format(amount.normalize(), "f")
    except (InvalidOperation, TypeError, ArithmeticError):
        return None


def _text(response: object) -> tuple[str, str]:
    if not isinstance(response, dict):
        return "", "invalid_response"
    if response.get("error"):
        return "", "provider_error"
    if response.get("status") != "completed":
        return "", "incomplete" if response.get("status") == "incomplete" else "provider_error"
    output = response.get("output")
    if not isinstance(output, list):
        return "", "invalid_response"
    parts: list[str] = []
    for item in output:
        if not isinstance(item, dict):
            return "", "invalid_response"
        if item.get("type") == "reasoning":
            continue
        if item.get("type") != "message" or item.get("role") != "assistant" or item.get("status") != "completed":
            return "", "invalid_response"
        if not isinstance(item.get("content"), list):
            return "", "invalid_response"
        for block in item["content"]:
            if not isinstance(block, dict):
                return "", "invalid_response"
            if block.get("type") == "refusal":
                return "", "refusal"
            if block.get("type") != "output_text" or not isinstance(block.get("text"), str):
                return "", "invalid_response"
            parts.append(block["text"])
    value = "".join(parts).strip()
    if not value:
        return "", "empty_output"
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return "", "invalid_response"
    if re.search(r"(?m)^\s*(?:#{1,6}\s|```|\|.*\|\s*$)|\*\*[^*]+\*\*", value):
        return "", "markdown_output"
    return value, ""


def _input(digest: dict[str, Any], limit: int) -> tuple[str | None, str, dict[str, Any] | None]:
    questions = digest.get("questions")
    if not isinstance(questions, list) or len(questions) > 300:
        return None, "invalid_digest", None
    source_status = digest.get("source_status")
    if not isinstance(source_status, dict) or not source_status:
        return None, "invalid_digest", None
    validated = []
    for item in questions:
        if (not isinstance(item, dict) or type(item.get("answer_count")) is not int
                or item["answer_count"] < 0 or any(not isinstance(item.get(key), str)
                                                   for key in ("source", "title", "excerpt", "url"))):
            return None, "invalid_digest", None
        if (item["source"] not in source_status or not isinstance(item.get("tags"), list)
                or any(not isinstance(tag, str) for tag in item["tags"])):
            return None, "invalid_digest", None
        validated.append({key: item[key] for key in ("source", "title", "excerpt", "tags", "url", "answer_count")})

    # Round-robin preserves each source's original order and keeps all available
    # sources represented even when the complete release exceeds the model budget.
    buckets = {source: [item for item in validated if item["source"] == source]
               for source in source_status}
    candidates = []
    for position in range(max((len(bucket) for bucket in buckets.values()), default=0)):
        candidates.extend(bucket[position] for bucket in buckets.values() if position < len(bucket))

    def encoded(selected: list[dict[str, Any]]) -> str:
        count = len(selected)
        payload = {"digest_date": digest.get("digest_date"), "partial": digest.get("partial"),
                   "source_status": source_status, "total_questions": len(validated),
                   "included_questions": count, "omitted_questions": len(validated) - count,
                   "context_truncated": count < len(validated), "questions": selected}
        return json.dumps(payload, ensure_ascii=False)

    try:
        selected = []
        for item in candidates:
            if len(encoded([*selected, item])) <= limit:
                selected.append(item)
        value = encoded(selected)
        value.encode("utf-8")
    except (TypeError, ValueError, UnicodeError, KeyError):
        return None, "invalid_digest", None
    if validated and not selected or len(value) > limit:
        return None, "input_too_long", None
    counts = {source: sum(item["source"] == source for item in selected) for source in source_status}
    context = {"total_questions": len(validated), "included_questions": len(selected),
               "omitted_questions": len(validated) - len(selected),
               "included_by_source": counts, "truncated": len(selected) < len(validated),
               "input_chars": len(value)}
    return value, "", context


class DailyAnalyzer:
    def __init__(self, api_key: str | None, *, config_path: Path = CONFIG_PATH,
                 client: httpx.AsyncClient | None = None):
        with config_path.open("rb") as stream:
            self.config = tomllib.load(stream)
        if (self.config["model"] != "gpt-6-luna"
                or self.config["input_policy"] != "bounded_digest"
                or self.config["output_policy"] != "completed_plain_text"
                or self.config["service_tier"] != "default"):
            raise ValueError("Unsupported daily analysis configuration")
        self.api_key = (api_key or "").strip()
        self.client = client

    def metadata(self, digest: dict[str, Any]) -> dict[str, Any]:
        return {"digest_date": digest["digest_date"], "run_id": digest["run_id"],
                "requested_model": self.config["model"], "actual_model": None,
                "requested_service_tier": self.config["service_tier"], "actual_service_tier": None,
                "pricing": self.config["pricing"], "error_code": None}

    async def analyze(self, digest: dict[str, Any]) -> dict[str, Any]:
        config = self.config
        ip = {"name": config["input_policy"], "status": "accepted"}
        op = {"name": config["output_policy"], "status": "not_checked"}
        metadata = self.metadata(digest)
        result = dict(status="error", text="", usage=None, usage_status="not_requested",
                      cost_usd=None, input_policy=ip, output_policy=op, metadata=metadata)
        prompt, problem, context = _input(digest, config["max_input_chars"])
        if problem:
            result.update(status="rejected", input_policy={**ip, "status": "rejected"})
            metadata["error_code"] = problem
            return result
        metadata["context"] = context
        if not self.api_key or not all(33 <= ord(char) <= 126 for char in self.api_key):
            metadata["error_code"] = "not_configured"
            return result
        payload = {"model": config["model"], "service_tier": config["service_tier"],
                   "reasoning": {"effort": config["reasoning_effort"]},
                   "max_output_tokens": config["max_output_tokens"],
                   "instructions": config["instructions"],
                   "input": [{"role": "user", "content": prompt}]}
        result["usage_status"] = "unavailable"
        try:
            if self.client is None:
                async with httpx.AsyncClient() as client:
                    response = await client.post(API_URL, headers={"Authorization": f"Bearer {self.api_key}"},
                                                 json=payload, timeout=config["timeout_seconds"], follow_redirects=False)
            else:
                response = await self.client.post(API_URL, headers={"Authorization": f"Bearer {self.api_key}"},
                                                  json=payload, timeout=config["timeout_seconds"], follow_redirects=False)
            if not response.is_success:
                metadata["error_code"] = "authentication" if response.status_code in (401, 403) else "provider_error"
                metadata["http_status"] = response.status_code
                return result
            body = response.json()
        except (httpx.TimeoutException, httpx.RequestError):
            metadata["error_code"] = "network_error"
            return result
        except (ValueError, UnicodeError):
            metadata["error_code"] = "invalid_response"
            return result
        usage = _usage(body)
        result["usage"] = usage
        result["usage_status"] = "reported" if usage is not None else "unavailable"
        result["cost_usd"] = _cost(body, usage, config)
        if isinstance(body, dict):
            metadata["actual_model"] = body.get("model")
            metadata["actual_service_tier"] = body.get("service_tier")
            metadata["provider_request_id"] = body.get("id")
        value, problem = _text(body)
        if problem:
            result.update(status="rejected", output_policy={**op, "status": "rejected"})
            metadata["error_code"] = problem
        else:
            result.update(status="ok", text=value, output_policy={**op, "status": "accepted"})
        return result

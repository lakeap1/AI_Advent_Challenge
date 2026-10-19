"""Single GLM transport boundary with durable accounting before network I/O."""

import hashlib
import json


class ProviderError(RuntimeError):
    """User-safe generation failure; sensitive transport details stay hidden."""


def _counter(value):
    return value if type(value) is int and value >= 0 else None


class GLMProvider:
    def __init__(self, settings, store, transport=None):
        self.settings = settings
        self.store = store
        self._default_transport = transport is None
        self.transport = transport or self._http_transport

    def _http_transport(self, payload):
        import httpx

        with httpx.Client(trust_env=False, follow_redirects=False,
                          timeout=httpx.Timeout(60.0, connect=10.0)) as client:
            response = client.post(self.settings.api_url, json=payload,
                                   headers={"Authorization": f"Bearer {self.settings.api_key}"})
            response.raise_for_status()
            return response.json()

    def _usage(self, raw):
        if not isinstance(raw, dict):
            return None
        inp = _counter(raw.get("prompt_tokens"))
        out = _counter(raw.get("completion_tokens"))
        total = _counter(raw.get("total_tokens"))
        input_details = raw.get("prompt_tokens_details") or raw.get("input_tokens_details")
        if not isinstance(input_details, dict) or "cached_tokens" not in input_details:
            return None
        cached = _counter(input_details.get("cached_tokens"))
        if None in (inp, out, total, cached) or cached > inp or total != inp + out:
            return None
        output_details = raw.get("completion_tokens_details") or raw.get("output_tokens_details") or {}
        reasoning = _counter(output_details.get("reasoning_tokens")) if isinstance(output_details, dict) else None
        if reasoning is not None and reasoning > out:
            return None
        return {"input_tokens": inp, "output_tokens": out, "total_tokens": total,
                "cached_input_tokens": cached, "reasoning_tokens": reasoning}

    def _cost(self, usage):
        s = self.settings
        return round(((usage["input_tokens"] - usage["cached_input_tokens"])
                      * s.price_input_per_million
                      + usage["cached_input_tokens"] * s.price_cached_per_million
                      + usage["output_tokens"] * s.price_output_per_million) / 1_000_000, 12)

    def call(self, user_id: int, branch_id: str, turn_id: str, stage: str,
             messages: list[dict], max_tokens: int | None = None,
             json_mode: bool = True) -> dict:
        if not self.settings.api_key:
            raise ProviderError("Модель сейчас недоступна")
        limit = self.settings.max_tokens if max_tokens is None else max_tokens
        if type(limit) is not int or not 1 <= limit <= self.settings.max_tokens:
            raise ProviderError("Недопустимый размер ответа")
        if not isinstance(messages, list) or not all(isinstance(m, dict) for m in messages):
            raise ProviderError("Некорректный контекст запроса")
        if self._default_transport:
            # A missing local dependency cannot have consumed remote model tokens.
            # Detect it before creating a pending financial receipt.
            try:
                import httpx  # noqa: F401
            except ImportError:
                raise ProviderError("HTTP-клиент модели не установлен в среде сервиса") from None
        payload = {"model": self.settings.model, "messages": messages,
                   "max_tokens": limit, "stream": False,
                   "thinking": {"type": "enabled"}, "reasoning_effort": "low"}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        else:
            payload["messages"] = [{"role": "system", "content":
                                    "Отвечай обычным текстом с абзацами, без Markdown, если пользователь явно не запросил другой формат."}] + messages
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > self.settings.input_budget:
            raise ProviderError("Запрос превышает бюджет контекста")
        reserve = ((len(encoded) + 1000) * self.settings.price_input_per_million
                   + limit * self.settings.price_output_per_million) / 1_000_000
        metadata = {"request_sha256": hashlib.sha256(encoded).hexdigest(),
                    "request_bytes": len(encoded),
                    "operational_input_budget_bytes": self.settings.input_budget,
                    "request_options": {key: value for key, value in payload.items() if key != "messages"},
                    "tariff_source": self.settings.tariff_source,
                    "tariff_checked_on": self.settings.tariff_checked_on,
                    "price_input_per_million": self.settings.price_input_per_million,
                    "price_cached_per_million": self.settings.price_cached_per_million,
                    "price_output_per_million": self.settings.price_output_per_million,
                    "service_mode": "standard_api", "bundle_debit_usd": None,
                    "max_tokens": limit}
        try:
            call_id = self.store.start_call(user_id, branch_id, turn_id, stage,
                                            self.settings.model, metadata,
                                            reserve_usd=reserve, budget_usd=self.settings.budget_usd)
        except (RuntimeError, PermissionError, ValueError) as exc:
            raise ProviderError(str(exc)) from None
        try:
            self.store.set_call_policy(call_id, input_policy={"ok": True,
                                                               "code": "bounded_provider_payload"})
            self.store.mark_call_dispatched(call_id)
            raw = self.transport(payload)
        except Exception:
            self.store.finish_call(call_id, None, None, status="unknown")
            raise ProviderError("Не удалось получить ответ модели; стоимость вызова неизвестна") from None
        if not isinstance(raw, dict) or raw.get("model", self.settings.model) != self.settings.model:
            self.store.finish_call(call_id, None, None, status="unknown")
            raise ProviderError("Ответ модели нельзя проверить; стоимость вызова неизвестна")
        usage = self._usage(raw.get("usage"))
        choices = raw.get("choices")
        content = None
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            message = choices[0].get("message")
            if isinstance(message, dict):
                content = message.get("content")
        if usage is None or not isinstance(content, str) or not content.strip():
            self.store.finish_call(call_id, usage, self._cost(usage) if usage else None,
                                   status="unknown")
            raise ProviderError("Ответ модели нельзя проверить; стоимость вызова неизвестна")
        cost = self._cost(usage)
        self.store.finish_call(call_id, usage, cost)
        return {"content": content, "usage": usage, "call_id": call_id,
                "cost_estimated_usd": cost}

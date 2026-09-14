"""Независимый агент: публичный контракт, политики и цикл запроса."""

from dataclasses import dataclass, replace
from typing import Literal, Protocol

from .config import AgentConfig
from .transport import TransportError
from .usage import TokenUsage, read_usage, estimate_cost


class Transport(Protocol):
    def create(self, payload: dict, timeout: float) -> object: ...


@dataclass(frozen=True)
class AgentResult:
    status: Literal["ok", "rejected", "error"]
    text: str
    code: str = ""
    usage: TokenUsage | None = None
    cost_usd: str | None = None
    usage_status: Literal["not_requested", "unavailable", "reported"] = "not_requested"


MESSAGES = {
    "input_invalid": "Введите непустой текст запроса.",
    "not_configured": "API-ключ не настроен. Добавьте OPENAI_API_KEY в .env и перезапустите приложение.",
    "authentication": "Провайдер отклонил API-ключ или доступ к модели. Проверьте настройки доступа.",
    "rate_limit": "Достигнут лимит API. Повторите запрос позже или проверьте баланс у провайдера.",
    "timeout": "Модель не ответила за отведённое время. Попробуйте отправить запрос ещё раз.",
    "connection": "Не удалось связаться с моделью. Проверьте соединение и повторите запрос.",
    "provider_error": "Провайдер не смог обработать запрос. Проверьте настройки модели или повторите позже.",
    "invalid_response": "Провайдер вернул ответ в неожиданном формате. Попробуйте ещё раз.",
    "incomplete": "Генерация не завершена. Сократите запрос или увеличьте лимит ответа в конфиге агента.",
    "refusal": "Модель отказалась отвечать на этот запрос. Попробуйте переформулировать вопрос.",
    "empty_output": "Модель вернула пустой ответ. Попробуйте уточнить вопрос.",
}


def failure(code: str, *, rejected=False) -> AgentResult:
    safe_code = code if code in MESSAGES else "provider_error"
    return AgentResult("rejected" if rejected else "error", MESSAGES[safe_code], safe_code)


def nonempty_text(prompt: object, config: AgentConfig) -> str | AgentResult:
    if not isinstance(prompt, str) or not prompt.strip():
        return failure("input_invalid", rejected=True)
    try:
        prompt.encode("utf-8")
    except UnicodeEncodeError:
        return failure("input_invalid", rejected=True)
    text = prompt.strip()
    if len(text) > config.max_input_chars:
        return AgentResult("rejected", f"Запрос слишком длинный. Допустимо до {config.max_input_chars} символов.", "input_too_long")
    return text


def completed_text(response: object) -> AgentResult:
    if not isinstance(response, dict):
        return failure("invalid_response")
    if response.get("status") == "incomplete":
        return failure("incomplete")
    if response.get("status") == "failed" or response.get("error"):
        return failure("provider_error")
    if response.get("status") != "completed" or not isinstance(response.get("output"), list):
        return failure("invalid_response")
    fragments = []
    for item in response["output"]:
        if not isinstance(item, dict):
            return failure("invalid_response")
        if item.get("type") == "reasoning":
            continue
        if item.get("type") != "message" or item.get("role") != "assistant":
            return failure("invalid_response")
        if item.get("status") == "incomplete":
            return failure("incomplete")
        if item.get("status") != "completed" or not isinstance(item.get("content"), list):
            return failure("invalid_response")
        for block in item["content"]:
            if not isinstance(block, dict):
                return failure("invalid_response")
            if block.get("type") == "refusal":
                return failure("refusal")
            if block.get("type") != "output_text" or not isinstance(block.get("text"), str):
                return failure("invalid_response")
            try:
                block["text"].encode("utf-8")
            except UnicodeEncodeError:
                return failure("invalid_response")
            fragments.append(block["text"])
    text = "".join(fragments).strip()
    return AgentResult("ok", text) if text else failure("empty_output")


class Agent:
    def __init__(self, config: AgentConfig, transport: Transport):
        self._config = config
        self._transport = transport
        self._input_policy = {"nonempty_text": nonempty_text}[config.input_policy]
        self._output_policy = {"completed_text": completed_text}[config.output_policy]

    def run(self, prompt: object) -> AgentResult:
        accepted = self._input_policy(prompt, self._config)
        if isinstance(accepted, AgentResult):
            return accepted
        payload = {
            "model": self._config.model,
            "instructions": self._config.instructions,
            "input": accepted,
            "reasoning": {"effort": self._config.reasoning_effort},
            "max_output_tokens": self._config.max_output_tokens,
            "store": False,
            "service_tier": self._config.service_tier,
        }
        try:
            response = self._transport.create(payload, self._config.timeout_seconds)
        except TransportError as exc:
            return replace(failure(exc.code), usage_status="not_requested" if exc.code == "not_configured" else "unavailable")
        usage = read_usage(response)
        return replace(
            self._output_policy(response), usage=usage,
            cost_usd=estimate_cost(response, usage, self._config),
            usage_status="reported" if usage is not None else "unavailable",
        )

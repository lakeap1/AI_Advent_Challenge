"""Независимый агент: публичный контракт, политики и цикл запроса."""

from dataclasses import asdict, dataclass, field, replace
from threading import RLock
from typing import Literal, Protocol

from .config import AgentConfig
from .storage import SQLiteStore
from .transport import TransportError
from .usage import TokenUsage, read_usage, estimate_cost
from .tokens import TokenCounter, accounting


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
    request_id: int | None = None
    input_policy: dict[str, str] = field(default_factory=dict)
    output_policy: dict[str, str] = field(default_factory=dict)


MESSAGES = {
    "context_length_exceeded": "Контекст превышает лимит модели: API отклонил запрос. Ответ не создан. История сохранена полностью; следующий запрос с ней тоже может не поместиться. Для нового чата используйте отдельную базу данных. Автоматической обрезки нет.",
    "insufficient_quota": "Исчерпана квота или баланс API. Это не переполнение контекста. Проверьте аккаунт провайдера.",
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
    def __init__(self, config: AgentConfig, transport: Transport, store=None):
        self._config = config
        self._transport = transport
        self._store = SQLiteStore() if store is None else store
        self._lock = RLock()
        self._counter = TokenCounter()
        self._input_policy = {"nonempty_text": nonempty_text}[config.input_policy]
        self._output_policy = {"completed_text": completed_text}[config.output_policy]

    def _metadata(self, response=None):
        actual = response if isinstance(response, dict) else {}
        return {
            "requested_model": self._config.model,
            "actual_model": actual.get("model") if isinstance(actual.get("model"), str) else None,
            "requested_service_tier": self._config.service_tier,
            "actual_service_tier": actual.get("service_tier") if isinstance(actual.get("service_tier"), str) else None,
            "pricing": asdict(self._config.pricing) if self._config.pricing is not None else None,
        }

    @staticmethod
    def _record(result, metadata):
        record = asdict(result)
        record.pop("request_id")
        record["metadata"] = metadata
        return record

    def state(self):
        with self._lock:
            state = self._store.state()
            state['token_accounting'] = accounting(state['requests'])
            state['context'] = {
                **self._counter.description,
                'history_tokens_estimate': self._counter.history(state['messages']),
                'context_window': self._config.context_window,
                'max_input_tokens': self._config.max_input_tokens,
                'max_output_tokens': self._config.max_output_tokens,
                'source': self._config.limits_source, 'checked_at': self._config.limits_checked_at,
                'model': self._config.model,
            }
            return state

    def preview(self, prompt):
        with self._lock:
            accepted = self._input_policy(prompt, self._config)
            if isinstance(accepted, AgentResult):
                return {'status': 'rejected', 'code': accepted.code, 'text': accepted.text}
            return {'status': 'ok', 'token_metrics': self._measure(accepted)}

    def _measure(self, text):
        return {**self._counter.measure(text, self._store.state()['messages'], self._config.instructions),
                'context_window': self._config.context_window, 'max_input_tokens': self._config.max_input_tokens,
                'max_output_tokens': self._config.max_output_tokens,
                'limits_source': self._config.limits_source, 'limits_checked_at': self._config.limits_checked_at}

    def close(self):
        with self._lock:
            self._store.close()

    def run(self, prompt: object) -> AgentResult:
        with self._lock:
            return self._run(prompt)

    def _run(self, prompt):
        accepted = self._input_policy(prompt, self._config)
        input_policy = {"name": self._config.input_policy, "status": "accepted"}
        output_policy = {"name": self._config.output_policy, "status": "not_checked"}
        metadata = self._metadata()
        if isinstance(accepted, AgentResult):
            result = replace(accepted, input_policy={**input_policy, "status": "rejected"}, output_policy=output_policy)
            request_id = self._store.begin(self._record(result, metadata))
            return replace(result, request_id=request_id)
        metrics = self._measure(accepted)
        metadata['token_metrics'] = metrics
        pending = {
            "status": "pending", "text": "Ожидается результат запроса.", "code": "",
            "usage": None, "cost_usd": None, "usage_status": "unavailable",
            "input_policy": input_policy, "output_policy": output_policy, "metadata": metadata,
        }
        request_id = self._store.begin(pending, accepted)
        history = self._store.state()["messages"]
        payload = {
            "model": self._config.model,
            "instructions": self._config.instructions,
            "input": [{"role": message["role"], "content": message["content"]} for message in history],
            "reasoning": {"effort": self._config.reasoning_effort},
            "max_output_tokens": self._config.max_output_tokens,
            "store": False,
            "truncation": "disabled",
            "service_tier": self._config.service_tier,
        }
        try:
            response = self._transport.create(payload, self._config.timeout_seconds)
        except TransportError as exc:
            result = replace(failure(exc.code), usage_status="unavailable" if exc.request_started else "not_requested")
            metadata['provider_error'] = {'http_status': exc.http_status, 'code': exc.provider_code}
        else:
            usage = read_usage(response)
            checked = self._output_policy(response)
            output_policy = {**output_policy, "status": "accepted" if checked.status == "ok" else "rejected"}
            metadata = self._metadata(response)
            metadata['token_metrics'] = metrics
            result = replace(
                checked, usage=usage, cost_usd=estimate_cost(response, usage, self._config),
                usage_status="reported" if usage is not None else "unavailable",
            )
        result = replace(result, request_id=request_id, input_policy=input_policy, output_policy=output_policy)
        self._store.finish(request_id, self._record(result, metadata), result.text if result.status == "ok" else None)
        return result

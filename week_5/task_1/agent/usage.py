"""Статистика провайдера и оценка стоимости; неизвестные данные не равны нулю."""
from dataclasses import dataclass
from decimal import Decimal

from .config import AgentConfig


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cached_input_tokens: int | None
    reasoning_tokens: int | None
    cache_write_input_tokens: int | None = None


def counter(value):
    return value if type(value) is int and value >= 0 else None


def read_usage(response: object) -> TokenUsage | None:
    usage = response.get('usage') if isinstance(response, dict) else None
    if not isinstance(usage, dict):
        return None
    incoming, outgoing, total = [counter(usage.get(key)) for key in ('input_tokens', 'output_tokens', 'total_tokens')]
    if any(value is None for value in (incoming, outgoing, total)) or total != incoming + outgoing:
        return None
    input_details = usage.get('input_tokens_details')
    output_details = usage.get('output_tokens_details')
    cached = counter(input_details.get('cached_tokens')) if isinstance(input_details, dict) else None
    written = counter(input_details.get('cache_write_tokens')) if isinstance(input_details, dict) else None
    reasoning = counter(output_details.get('reasoning_tokens')) if isinstance(output_details, dict) else None
    if cached is not None and cached > incoming:
        cached = None
    if written is not None and (written > incoming or (cached is not None and written + cached > incoming)):
        written = None
    if reasoning is not None and reasoning > outgoing:
        reasoning = None
    return TokenUsage(incoming, outgoing, total, cached, reasoning, written)


def estimate_cost(response: object, usage: TokenUsage | None, config: AgentConfig) -> str | None:
    pricing = config.pricing
    if not isinstance(response, dict) or usage is None or pricing is None:
        return None
    if pricing.model != config.model or response.get('model') != pricing.model or response.get('service_tier') != 'default':
        return None
    cached = usage.cached_input_tokens
    written = usage.cache_write_input_tokens
    if cached is None or written is None or usage.input_tokens > pricing.max_input_tokens:
        return None
    # Запись кеша имеет отдельный тариф: неизвестную схему не считаем обычным входом.
    details = response['usage'].get('input_tokens_details', {})
    if any(value for key, value in details.items() if key != 'cache_write_tokens' and ('writ' in key or 'creat' in key)):
        return None
    if written and pricing.cache_write_usd_per_million is None:
        return None
    amount = (
        (usage.input_tokens - cached - written) * Decimal(pricing.input_usd_per_million)
        + cached * Decimal(pricing.cached_input_usd_per_million)
        + written * Decimal(pricing.cache_write_usd_per_million or '0')
        + usage.output_tokens * Decimal(pricing.output_usd_per_million)
    ) / Decimal(1_000_000)
    # Reasoning уже включён в output_tokens, повторно его не прибавляем.
    return format(amount.normalize(), 'f')

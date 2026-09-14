"""Загрузка и ранняя проверка конфигурации агента."""

import math
import tomllib
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path


@dataclass(frozen=True)
class Pricing:
    model: str
    input_usd_per_million: str
    cached_input_usd_per_million: str
    output_usd_per_million: str
    checked_at: str
    source: str
    max_input_tokens: int = 272000

    def __post_init__(self):
        for name in ('model', 'checked_at', 'source'):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError(f'Тариф: {name} должен быть непустой строкой.')
        for name in ('input_usd_per_million', 'cached_input_usd_per_million', 'output_usd_per_million'):
            try:
                raw = getattr(self, name)
                if not isinstance(raw, str):
                    raise ValueError('Тариф задаётся десятичной строкой.')
                value = Decimal(raw)
                if not value.is_finite() or value < 0:
                    raise ValueError('Тариф должен быть конечным неотрицательным числом.')
            except (InvalidOperation, TypeError):
                raise ValueError(f'Некорректный тариф {name}.') from None
        if type(self.max_input_tokens) is not int or self.max_input_tokens <= 0:
            raise ValueError('Тариф: max_input_tokens должен быть положительным целым числом.')


@dataclass(frozen=True)
class AgentConfig:
    model: str
    reasoning_effort: str
    max_output_tokens: int
    timeout_seconds: float
    instructions: str
    input_policy: str
    max_input_chars: int
    output_policy: str
    judge: str
    service_tier: str = "default"
    pricing: Pricing | None = None

    def __post_init__(self):
        for name in ("model", "instructions"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Конфиг: {name} должен быть непустой строкой.")
            try:
                value.encode("utf-8")
            except UnicodeEncodeError:
                raise ValueError(f"Конфиг: некорректный Unicode в {name}.") from None
        for name in ("max_output_tokens", "max_input_chars"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"Конфиг: {name} должен быть положительным целым числом.")
        if type(self.timeout_seconds) not in (int, float) or not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("Конфиг: timeout_seconds должен быть положительным конечным числом.")
        for name, allowed in (
            ("reasoning_effort", ("none", "minimal", "low", "medium", "high", "xhigh")),
            ("input_policy", ("nonempty_text",)),
            ("output_policy", ("completed_text",)),
            ("judge", ("disabled",)),
            ("service_tier", ("default",)),
        ):
            if getattr(self, name) not in allowed:
                raise ValueError(f"Конфиг: неподдерживаемое значение {name}.")

        if self.pricing is not None and not isinstance(self.pricing, Pricing):
            raise ValueError("Конфиг: pricing должен содержать тариф модели.")


def load_config(path=None) -> AgentConfig:
    source = Path(path) if path is not None else Path(__file__).with_name("config.toml")
    try:
        with source.open("rb") as stream:
            data = tomllib.load(stream)
        if "pricing" in data:
            data["pricing"] = Pricing(**data["pricing"])
        return AgentConfig(**data)
    except (OSError, TypeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError("Не удалось загрузить конфиг агента: проверьте файл, поля и синтаксис TOML.") from exc

"""Публичная точка входа агента; не зависит от Flask и интерфейса."""

import os

from .config import AgentConfig, load_config
from .core import Agent, AgentResult
from .transport import ResponsesTransport

__all__ = ["Agent", "AgentConfig", "AgentResult", "create_agent", "load_config"]


def create_agent(config_path=None, api_key=None) -> Agent:
    key = os.getenv("OPENAI_API_KEY") if api_key is None else api_key
    return Agent(load_config(config_path), ResponsesTransport(key))

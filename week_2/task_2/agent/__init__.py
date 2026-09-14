"""Публичная точка входа агента; не зависит от Flask и интерфейса."""

import os

from .config import AgentConfig, load_config
from .core import Agent, AgentResult
from .transport import ResponsesTransport
from .storage import SQLiteStore, StorageError

__all__ = ["Agent", "AgentConfig", "AgentResult", "create_agent", "load_config", "SQLiteStore", "StorageError"]


def create_agent(config_path=None, api_key=None, db_path=None) -> Agent:
    key = os.getenv("OPENAI_API_KEY") if api_key is None else api_key
    return Agent(load_config(config_path), ResponsesTransport(key), SQLiteStore(db_path))

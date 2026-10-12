"""Pinned runtime and tariff configuration."""

from dataclasses import dataclass
from pathlib import Path
import tomllib


@dataclass(frozen=True)
class Config:
    local: dict
    retrieval: dict
    cloud: dict
    policy: dict


def load_config() -> Config:
    raw = tomllib.loads(Path(__file__).with_name('config.toml').read_text(encoding='utf-8'))
    return Config(raw['local'], raw['retrieval'], raw['cloud'], raw['policy'])

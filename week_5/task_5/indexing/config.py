"""Validated immutable indexing contract and tariff provenance."""
from dataclasses import dataclass
import math
from pathlib import Path
import tomllib


@dataclass(frozen=True)
class Config:
    model: str = "text-embedding-3-small"
    dimensions: int = 1536
    batch_size: int = 16
    chunk_size: int = 1800
    overlap: int = 180
    min_corpus_characters: int = 54000
    timeout_seconds: float = 60.0
    input_usd_per_million: float = 0.02
    service_tier: str = "standard"
    tariff_source: str = "https://developers.openai.com/api/docs/models/text-embedding-3-small"
    tariff_verified_on: str = "2026-10-03"
    input_policy: str = "manifest_utf8_safe_paths_min_corpus_v1"
    output_policy: str = "indexed_finite_nonzero_dimensions_v1"

    def validate(self):
        for name in ("dimensions", "batch_size", "chunk_size", "overlap", "min_corpus_characters"):
            value = getattr(self, name)
            if type(value) is not int or value < (0 if name in ("overlap", "min_corpus_characters") else 1):
                raise ValueError(f"invalid indexing config: {name}")
        if self.overlap >= self.chunk_size:
            raise ValueError("overlap must be smaller than chunk_size")
        if type(self.timeout_seconds) not in (int, float) or not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("invalid indexing config: timeout_seconds")
        if type(self.input_usd_per_million) not in (int, float) or not math.isfinite(self.input_usd_per_million) or self.input_usd_per_million <= 0:
            raise ValueError("invalid indexing config: tariff")
        if self.model != "text-embedding-3-small" or self.dimensions != 1536:
            raise ValueError("unsupported embedding model or dimensions")
        if self.input_policy != "manifest_utf8_safe_paths_min_corpus_v1" or self.output_policy != "indexed_finite_nonzero_dimensions_v1":
            raise ValueError("unsupported input or output policy")
        return self

    @property
    def tariff(self):
        return {"input_usd_per_million": self.input_usd_per_million,
                "service_tier": self.service_tier,
                "source": self.tariff_source,
                "verified_on": self.tariff_verified_on}

    @property
    def contract(self):
        return {"model": self.model, "dimensions": self.dimensions,
                "batch_size": self.batch_size, "chunk_size": self.chunk_size,
                "overlap": self.overlap, "tariff": self.tariff,
                "input_policy": self.input_policy, "output_policy": self.output_policy}


def load_config(path=None):
    config_path = Path(path) if path is not None else Path(__file__).with_name("config.toml")
    with config_path.open("rb") as stream:
        values = tomllib.load(stream)
    return Config(**values).validate()

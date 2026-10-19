"""Explicit, server-side configuration for the standalone service."""

from dataclasses import dataclass
from pathlib import Path
import os


@dataclass(frozen=True)
class Settings:
    app_root: Path
    data_dir: Path
    api_key: str = ""
    secret_key: str = ""
    secure_cookies: bool = True
    model: str = "glm-5.3-flash"
    api_url: str = "https://api.z.ai/api/paas/v4/chat/completions"
    embedding_url: str = "http://127.0.0.1:11436"
    embedding_model: str = "qwen3-embedding:0.6b"
    embedding_dimensions: int = 1024
    embedding_threads: int = 2
    repo_root: Path | None = None
    budget_usd: float = 1.0
    input_budget: int = 32000
    history_budget: int = 10000
    summary_trigger_ratio: float = 0.75
    summary_budget: int = 3000
    sources_budget: int = 9000
    tools_budget: int = 1500
    explicit_budget: int = 4000
    max_tokens: int = 4096
    documented_context_window: int = 1_000_000
    price_input_per_million: float = 0.15
    price_cached_per_million: float = 0.03
    price_output_per_million: float = 0.50
    tariff_source: str = "https://docs.z.ai/guides/overview/pricing"
    tariff_checked_on: str = "2026-10-19"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "service.sqlite3"

    @classmethod
    def from_env(cls, base_dir: Path | None = None) -> "Settings":
        root = Path(base_dir).resolve() if base_dir is not None else Path(__file__).resolve().parents[1]
        data = Path(os.environ.get("GRAPHICS_DATA_DIR", str(root / "data"))).resolve()
        repo = Path(os.environ.get("GRAPHICS_REPO_ROOT", str(root.parents[1]))).resolve()
        return cls(
            app_root=root, data_dir=data,
            api_key=os.environ.get("ZAI_API_KEY", ""),
            secret_key=os.environ.get("GRAPHICS_SECRET_KEY", ""),
            secure_cookies=os.environ.get("GRAPHICS_SECURE_COOKIES", "1") != "0",
            repo_root=repo,
            embedding_url=os.environ.get("GRAPHICS_EMBEDDING_URL", "http://127.0.0.1:11436"),
            budget_usd=float(os.environ.get("GRAPHICS_BUDGET_USD", "1")),
        )


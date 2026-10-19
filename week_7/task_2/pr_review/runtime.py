"""Explicit dependency on the trusted Day31 service, never on PR paths."""
import importlib
from pathlib import Path
import sys


def run_review(snapshot, run_key, *, data_dir, service_root=None):
    root = (Path(service_root) if service_root is not None else
            Path(__file__).resolve().parents[2] / "task_1").resolve()
    for name in ("config.py", "provider.py", "store.py", "validation.py"):
        if not (root / "graphics" / name).is_file():
            raise ValueError("trusted_service_dependency_missing")
    existing = sys.modules.get("graphics")
    if existing is not None and not Path(existing.__file__).resolve().is_relative_to(root):
        raise ValueError("conflicting_service_dependency")
    sys.path.insert(0, str(root))
    settings = importlib.import_module("graphics.config").Settings.from_env(root)
    runner_class = importlib.import_module("pr_review.pipeline").ReviewRunner
    return runner_class(settings, Path(data_dir).resolve()).run(snapshot, run_key)

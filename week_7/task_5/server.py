"""WSGI entry point for the completed graphics learning site."""

import sys
from pathlib import Path

_WEEK = Path(__file__).resolve().parents[1]
for _task in ("task_1", "task_4"):
    _path = str(_WEEK / _task)
    if _path not in sys.path:
        sys.path.insert(0, _path)

from learning_product.web import create_app  # noqa: E402

app = create_app()

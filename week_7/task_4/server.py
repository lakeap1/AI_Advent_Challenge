"""WSGI entry point: day31 site with private learning-file workflows."""

import sys
from pathlib import Path

_BASE = Path(__file__).resolve().parents[1] / "task_1"
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))

from learning_files.web import create_app  # noqa: E402

app = create_app()

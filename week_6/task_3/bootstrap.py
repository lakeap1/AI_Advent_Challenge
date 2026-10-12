"""Explicit dependency on the preserved Day 27 application and corpus code."""

import importlib.util
from pathlib import Path
import sys


TASK_ROOT = Path(__file__).resolve().parent
BASE_ROOT = TASK_ROOT.parent / 'task_2'

if str(BASE_ROOT) not in sys.path:
    sys.path.append(str(BASE_ROOT))


def load_day27_app():
    module_name = '_day27_app_for_day28'
    module = sys.modules.get(module_name)
    if module is None:
        spec = importlib.util.spec_from_file_location(module_name, BASE_ROOT / 'app.py')
        if spec is None or spec.loader is None:
            raise RuntimeError('Day 27 application unavailable')
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    return module

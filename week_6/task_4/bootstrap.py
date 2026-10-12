"""Load Day 28 with its own bootstrap while keeping Day 29 paths explicit."""

import importlib.util
import sys
from pathlib import Path


TASK_ROOT = Path(__file__).resolve().parent
DAY28_ROOT = TASK_ROOT.parent / 'task_3'
BASE_ROOT = TASK_ROOT.parent / 'task_2'


def load_day28_app():
    name = '_day28_app_for_day29'
    if name in sys.modules:
        return sys.modules[name]
    for path in (BASE_ROOT, DAY28_ROOT):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    legacy_name = '_day28_bootstrap_for_day29'
    spec = importlib.util.spec_from_file_location(legacy_name, DAY28_ROOT / 'bootstrap.py')
    if spec is None or spec.loader is None:
        raise RuntimeError('Day 28 bootstrap unavailable')
    legacy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(legacy)
    app_spec = importlib.util.spec_from_file_location(name, DAY28_ROOT / 'app.py')
    if app_spec is None or app_spec.loader is None:
        raise RuntimeError('Day 28 application unavailable')
    module = importlib.util.module_from_spec(app_spec)
    previous = sys.modules.get('bootstrap')
    sys.modules['bootstrap'] = legacy
    try:
        app_spec.loader.exec_module(module)
    finally:
        if previous is None:
            sys.modules.pop('bootstrap', None)
        else:
            sys.modules['bootstrap'] = previous
    sys.modules[name] = module
    return module

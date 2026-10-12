"""Explicit, narrow reuse of the day-27 local agent."""

from pathlib import Path
import sys


def build_chat(data_dir, config_path, transport):
    task_2 = Path(__file__).resolve().parents[1] / 'task_2'
    if str(task_2) not in sys.path:
        sys.path.insert(0, str(task_2))
    from local_agent import LocalChat
    return LocalChat(data_dir, config_path=config_path, transport=transport)

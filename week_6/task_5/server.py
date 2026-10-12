"""Loopback-only Waitress entry point for the private service."""

from pathlib import Path
import os
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from waitress import serve
from week_6.task_5.app import create_app


if __name__ == '__main__':
    port = int(os.environ.get('DAY30_API_PORT', '5030'))
    if not 1 <= port <= 65535:
        raise ValueError('DAY30_API_PORT должен быть допустимым TCP-портом.')
    serve(create_app(), host='127.0.0.1', port=port, threads=8,
          max_request_body_size=32000)

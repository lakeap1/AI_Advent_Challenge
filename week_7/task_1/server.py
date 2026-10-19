"""Gunicorn entry point. Configuration is read at startup and fails closed."""

from app import create_app

app = create_app()

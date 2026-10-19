"""WSGI application factory for tests and local integration checks."""

from graphics.web import create_app

__all__ = ["create_app"]

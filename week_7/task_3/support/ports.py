"""Contracts for the optional authenticated Day31 Store binding."""

from typing import Protocol

class UserContextPort(Protocol):
    def call(self, name: str, arguments: dict) -> dict: ...


class DiagnosticPort(Protocol):
    def diagnostics(self) -> dict: ...

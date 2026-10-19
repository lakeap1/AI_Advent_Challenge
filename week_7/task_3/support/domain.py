"""Domain values shared by the storage and MCP boundaries."""

from dataclasses import dataclass


ERROR_CODES = frozenset({
    "invalid_argument",
    "not_found",
    "closed",
    "idempotency_conflict",
    "limit_exceeded",
    "storage_unavailable",
})


@dataclass(frozen=True)
class Actor:
    user_id: str


class SupportError(Exception):
    def __init__(self, code: str, message: str):
        if code not in ERROR_CODES:
            raise ValueError("Unknown support error code")
        self.code = code
        self.message = message
        super().__init__(message)

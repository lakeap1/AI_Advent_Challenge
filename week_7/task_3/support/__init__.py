"""Private support storage and future integration ports."""

from .domain import Actor, SupportError
from .tickets import TicketStore

__all__ = ["Actor", "SupportError", "TicketStore"]

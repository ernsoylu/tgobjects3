"""TelegramPort protocol, typed errors, and Telethon adapter."""

from .adapter import TelethonAdapter
from .errors import FloodWaitError, TelegramError, TelegramUnavailableError
from .port import SessionState, TelegramMessage, TelegramPort

__all__ = [
    "FloodWaitError",
    "SessionState",
    "TelegramError",
    "TelegramMessage",
    "TelegramPort",
    "TelegramUnavailableError",
    "TelethonAdapter",
]

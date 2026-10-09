"""Typed exceptions raised across the gateway's Telegram boundary.

These are the *only* error types the rest of the gateway sees from Telegram
operations. The Telethon adapter (task 10.2) catches raw ``telethon.errors.*``
exceptions and re-raises them as instances of the classes defined here, so no
Telethon-specific exception ever leaks past the :class:`TelegramPort` boundary
(requirement 3.4). The reconnection machinery (task 10.3) imports these same
classes — in particular it raises :class:`TelegramUnavailableError` to every
caller while a session is reconnecting (requirement 3.3) — so they live in this
dedicated module rather than in any single adapter so both can share them
without a circular import.

Error taxonomy:

- :class:`TelegramError` — base class for everything in this module; catch it
  to handle any Telegram-boundary failure generically.
- :class:`TelegramUnavailableError` — the session cannot currently serve a
  call: a connection or RPC-level transport failure, or a reconnection in
  progress. Callers translate this to an S3/admin ``503`` without mutating
  committed state.
- :class:`FloodWaitError` — Telegram applied rate limiting and asked the client
  to wait before retrying. Carries :attr:`FloodWaitError.retry_after`, the
  number of seconds Telegram requested, so retry/back-off logic can honour it.
"""

from __future__ import annotations


class TelegramError(Exception):
    """Base class for all errors surfaced at the gateway's Telegram boundary."""


class TelegramUnavailableError(TelegramError):
    """The Telegram session cannot currently serve a request.

    Raised when a connection cannot be established, an in-flight call fails at
    the transport/RPC level, or the session is mid-reconnection (requirements
    3.2, 3.3). Callers surface this as a ``503`` without modifying any
    previously committed state.
    """


class FloodWaitError(TelegramError):
    """Telegram rate-limited the client and asked it to wait before retrying.

    Mirrors Telethon's own flood-wait signal but as a gateway-owned type.

    Attributes:
        retry_after: Number of seconds Telegram requested the client wait
            before issuing the same request again.
    """

    def __init__(self, retry_after: int, message: str | None = None) -> None:
        self.retry_after = retry_after
        super().__init__(
            message
            if message is not None
            else f"Telegram flood wait: retry after {retry_after}s"
        )

"""The :class:`TelegramPort` protocol — the gateway's only boundary to Telegram.

Every Telegram operation the rest of the gateway performs — sending data chunks,
reading messages back, downloading documents, managing bucket channels, and
checking session health — passes through this interface. No other module imports
Telethon directly, so the concrete transport (the Telethon adapter, task 10.2)
can be swapped for a fake in tests or replaced wholesale without touching
callers (requirement 3.4).

This module defines only the *shape* of that boundary: the :class:`TelegramPort`
``Protocol`` and the two supporting value types its methods exchange —
:class:`TelegramMessage` (a decoded channel message plus the document metadata
the Recovery CLI needs to re-fetch content) and :class:`SessionState` (the
connection/authentication state of the single MTProto session). The adapter and
reconnection machinery live in sibling modules (tasks 10.2, 10.3).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class SessionState(enum.Enum):
    """Connection and authentication state of the single Telegram session.

    The gateway maintains exactly one MTProto session (requirement 3.1). This
    enum models the lifecycle that reconnection logic (task 10.3) drives and
    that callers consult via :meth:`TelegramPort.get_session_state`:

    - :attr:`DISCONNECTED` — not currently connected to Telegram; no API calls
      can be made.
    - :attr:`CONNECTING` — a connection attempt is in progress but the session
      is not yet authenticated and usable.
    - :attr:`RECONNECTING` — the session was previously connected, a disconnect
      was detected, and exponential back-off reconnection is underway. While in
      this state the gateway surfaces Telegram-unavailable errors to callers
      without mutating committed state (requirements 3.2, 3.3).
    - :attr:`CONNECTED` — authenticated and able to send and receive Telegram
      API calls; the only state in which operations succeed (requirement 3.1).
    """

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    RECONNECTING = "reconnecting"
    CONNECTED = "connected"


@dataclass(frozen=True)
class TelegramMessage:
    """A decoded message read back from a bucket or system channel.

    Returned by :meth:`TelegramPort.get_messages`. Carries the message identity
    and caption the format layer uses to classify a message (manifest, commit,
    tombstone, bucket manifest, or checkpoint), together with the document
    metadata the Recovery CLI needs to locate and download the attached payload
    without a prior database — ``document_id``, ``file_reference``, and
    ``dc_id`` mirror the ``chunks`` table columns in the design's data model.

    Attributes:
        message_id: The Telegram message id within its channel. Monotonic per
            channel; the Recovery CLI scans in ascending ``message_id`` order.
        caption: The message caption, used to classify the message type and
            recover its logical identity (e.g. ``tgo3-c-<generation_id>``).
            Empty when the message carries no caption.
        document_id: Telegram document id of the attached file, or ``None`` for
            a message with no document.
        file_reference: The opaque, expiring file-reference blob required to
            download the document. ``None`` when there is no document; it is
            re-fetched when stale before a download.
        dc_id: The Telegram data-center id hosting the document, or ``None``
            when there is no document.
    """

    message_id: int
    caption: str
    document_id: int | None = None
    file_reference: bytes | None = None
    dc_id: int | None = None


@runtime_checkable
class TelegramPort(Protocol):
    """The single interface through which the gateway reaches Telegram.

    All Telegram access is routed through this protocol so Telethon can be
    replaced without changes elsewhere (requirement 3.4). The Telethon adapter
    (task 10.2) is the production implementation; tests supply a fake that
    replays recorded message sequences or injects faults.

    Every method is a coroutine. Implementations raise typed errors
    (``TelegramUnavailableError`` while reconnecting, ``FloodWaitError`` on rate
    limits) rather than leaking Telethon exceptions to callers.
    """

    async def send_document(
        self, channel_id: int, data: bytes, file_name: str, caption: str
    ) -> int:
        """Upload ``data`` as a document to a channel.

        Args:
            channel_id: Target channel id.
            data: Raw bytes of the document body (a sealed record chunk, a
                serialised manifest, etc.).
            file_name: File name to attach to the uploaded document.
            caption: Caption identifying the message's logical role.

        Returns:
            The ``message_id`` of the newly created message.
        """
        ...

    async def get_messages(
        self, channel_id: int, ids: list[int]
    ) -> list[TelegramMessage]:
        """Fetch messages by id from a channel.

        Args:
            channel_id: Channel to read from.
            ids: Message ids to retrieve.

        Returns:
            The decoded :class:`TelegramMessage` values for the requested ids.
        """
        ...

    async def download_document(self, channel_id: int, message_id: int) -> bytes:
        """Download the document attached to a message.

        Args:
            channel_id: Channel containing the message.
            message_id: Message whose document to download.

        Returns:
            The raw bytes of the attached document.
        """
        ...

    async def delete_messages(
        self, channel_id: int, message_ids: list[int]
    ) -> None:
        """Delete messages from a channel.

        Used by the GC Worker to purge the ``tgo3`` messages — data chunks, blob
        manifest, commit record, and tombstone — backing a generation or blob
        once it has been dereferenced for ``GC_GRACE`` (design "Generation State
        Machine": ``SUPERSEDED -> PURGED``, "tgo3 messages deleted from
        Telegram"). Deleting an already-absent message id is a no-op, so the
        call is safe to retry after a partial failure.

        Args:
            channel_id: Channel to delete from.
            message_ids: Message ids to delete.
        """
        ...

    async def create_channel(self, title: str) -> int:
        """Create a new channel to back a bucket.

        Args:
            title: Human-readable channel title.

        Returns:
            The ``channel_id`` of the created channel.
        """
        ...

    async def delete_channel(self, channel_id: int) -> None:
        """Delete a channel.

        Args:
            channel_id: Channel to delete.
        """
        ...

    async def get_session_state(self) -> SessionState:
        """Return the current session connection/authentication state."""
        ...

    async def is_connected(self) -> bool:
        """Return whether the session is connected and able to serve calls."""
        ...

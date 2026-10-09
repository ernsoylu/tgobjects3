"""Production :class:`TelegramPort` implementation backed by Telethon.

:class:`TelethonAdapter` is the one place in the gateway that imports Telethon.
It wraps a single :class:`telethon.TelegramClient` (requirement 3.1 — exactly
one MTProto session) and maps each of the seven :class:`TelegramPort` methods
onto the corresponding Telethon call, translating raw ``telethon.errors.*``
exceptions into the gateway's typed errors (:mod:`.errors`) so Telethon never
leaks past this boundary (requirement 3.4).

Scope: this module provides connection lifecycle (:meth:`connect`,
:meth:`disconnect`) and the request-mapping surface only. Automatic
disconnect detection, exponential back-off, and the ``RECONNECTING`` state
transitions live in the sibling reconnection module (task 10.3); this adapter
reports whatever state it currently holds and otherwise performs calls directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from telethon import TelegramClient
from telethon import errors as telethon_errors
from telethon.tl.functions.channels import CreateChannelRequest, DeleteChannelRequest

from .errors import FloodWaitError, TelegramUnavailableError
from .port import SessionState, TelegramMessage

if TYPE_CHECKING:
    from telethon.tl.custom.message import Message


class TelethonAdapter:
    """A :class:`TelegramPort` backed by a single Telethon client.

    The adapter owns exactly one :class:`telethon.TelegramClient` for the whole
    gateway process (requirement 3.1). Construct it with an already-configured
    client — session string, API id/hash, and any proxy settings are the
    caller's concern — then drive its lifecycle with :meth:`connect` and
    :meth:`disconnect`.

    All request methods assume the single-session invariant and translate
    Telethon failures into gateway-typed errors: ``telethon.errors.FloodWaitError``
    becomes :class:`.errors.FloodWaitError` (carrying the requested wait), and
    any other RPC or connection failure becomes
    :class:`.errors.TelegramUnavailableError`.
    """

    def __init__(self, client: TelegramClient) -> None:
        """Store the single client this adapter will drive.

        Args:
            client: A fully configured, not-yet-connected
                :class:`telethon.TelegramClient`. The adapter holds this exact
                instance for its entire lifetime and never creates another.
        """
        self._client = client
        self._state: SessionState = SessionState.DISCONNECTED

    # -- lifecycle ---------------------------------------------------------

    async def connect(self) -> None:
        """Connect and authenticate the single Telegram session.

        Moves the session to :attr:`SessionState.CONNECTING` for the duration
        of the attempt, then to :attr:`SessionState.CONNECTED` on success. On
        failure the session returns to :attr:`SessionState.DISCONNECTED` and a
        :class:`.errors.TelegramUnavailableError` is raised.

        Raises:
            TelegramUnavailableError: The connection could not be established
                or the session is not authorised.
        """
        self._state = SessionState.CONNECTING
        try:
            await self._client.connect()
            if not await self._client.is_user_authorized():
                self._state = SessionState.DISCONNECTED
                raise TelegramUnavailableError(
                    "Telegram session is not authorised"
                )
        except telethon_errors.FloodWaitError as exc:
            self._state = SessionState.DISCONNECTED
            raise self._flood(exc) from exc
        except TelegramUnavailableError:
            raise
        except Exception as exc:
            self._state = SessionState.DISCONNECTED
            raise TelegramUnavailableError(
                f"Failed to connect Telegram session: {exc}"
            ) from exc
        self._state = SessionState.CONNECTED

    async def disconnect(self) -> None:
        """Disconnect the Telegram session and mark it disconnected.

        Idempotent: safe to call when already disconnected. The session state
        becomes :attr:`SessionState.DISCONNECTED` regardless of whether the
        underlying disconnect raised.
        """
        try:
            result = self._client.disconnect()
            if result is not None:
                await result
        finally:
            self._state = SessionState.DISCONNECTED

    # -- port methods ------------------------------------------------------

    async def send_document(
        self, channel_id: int, data: bytes, file_name: str, caption: str
    ) -> int:
        """Upload ``data`` as a document and return the new message id."""
        try:
            message = await self._client.send_file(
                channel_id,
                file=data,
                caption=caption,
                file_name=file_name,
                force_document=True,
            )
        except telethon_errors.FloodWaitError as exc:
            raise self._flood(exc) from exc
        except telethon_errors.RPCError as exc:
            raise self._unavailable("send_document", exc) from exc
        except (OSError, ConnectionError) as exc:
            raise self._unavailable("send_document", exc) from exc
        return int(message.id)

    async def get_messages(
        self, channel_id: int, ids: list[int]
    ) -> list[TelegramMessage]:
        """Fetch messages by id and decode them to :class:`TelegramMessage`."""
        try:
            messages = await self._client.get_messages(channel_id, ids=ids)
        except telethon_errors.FloodWaitError as exc:
            raise self._flood(exc) from exc
        except telethon_errors.RPCError as exc:
            raise self._unavailable("get_messages", exc) from exc
        except (OSError, ConnectionError) as exc:
            raise self._unavailable("get_messages", exc) from exc
        return [self._to_message(m) for m in messages if m is not None]

    async def download_document(self, channel_id: int, message_id: int) -> bytes:
        """Download the document attached to a message and return its bytes."""
        try:
            messages = await self._client.get_messages(channel_id, ids=[message_id])
            message = messages[0] if messages else None
            if message is None or message.document is None:
                raise TelegramUnavailableError(
                    f"No document on message {message_id} in channel {channel_id}"
                )
            data = await self._client.download_media(message, file=bytes)
        except telethon_errors.FloodWaitError as exc:
            raise self._flood(exc) from exc
        except TelegramUnavailableError:
            raise
        except telethon_errors.RPCError as exc:
            raise self._unavailable("download_document", exc) from exc
        except (OSError, ConnectionError) as exc:
            raise self._unavailable("download_document", exc) from exc
        if not isinstance(data, bytes):
            raise TelegramUnavailableError(
                f"Unexpected download result for message {message_id}"
            )
        return data

    async def delete_messages(
        self, channel_id: int, message_ids: list[int]
    ) -> None:
        """Delete messages from a channel (used by the GC Worker)."""
        if not message_ids:
            return
        try:
            await self._client.delete_messages(channel_id, message_ids)
        except telethon_errors.FloodWaitError as exc:
            raise self._flood(exc) from exc
        except telethon_errors.RPCError as exc:
            raise self._unavailable("delete_messages", exc) from exc
        except (OSError, ConnectionError) as exc:
            raise self._unavailable("delete_messages", exc) from exc

    async def create_channel(self, title: str) -> int:
        """Create a private broadcast channel and return its channel id."""
        try:
            result = await self._client(
                CreateChannelRequest(title=title, about="", broadcast=True)
            )
            channel = result.chats[0]
        except telethon_errors.FloodWaitError as exc:
            raise self._flood(exc) from exc
        except telethon_errors.RPCError as exc:
            raise self._unavailable("create_channel", exc) from exc
        except (OSError, ConnectionError) as exc:
            raise self._unavailable("create_channel", exc) from exc
        return int(channel.id)

    async def delete_channel(self, channel_id: int) -> None:
        """Delete a channel."""
        try:
            entity = await self._client.get_input_entity(channel_id)
            await self._client(DeleteChannelRequest(channel=entity))
        except telethon_errors.FloodWaitError as exc:
            raise self._flood(exc) from exc
        except telethon_errors.RPCError as exc:
            raise self._unavailable("delete_channel", exc) from exc
        except (OSError, ConnectionError) as exc:
            raise self._unavailable("delete_channel", exc) from exc

    async def get_session_state(self) -> SessionState:
        """Return the current session connection/authentication state."""
        return self._state

    async def is_connected(self) -> bool:
        """Return whether the single session is connected and usable."""
        return self._state is SessionState.CONNECTED and bool(
            self._client.is_connected()
        )

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _to_message(message: Message) -> TelegramMessage:
        """Map a Telethon message onto the port's :class:`TelegramMessage`.

        Extracts the document metadata the Recovery CLI needs — ``document_id``,
        ``file_reference``, and ``dc_id`` — when the message carries a document;
        leaves them ``None`` otherwise.
        """
        document_id: int | None = None
        file_reference: bytes | None = None
        dc_id: int | None = None

        document = getattr(message, "document", None)
        if document is not None:
            document_id = int(document.id)
            raw_reference = getattr(document, "file_reference", None)
            if raw_reference is not None:
                file_reference = bytes(raw_reference)
            raw_dc = getattr(document, "dc_id", None)
            if raw_dc is not None:
                dc_id = int(raw_dc)

        caption = message.message or ""
        return TelegramMessage(
            message_id=int(message.id),
            caption=caption,
            document_id=document_id,
            file_reference=file_reference,
            dc_id=dc_id,
        )

    @staticmethod
    def _flood(exc: telethon_errors.FloodWaitError) -> FloodWaitError:
        """Translate Telethon's flood-wait into the gateway-typed error."""
        retry_after = int(getattr(exc, "seconds", 0) or 0)
        return FloodWaitError(retry_after=retry_after, message=str(exc))

    def _unavailable(
        self, operation: str, exc: Exception
    ) -> TelegramUnavailableError:
        """Build a :class:`TelegramUnavailableError` for a failed operation."""
        return TelegramUnavailableError(
            f"Telegram {operation} failed: {exc}"
        )

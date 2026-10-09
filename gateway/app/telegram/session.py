"""Connection lifecycle for the single Telegram session.

The gateway holds exactly one MTProto session (requirement 3.1). This module
owns that session's health: it watches for disconnection, drives reconnection
with exponential back-off, and gates every Telegram-dependent call on the
current :class:`~gateway.app.telegram.port.SessionState`.

:class:`SessionManager` wraps any :class:`~gateway.app.telegram.port.TelegramPort`
implementation (the Telethon adapter in production, a fake in tests) and layers
the reconnection state machine on top of it:

- A **monitor** polls the wrapped client's connectivity and detects a
  disconnection within 30 seconds (requirement 3.2). ``MONITOR_INTERVAL`` is
  set so at least one poll falls inside that window.
- A **reconnection loop** retries ``connect`` with exponential back-off starting
  at 1 second and doubling each attempt, capped at a configurable maximum
  (default 60 seconds, valid range 5–300 seconds; requirement 3.2). The maximum
  is validated at construction.
- While the session is anything other than
  :attr:`~gateway.app.telegram.port.SessionState.CONNECTED`, every call routed
  through the manager raises
  :class:`~gateway.app.telegram.errors.TelegramUnavailableError` without
  touching committed state (requirement 3.3). Callers translate that into an
  HTTP 503 response.

The manager depends only on the :class:`TelegramPort` protocol and an injectable
clock/sleep pair, so task 10.4 can drive it with a fake port that injects
disconnect events and a virtual clock, with no real time elapsing.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from gateway.app.telegram.errors import TelegramUnavailableError
from gateway.app.telegram.port import SessionState, TelegramMessage, TelegramPort

if TYPE_CHECKING:
    from types import TracebackType


@runtime_checkable
class ManagedTelegramClient(TelegramPort, Protocol):
    """A :class:`TelegramPort` that also exposes connection control.

    The reconnection machinery needs to open and close the underlying session,
    which the bare :class:`TelegramPort` surface does not expose. The Telethon
    adapter (task 10.2) provides these coroutines, and the test fake (task 10.4)
    does too, so :class:`SessionManager` can drive the lifecycle without
    reaching for Telethon directly.
    """

    async def connect(self) -> None:
        """Open the underlying session, authenticating if required."""
        ...

    async def disconnect(self) -> None:
        """Close the underlying session and release its transport."""
        ...


#: Initial back-off interval, in seconds, for the first reconnection attempt.
INITIAL_BACKOFF_SECONDS = 1.0

#: Default ceiling for the exponential back-off interval, in seconds.
DEFAULT_MAX_BACKOFF_SECONDS = 60.0

#: Lowest permitted value for the configurable back-off ceiling, in seconds.
MIN_MAX_BACKOFF_SECONDS = 5.0

#: Highest permitted value for the configurable back-off ceiling, in seconds.
MAX_MAX_BACKOFF_SECONDS = 300.0

#: How often the monitor polls connectivity, in seconds. Kept well under the
#: 30-second detection budget (requirement 3.2) so a dropped connection is
#: observed within one or two polls.
MONITOR_INTERVAL_SECONDS = 5.0

#: Type of the awaitable sleep used for back-off and polling waits. Injectable
#: so tests can drive the state machine with a virtual clock and no real delay.
SleepFn = Callable[[float], Awaitable[None]]


def _validate_max_backoff(max_backoff_seconds: float) -> float:
    """Validate the configured back-off ceiling against its permitted range.

    Args:
        max_backoff_seconds: The requested maximum back-off interval, in
            seconds.

    Returns:
        The validated maximum, unchanged.

    Raises:
        ValueError: If the value is outside the inclusive 5–300 second range
            (requirement 3.2).
    """
    if not MIN_MAX_BACKOFF_SECONDS <= max_backoff_seconds <= MAX_MAX_BACKOFF_SECONDS:
        raise ValueError(
            "max_backoff_seconds must be within "
            f"[{MIN_MAX_BACKOFF_SECONDS}, {MAX_MAX_BACKOFF_SECONDS}]; "
            f"got {max_backoff_seconds}"
        )
    return max_backoff_seconds


class SessionManager:
    """Owns the lifecycle of the single Telegram session.

    Wraps a :class:`TelegramPort` and adds disconnection detection and
    exponential-back-off reconnection, exposing the current session state and
    gating every Telegram-dependent call behind it.

    The manager is an async context manager: entering it connects and starts the
    monitor; exiting it stops the background tasks and disconnects.

    Args:
        client: The underlying Telegram client to manage. A
            :class:`ManagedTelegramClient` — a :class:`TelegramPort` that also
            exposes ``connect`` and ``disconnect`` coroutines (the Telethon
            adapter does; test fakes do too).
        max_backoff_seconds: Ceiling for the exponential back-off interval.
            Defaults to 60 s; validated to the inclusive 5–300 s range.
        monitor_interval_seconds: How often the monitor polls connectivity.
            Defaults to 5 s, comfortably inside the 30 s detection budget.
        sleep: Awaitable sleep used for back-off and polling. Injectable so
            tests can advance a virtual clock without real delay.

    Raises:
        ValueError: If ``max_backoff_seconds`` is outside the 5–300 s range.
    """

    def __init__(
        self,
        client: ManagedTelegramClient,
        *,
        max_backoff_seconds: float = DEFAULT_MAX_BACKOFF_SECONDS,
        monitor_interval_seconds: float = MONITOR_INTERVAL_SECONDS,
        sleep: SleepFn = asyncio.sleep,
    ) -> None:
        self._client = client
        self._max_backoff = _validate_max_backoff(max_backoff_seconds)
        self._monitor_interval = monitor_interval_seconds
        self._sleep = sleep

        self._state: SessionState = SessionState.DISCONNECTED
        self._state_lock = asyncio.Lock()
        self._monitor_task: asyncio.Task[None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self._closing = False

    # -- Lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Establish the initial connection and launch the monitor.

        Performs a first connection attempt (with back-off until it succeeds)
        and starts the background monitor that watches for disconnection.
        """
        self._closing = False
        await self._connect_with_backoff()
        if self._monitor_task is None or self._monitor_task.done():
            self._monitor_task = asyncio.create_task(self._monitor_loop())

    async def stop(self) -> None:
        """Stop all background tasks and disconnect the underlying client."""
        self._closing = True
        await self._cancel_task("_monitor_task")
        await self._cancel_task("_reconnect_task")
        await self._disconnect_client()
        await self._set_state(SessionState.DISCONNECTED)

    async def __aenter__(self) -> SessionManager:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.stop()

    # -- State -------------------------------------------------------------

    @property
    def state(self) -> SessionState:
        """The current session state (synchronous snapshot)."""
        return self._state

    async def get_session_state(self) -> SessionState:
        """Return the current session connection/authentication state."""
        return self._state

    async def is_connected(self) -> bool:
        """Return whether the session is currently able to serve calls."""
        return self._state is SessionState.CONNECTED

    async def _set_state(self, state: SessionState) -> None:
        async with self._state_lock:
            self._state = state

    def _ensure_available(self) -> None:
        """Raise if the session cannot currently serve Telegram calls.

        Raises:
            TelegramUnavailableError: When the session is anything other than
                CONNECTED — i.e. disconnected, connecting, or reconnecting
                (requirements 3.2, 3.3). The caller's committed state is left
                untouched because the guard runs before any work begins.
        """
        if self._state is not SessionState.CONNECTED:
            raise TelegramUnavailableError(
                f"Telegram session is {self._state.value}; cannot serve request"
            )

    # -- Guarded TelegramPort surface -------------------------------------

    async def send_document(
        self, channel_id: int, data: bytes, file_name: str, caption: str
    ) -> int:
        """Guarded :meth:`TelegramPort.send_document`."""
        self._ensure_available()
        return await self._client.send_document(channel_id, data, file_name, caption)

    async def get_messages(
        self, channel_id: int, ids: list[int]
    ) -> list[TelegramMessage]:
        """Guarded :meth:`TelegramPort.get_messages`."""
        self._ensure_available()
        return await self._client.get_messages(channel_id, ids)

    async def download_document(self, channel_id: int, message_id: int) -> bytes:
        """Guarded :meth:`TelegramPort.download_document`."""
        self._ensure_available()
        return await self._client.download_document(channel_id, message_id)

    async def delete_messages(
        self, channel_id: int, message_ids: list[int]
    ) -> None:
        """Guarded :meth:`TelegramPort.delete_messages`."""
        self._ensure_available()
        await self._client.delete_messages(channel_id, message_ids)

    async def create_channel(self, title: str) -> int:
        """Guarded :meth:`TelegramPort.create_channel`."""
        self._ensure_available()
        return await self._client.create_channel(title)

    async def delete_channel(self, channel_id: int) -> None:
        """Guarded :meth:`TelegramPort.delete_channel`."""
        self._ensure_available()
        await self._client.delete_channel(channel_id)

    # -- Monitor and reconnection -----------------------------------------

    async def _monitor_loop(self) -> None:
        """Poll connectivity and trigger reconnection on disconnect.

        Runs until :meth:`stop` is called. Each poll interval is kept below the
        30-second detection budget (requirement 3.2); the first poll that sees
        the client disconnected while the manager believes it is CONNECTED kicks
        off the reconnection loop.
        """
        while not self._closing:
            try:
                await self._sleep(self._monitor_interval)
            except asyncio.CancelledError:
                raise
            if self._closing:
                break
            if self._state is not SessionState.CONNECTED:
                continue
            still_connected = await self._safe_is_connected()
            if not still_connected:
                await self._trigger_reconnect()

    async def _trigger_reconnect(self) -> None:
        """Start the reconnection loop if one is not already running."""
        if self._reconnect_task is not None and not self._reconnect_task.done():
            return
        await self._set_state(SessionState.RECONNECTING)
        self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def _reconnect_loop(self) -> None:
        """Reconnect with exponential back-off until connected or stopped.

        Starts at :data:`INITIAL_BACKOFF_SECONDS` and doubles the wait after
        each failed attempt, never exceeding the configured maximum
        (requirement 3.2). The session remains RECONNECTING — so callers keep
        receiving :class:`TelegramUnavailableError` (requirement 3.3) — until a
        connection attempt succeeds.
        """
        backoff = INITIAL_BACKOFF_SECONDS
        while not self._closing:
            try:
                await self._client.connect()
            except asyncio.CancelledError:
                raise
            except Exception:
                if self._closing:
                    return
                await self._sleep(backoff)
                backoff = min(backoff * 2, self._max_backoff)
                continue

            if await self._safe_is_connected():
                await self._set_state(SessionState.CONNECTED)
                return

            # Connect returned without raising but the client is still not
            # usable; keep backing off.
            await self._sleep(backoff)
            backoff = min(backoff * 2, self._max_backoff)

    async def _connect_with_backoff(self) -> None:
        """Perform the initial connection, backing off until it succeeds."""
        await self._set_state(SessionState.CONNECTING)
        backoff = INITIAL_BACKOFF_SECONDS
        while not self._closing:
            try:
                await self._client.connect()
            except asyncio.CancelledError:
                raise
            except Exception:
                if self._closing:
                    return
                await self._sleep(backoff)
                backoff = min(backoff * 2, self._max_backoff)
                continue

            if await self._safe_is_connected():
                await self._set_state(SessionState.CONNECTED)
                return
            await self._sleep(backoff)
            backoff = min(backoff * 2, self._max_backoff)

    async def _safe_is_connected(self) -> bool:
        """Query the client's connectivity, treating any error as disconnected."""
        try:
            return await self._client.is_connected()
        except asyncio.CancelledError:
            raise
        except Exception:
            return False

    async def _disconnect_client(self) -> None:
        """Best-effort disconnect of the underlying client."""
        try:
            await self._client.disconnect()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def _cancel_task(self, attr: str) -> None:
        """Cancel and await a named background task if it is running."""
        task: asyncio.Task[None] | None = getattr(self, attr)
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        setattr(self, attr, None)

"""Unit tests for the Telegram session reconnection state machine.

Covers :class:`gateway.app.telegram.session.SessionManager` — the reconnection
state machine layered over any :class:`~gateway.app.telegram.port.TelegramPort`
(Requirements 3.2, 3.3):

* **Disconnect detection within 30 s** — the monitor polls connectivity on an
  interval well under the 30-second budget, so an injected disconnect flips the
  manager into ``RECONNECTING`` and launches the reconnection loop
  (Requirement 3.2).
* **Exponential back-off within spec** — reconnection waits start at 1 second,
  double on each failed attempt, and never exceed the configured maximum
  (default 60 s, valid range 5–300 s). A ceiling outside that range is rejected
  at construction (Requirement 3.2).
* **Callers receive ``TelegramUnavailableError``** — while the session is
  anything other than ``CONNECTED`` (disconnected, connecting, or reconnecting)
  every guarded :class:`TelegramPort` call raises
  :class:`~gateway.app.telegram.errors.TelegramUnavailableError` without
  touching the underlying client (Requirement 3.3).

The tests drive the machine with a fake :class:`ManagedTelegramClient` that
injects disconnect events on demand and an injectable sleep that records every
requested interval, so the back-off schedule is asserted exactly and no real
time elapses.
"""

from __future__ import annotations

import asyncio

import pytest
from gateway.app.telegram.errors import TelegramUnavailableError
from gateway.app.telegram.port import SessionState, TelegramMessage
from gateway.app.telegram.session import (
    DEFAULT_MAX_BACKOFF_SECONDS,
    INITIAL_BACKOFF_SECONDS,
    MAX_MAX_BACKOFF_SECONDS,
    MIN_MAX_BACKOFF_SECONDS,
    MONITOR_INTERVAL_SECONDS,
    SessionManager,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeTelegramClient:
    """A controllable :class:`ManagedTelegramClient` for driving the machine.

    Models the two signals the reconnection logic depends on:

    * connectivity, reported by :meth:`is_connected` and flipped by
      :meth:`inject_disconnect` to simulate a dropped session, and
    * the outcome of :meth:`connect`, which either restores connectivity or
      raises (via ``fail_connects``) to exercise the back-off loop.

    Every guarded data method records that it was invoked so a test can assert
    the manager never forwarded a call while unavailable (Requirement 3.3).
    """

    def __init__(self) -> None:
        self._connected = False
        #: Number of leading ``connect`` calls that should raise before one
        #: succeeds. Each failing call decrements it.
        self.fail_connects = 0
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.forwarded_calls: list[str] = []

    # -- connection control (ManagedTelegramClient) -----------------------

    async def connect(self) -> None:
        self.connect_calls += 1
        if self.fail_connects > 0:
            self.fail_connects -= 1
            raise ConnectionError("injected connect failure")
        self._connected = True

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self._connected = False

    async def is_connected(self) -> bool:
        return self._connected

    def inject_disconnect(self) -> None:
        """Simulate the transport dropping underneath an established session."""
        self._connected = False

    # -- TelegramPort surface (should never run while unavailable) --------

    async def get_session_state(self) -> SessionState:  # pragma: no cover
        return SessionState.CONNECTED if self._connected else SessionState.DISCONNECTED

    async def send_document(
        self, channel_id: int, data: bytes, file_name: str, caption: str
    ) -> int:
        self.forwarded_calls.append("send_document")
        return 1

    async def get_messages(
        self, channel_id: int, ids: list[int]
    ) -> list[TelegramMessage]:
        self.forwarded_calls.append("get_messages")
        return []

    async def download_document(self, channel_id: int, message_id: int) -> bytes:
        self.forwarded_calls.append("download_document")
        return b""

    async def delete_messages(self, channel_id: int, message_ids: list[int]) -> None:
        self.forwarded_calls.append("delete_messages")

    async def create_channel(self, title: str) -> int:
        self.forwarded_calls.append("create_channel")
        return 1

    async def delete_channel(self, channel_id: int) -> None:
        self.forwarded_calls.append("delete_channel")


class _RecordingSleep:
    """An injectable sleep that records intervals and never really waits.

    Returning immediately would spin the monitor loop with no pause, so the
    sleep can be *gated*: once ``gate_after`` sleeps have been recorded it blocks
    on an :class:`asyncio.Event` the test controls, letting the test freeze the
    machine at a known point (e.g. mid-reconnect) and inspect it. By default the
    gate is open and every sleep returns at once.
    """

    def __init__(self, gate_after: int | None = None) -> None:
        self.intervals: list[float] = []
        self._gate_after = gate_after
        self._release = asyncio.Event()
        if gate_after is None:
            self._release.set()

    async def __call__(self, seconds: float) -> None:
        self.intervals.append(seconds)
        if self._gate_after is not None and len(self.intervals) >= self._gate_after:
            await self._release.wait()
        await asyncio.sleep(0)

    def open_gate(self) -> None:
        self._release.set()


async def _wait_for_state(
    manager: SessionManager, state: SessionState, *, timeout: float = 1.0
) -> None:
    """Poll until the manager reaches ``state`` or the timeout elapses."""

    async def _poll() -> None:
        while manager.state is not state:
            await asyncio.sleep(0)

    await asyncio.wait_for(_poll(), timeout=timeout)


# ---------------------------------------------------------------------------
# Construction / configuration (Requirement 3.2 — configurable ceiling)
# ---------------------------------------------------------------------------


def test_default_max_backoff_is_60_seconds() -> None:
    """The documented default ceiling is 60 s and sits inside the valid range."""
    assert DEFAULT_MAX_BACKOFF_SECONDS == 60.0
    assert MIN_MAX_BACKOFF_SECONDS == 5.0
    assert MAX_MAX_BACKOFF_SECONDS == 300.0
    assert INITIAL_BACKOFF_SECONDS == 1.0


def test_monitor_interval_is_within_detection_budget() -> None:
    """A single poll must fall inside the 30-second detection window."""
    assert MONITOR_INTERVAL_SECONDS < 30.0


@pytest.mark.parametrize("max_backoff", [MIN_MAX_BACKOFF_SECONDS, 60.0, MAX_MAX_BACKOFF_SECONDS])
def test_max_backoff_within_range_is_accepted(max_backoff: float) -> None:
    """Any ceiling in the inclusive 5–300 s range constructs successfully."""
    manager = SessionManager(_FakeTelegramClient(), max_backoff_seconds=max_backoff)
    assert manager.state is SessionState.DISCONNECTED


@pytest.mark.parametrize("max_backoff", [4.9, 0.0, -1.0, 300.1, 600.0])
def test_max_backoff_out_of_range_is_rejected(max_backoff: float) -> None:
    """A ceiling outside the 5–300 s range raises at construction time."""
    with pytest.raises(ValueError, match="max_backoff_seconds"):
        SessionManager(_FakeTelegramClient(), max_backoff_seconds=max_backoff)


# ---------------------------------------------------------------------------
# Unavailability guard (Requirement 3.3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "state",
    [SessionState.DISCONNECTED, SessionState.CONNECTING, SessionState.RECONNECTING],
)
async def test_calls_raise_when_not_connected(state: SessionState) -> None:
    """Every non-CONNECTED state makes guarded calls raise, forwarding nothing."""
    client = _FakeTelegramClient()
    manager = SessionManager(client, sleep=_RecordingSleep())
    await manager._set_state(state)

    with pytest.raises(TelegramUnavailableError):
        await manager.send_document(1, b"x", "f", "c")
    with pytest.raises(TelegramUnavailableError):
        await manager.get_messages(1, [1])
    with pytest.raises(TelegramUnavailableError):
        await manager.download_document(1, 1)
    with pytest.raises(TelegramUnavailableError):
        await manager.delete_messages(1, [1])
    with pytest.raises(TelegramUnavailableError):
        await manager.create_channel("t")
    with pytest.raises(TelegramUnavailableError):
        await manager.delete_channel(1)

    # None of the guarded calls reached the underlying client.
    assert client.forwarded_calls == []


async def test_calls_forward_when_connected() -> None:
    """Once CONNECTED, guarded calls pass through to the underlying client."""
    client = _FakeTelegramClient()
    manager = SessionManager(client, sleep=_RecordingSleep())
    await manager._set_state(SessionState.CONNECTED)

    assert await manager.send_document(1, b"x", "f", "c") == 1
    assert await manager.get_messages(1, [1]) == []
    assert client.forwarded_calls == ["send_document", "get_messages"]


# ---------------------------------------------------------------------------
# Disconnect detection + reconnection (Requirements 3.2, 3.3)
# ---------------------------------------------------------------------------


async def test_start_connects_and_reports_connected() -> None:
    """A clean start reaches CONNECTED with a single successful connect."""
    client = _FakeTelegramClient()
    manager = SessionManager(client, sleep=_RecordingSleep())

    await manager.start()
    try:
        assert manager.state is SessionState.CONNECTED
        assert await manager.is_connected() is True
        assert client.connect_calls == 1
    finally:
        await manager.stop()
    assert manager.state is SessionState.DISCONNECTED


async def test_injected_disconnect_is_detected_and_recovers() -> None:
    """An injected disconnect flips to RECONNECTING, then recovers to CONNECTED.

    The monitor observes the dropped connection within one poll interval (well
    under the 30-second budget, Requirement 3.2) and the back-off loop restores
    the session once ``connect`` succeeds again. The monitor's poll sleep is
    gated so the test, not a busy loop, decides when the next poll runs: the
    disconnect is injected while the monitor is parked on its first sleep, then
    the gate is opened to let exactly that poll observe the drop.
    """
    client = _FakeTelegramClient()
    sleep = _RecordingSleep(gate_after=1)
    manager = SessionManager(client, sleep=sleep)

    await manager.start()
    try:
        assert manager.state is SessionState.CONNECTED
        assert client.connect_calls == 1

        # Let the monitor task reach and park on its first poll sleep.
        while not sleep.intervals:
            await asyncio.sleep(0)

        # The monitor is parked on its first poll sleep (gate_after=1). Drop the
        # connection underneath it, then release the poll so it observes the drop.
        client.inject_disconnect()
        # Make the first reconnect attempt fail once so the machine is observably
        # held in RECONNECTING before it recovers; then let the polls run.
        client.fail_connects = 1
        sleep.open_gate()

        # The monitor flips the session into RECONNECTING on detecting the drop,
        # then the back-off loop re-connects and returns it to CONNECTED.
        await _wait_for_state(manager, SessionState.RECONNECTING)
        await _wait_for_state(manager, SessionState.CONNECTED)
        # One failed reconnect + one success, on top of the initial connect.
        assert client.connect_calls >= 3
        # The monitor's detection poll used the (sub-30s) monitor interval.
        assert sleep.intervals[0] == MONITOR_INTERVAL_SECONDS
    finally:
        await manager.stop()


async def test_caller_sees_unavailable_while_reconnecting() -> None:
    """While reconnection is underway, callers get TelegramUnavailableError.

    The injected ``connect`` failures hold the machine in RECONNECTING; the
    gated back-off sleep freezes it there so a guarded call is guaranteed to
    observe the reconnecting state (Requirement 3.3). The reconnection is driven
    through :meth:`_trigger_reconnect`, which is what sets the RECONNECTING
    state in production before the loop starts.
    """
    client = _FakeTelegramClient()
    client.fail_connects = 100  # never succeed while the gate is shut
    sleep = _RecordingSleep(gate_after=1)
    manager = SessionManager(client, sleep=sleep)
    await manager._set_state(SessionState.CONNECTED)

    await manager._trigger_reconnect()
    try:
        await _wait_for_state(manager, SessionState.RECONNECTING)
        # First connect fails, back-off sleep is reached and gated -> frozen.
        with pytest.raises(TelegramUnavailableError):
            await manager.send_document(1, b"x", "f", "c")
        assert client.forwarded_calls == []
    finally:
        manager._closing = True
        sleep.open_gate()
        await manager._cancel_task("_reconnect_task")


# ---------------------------------------------------------------------------
# Back-off schedule (Requirement 3.2)
# ---------------------------------------------------------------------------


async def test_backoff_doubles_from_one_second() -> None:
    """Failed reconnects back off 1, 2, 4, 8 … seconds before succeeding."""
    client = _FakeTelegramClient()
    client.fail_connects = 4  # four failures, then connect succeeds
    sleep = _RecordingSleep()
    manager = SessionManager(client, sleep=sleep)

    await manager._reconnect_loop()

    assert manager.state is SessionState.CONNECTED
    # One recorded sleep per failed attempt; the successful attempt adds none.
    assert sleep.intervals == [1.0, 2.0, 4.0, 8.0]


async def test_backoff_is_capped_at_configured_max() -> None:
    """Back-off doubles but never exceeds the configured ceiling."""
    client = _FakeTelegramClient()
    client.fail_connects = 8
    sleep = _RecordingSleep()
    manager = SessionManager(client, max_backoff_seconds=10.0, sleep=sleep)

    await manager._reconnect_loop()

    assert manager.state is SessionState.CONNECTED
    # 1, 2, 4, 8, then clamped to the 10 s ceiling for the rest.
    assert sleep.intervals == [1.0, 2.0, 4.0, 8.0, 10.0, 10.0, 10.0, 10.0]
    assert all(interval <= 10.0 for interval in sleep.intervals)


async def test_backoff_never_exceeds_default_ceiling() -> None:
    """With the default ceiling, no back-off interval tops 60 s."""
    client = _FakeTelegramClient()
    client.fail_connects = 12
    sleep = _RecordingSleep()
    manager = SessionManager(client, sleep=sleep)

    await manager._reconnect_loop()

    assert manager.state is SessionState.CONNECTED
    assert sleep.intervals[0] == INITIAL_BACKOFF_SECONDS
    assert max(sleep.intervals) <= DEFAULT_MAX_BACKOFF_SECONDS
    # The schedule is monotonically non-decreasing up to the ceiling.
    assert sleep.intervals == sorted(sleep.intervals)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))

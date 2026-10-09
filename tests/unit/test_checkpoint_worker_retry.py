"""Unit tests for the Checkpoint Worker's write-retry policy (Requirement 15.6).

The design's checkpoint rule (requirement 15.6) says a failed Telegram write of
the ``Checkpoint`` message is retried up to
:data:`~gateway.app.jobs.checkpoint_worker.CHECKPOINT_WRITE_MAX_ATTEMPTS` times
at :data:`~gateway.app.jobs.checkpoint_worker.CHECKPOINT_RETRY_INTERVAL_SECONDS`
intervals; if every attempt fails the worker emits an error log entry and
returns normally, leaving object storage operations untouched. A checkpoint is a
*cache* accelerator, never a source of truth, so its failure must never
interrupt the write path.

These tests exercise that policy in isolation at two levels:

* :meth:`CheckpointWorker._write_with_retry` is driven directly against a fake
  :class:`~gateway.app.telegram.port.TelegramPort` that fails a configured
  number of leading attempts. They assert the success path (succeeds on the 3rd
  attempt after two failures and returns the ``message_id``), the number of
  Telegram ``send_document`` calls, the backoff sleeps between attempts, and the
  exhaustion path (all three fail -> :class:`CheckpointWriteError` chained from
  the last Telegram error).
* :meth:`CheckpointWorker.run_once` is driven end to end with the pg_dump
  subprocess and the database boundary faked, so a cycle can be observed as a
  whole: on recovery after two failures it records a ``checkpoints`` row and
  returns ``True``; on total failure it swallows the error, logs it, records no
  row, and returns ``False`` (operations continue).

The retry interval is never actually slept: ``asyncio.sleep`` is patched in the
worker module to a recorder, so the backoff is observed as recorded durations
rather than real waits.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

import pytest
from gateway.app.format import checkpoint as checkpoint_codec
from gateway.app.jobs import checkpoint_worker
from gateway.app.jobs.checkpoint_worker import (
    CHECKPOINT_CAPTION_PREFIX,
    CHECKPOINT_RETRY_INTERVAL_SECONDS,
    CHECKPOINT_WRITE_MAX_ATTEMPTS,
    CheckpointWorker,
    CheckpointWriteError,
)
from gateway.app.telegram.errors import FloodWaitError, TelegramUnavailableError
from gateway.app.telegram.port import SessionState, TelegramMessage

# A 16-byte master key (meets the TGOS3_MASTER_KEY minimum) for key derivation.
_MASTER_KEY = "0123456789abcdef"
_SYSTEM_CHANNEL_ID = 777
_DATABASE_URL = "postgresql+asyncpg://user:pass@localhost/db"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeTelegram:
    """A :class:`TelegramPort` that fails a configured number of leading writes.

    ``send_document`` raises the supplied error for its first ``fail_times``
    calls, then returns ``success_message_id`` on the next call. Every call is
    counted and its arguments recorded so a test can assert both the retry count
    and the caption/file-name scheme the worker uses. The remaining port methods
    are never reached by the checkpoint write path and fail loudly if they are.
    """

    def __init__(
        self,
        *,
        fail_times: int,
        error: Exception,
        success_message_id: int = 4242,
    ) -> None:
        self._fail_times = fail_times
        self._error = error
        self._success_message_id = success_message_id
        self.calls: list[dict[str, Any]] = []

    async def send_document(
        self, channel_id: int, data: bytes, file_name: str, caption: str
    ) -> int:
        self.calls.append(
            {
                "channel_id": channel_id,
                "data": data,
                "file_name": file_name,
                "caption": caption,
            }
        )
        if len(self.calls) <= self._fail_times:
            raise self._error
        return self._success_message_id

    async def get_messages(
        self, channel_id: int, ids: list[int]
    ) -> list[TelegramMessage]:  # pragma: no cover - write path never reads.
        raise AssertionError("checkpoint write must not read Telegram")

    async def download_document(
        self, channel_id: int, message_id: int
    ) -> bytes:  # pragma: no cover
        raise AssertionError("checkpoint write must not download from Telegram")

    async def delete_messages(
        self, channel_id: int, message_ids: list[int]
    ) -> None:  # pragma: no cover
        raise AssertionError("checkpoint write must not delete from Telegram")

    async def create_channel(self, title: str) -> int:  # pragma: no cover
        raise AssertionError("checkpoint write must not create channels")

    async def delete_channel(self, channel_id: int) -> None:  # pragma: no cover
        raise AssertionError("checkpoint write must not delete channels")

    async def get_session_state(self) -> SessionState:  # pragma: no cover
        return SessionState.CONNECTED

    async def is_connected(self) -> bool:  # pragma: no cover
        return True


class _RecordingSession:
    """AsyncSession stand-in that records the ``checkpoints`` row added.

    The cycle opens two sessions: one for :func:`compute_high_water` (a
    read-only query) and one wrapped in ``session.begin()`` for
    :meth:`CheckpointWorker._record_checkpoint`. The high-water query is faked
    away in these tests, so the only method exercised here is ``add`` recording
    the ``Checkpoint`` row; ``begin`` is a no-op transaction context manager.
    """

    def __init__(self) -> None:
        self.added: list[Any] = []

    def begin(self) -> Any:
        @asynccontextmanager
        async def _begin() -> Any:
            yield self

        return _begin()

    def add(self, instance: Any) -> None:
        self.added.append(instance)


class _RecordingSessionFactory:
    """Factory yielding a shared :class:`_RecordingSession` as a context manager.

    The worker opens ``session_factory()`` as an async context manager; the same
    recording session is returned every time so a test reads back what the cycle
    wrote. ``opened`` counts how many sessions the cycle acquired.
    """

    def __init__(self) -> None:
        self.session = _RecordingSession()
        self.opened = 0

    def __call__(self) -> _RecordingSessionFactory:
        self.opened += 1
        return self

    async def __aenter__(self) -> _RecordingSession:
        return self.session

    async def __aexit__(self, *exc: object) -> None:
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_worker(
    telegram: _FakeTelegram,
    session_factory: _RecordingSessionFactory | None = None,
) -> CheckpointWorker:
    """Build a CheckpointWorker over the fake port (and optional fake factory)."""
    return CheckpointWorker(
        session_factory,  # type: ignore[arg-type]
        telegram,
        _MASTER_KEY,
        _SYSTEM_CHANNEL_ID,
        _DATABASE_URL,
        interval_seconds=60.0,
    )


@pytest.fixture
def recorded_sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Patch the worker's ``asyncio.sleep`` to record durations without waiting.

    Returns the list the recorded sleep durations accumulate into, so a test can
    assert the number of backoff waits and their :data:`CHECKPOINT_RETRY_INTERVAL
    _SECONDS` length without any real delay.
    """
    sleeps: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(
        "gateway.app.jobs.checkpoint_worker.asyncio.sleep", _fake_sleep
    )
    return sleeps


# ---------------------------------------------------------------------------
# _write_with_retry — success after transient failures
# ---------------------------------------------------------------------------


async def test_write_succeeds_on_third_attempt_after_two_failures(
    recorded_sleeps: list[float],
) -> None:
    """Two Telegram failures then success returns the id after three attempts.

    The write is retried up to the budget; the two leading failures each sleep
    the retry interval, and the third attempt succeeds and returns its
    ``message_id``.

    Validates: Requirements 15.6
    """
    telegram = _FakeTelegram(
        fail_times=2,
        error=TelegramUnavailableError("transport down"),
        success_message_id=9001,
    )
    worker = _make_worker(telegram)

    message_id = await worker._write_with_retry(b"payload")

    assert message_id == 9001
    assert len(telegram.calls) == 3
    # One backoff sleep per failed attempt; none after the final success.
    assert recorded_sleeps == [
        CHECKPOINT_RETRY_INTERVAL_SECONDS,
        CHECKPOINT_RETRY_INTERVAL_SECONDS,
    ]


async def test_write_succeeds_first_attempt_without_sleeping(
    recorded_sleeps: list[float],
) -> None:
    """A write that succeeds immediately makes one call and never backs off.

    Validates: Requirements 15.6
    """
    telegram = _FakeTelegram(
        fail_times=0,
        error=TelegramUnavailableError("unused"),
        success_message_id=1,
    )
    worker = _make_worker(telegram)

    message_id = await worker._write_with_retry(b"payload")

    assert message_id == 1
    assert len(telegram.calls) == 1
    assert recorded_sleeps == []


async def test_write_uses_timestamped_checkpoint_caption(
    recorded_sleeps: list[float],
) -> None:
    """The write carries a ``tgo3-p-<ts>`` caption and matching file name.

    Validates: Requirements 15.6
    """
    telegram = _FakeTelegram(fail_times=0, error=RuntimeError("unused"))
    worker = _make_worker(telegram)

    await worker._write_with_retry(b"payload")

    call = telegram.calls[0]
    assert call["channel_id"] == _SYSTEM_CHANNEL_ID
    assert call["data"] == b"payload"
    assert call["caption"].startswith(CHECKPOINT_CAPTION_PREFIX)
    timestamp = call["caption"].removeprefix(CHECKPOINT_CAPTION_PREFIX)
    assert timestamp.isdigit()
    assert call["file_name"] == f"checkpoint-{timestamp}.msgpack"


# ---------------------------------------------------------------------------
# _write_with_retry — exhaustion
# ---------------------------------------------------------------------------


async def test_write_raises_after_all_attempts_fail(
    recorded_sleeps: list[float],
) -> None:
    """All attempts failing raises CheckpointWriteError chained from the last.

    The budget is :data:`CHECKPOINT_WRITE_MAX_ATTEMPTS` calls with a backoff
    between each pair, so the number of sleeps is one fewer than the attempts.

    Validates: Requirements 15.6
    """
    last_error = TelegramUnavailableError("still down")
    telegram = _FakeTelegram(
        fail_times=CHECKPOINT_WRITE_MAX_ATTEMPTS, error=last_error
    )
    worker = _make_worker(telegram)

    with pytest.raises(CheckpointWriteError) as excinfo:
        await worker._write_with_retry(b"payload")

    assert excinfo.value.__cause__ is last_error
    assert len(telegram.calls) == CHECKPOINT_WRITE_MAX_ATTEMPTS
    assert recorded_sleeps == [CHECKPOINT_RETRY_INTERVAL_SECONDS] * (
        CHECKPOINT_WRITE_MAX_ATTEMPTS - 1
    )


async def test_write_retries_flood_wait_errors(
    recorded_sleeps: list[float],
) -> None:
    """A ``FloodWaitError`` is a retryable Telegram error on the write path.

    ``FloodWaitError`` subclasses ``TelegramError``, so the write retries it like
    any other transient failure rather than letting it escape untyped.

    Validates: Requirements 15.6
    """
    telegram = _FakeTelegram(
        fail_times=1,
        error=FloodWaitError(retry_after=30),
        success_message_id=55,
    )
    worker = _make_worker(telegram)

    message_id = await worker._write_with_retry(b"payload")

    assert message_id == 55
    assert len(telegram.calls) == 2
    assert recorded_sleeps == [CHECKPOINT_RETRY_INTERVAL_SECONDS]


# ---------------------------------------------------------------------------
# run_once — full cycle around the retrying write
# ---------------------------------------------------------------------------


def _patch_cycle_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake the pg_dump subprocess and the high-water query for ``run_once``.

    ``_run_pg_dump`` is replaced with a coroutine returning fixed dump bytes so
    no real ``pg_dump`` subprocess runs, and ``compute_high_water`` returns a
    fixed per-channel map so no real database is queried. The encryption and
    msgpack serialisation in between run for real.
    """

    async def _fake_pg_dump(database_url: str) -> bytes:
        assert database_url == _DATABASE_URL
        return b"-- fake pg_dump output"

    async def _fake_high_water(session: Any) -> dict[str, int]:
        return {"777": 12, "888": 0}

    monkeypatch.setattr(checkpoint_worker, "_run_pg_dump", _fake_pg_dump)
    monkeypatch.setattr(checkpoint_worker, "compute_high_water", _fake_high_water)


async def test_run_once_records_checkpoint_after_two_write_failures(
    monkeypatch: pytest.MonkeyPatch,
    recorded_sleeps: list[float],
) -> None:
    """A cycle recovering on the 3rd write records a row and returns True.

    The two leading write failures are retried, the third succeeds, and the
    cycle inserts exactly one ``checkpoints`` row carrying the written
    ``message_id`` and the computed high-water map.

    Validates: Requirements 15.6
    """
    _patch_cycle_boundaries(monkeypatch)
    telegram = _FakeTelegram(
        fail_times=2,
        error=TelegramUnavailableError("transport down"),
        success_message_id=321,
    )
    factory = _RecordingSessionFactory()
    worker = _make_worker(telegram, factory)

    result = await worker.run_once()

    assert result is True
    assert len(telegram.calls) == 3
    # The payload carried a well-formed Checkpoint with the high-water map.
    sent = checkpoint_codec.deserialise(telegram.calls[-1]["data"])
    assert sent.high_water == {"777": 12, "888": 0}
    # Exactly one checkpoints row recorded, with the written message id.
    assert len(factory.session.added) == 1
    row = factory.session.added[0]
    assert row.message_id == 321
    assert row.high_water == {"777": 12, "888": 0}


async def test_run_once_swallows_total_write_failure(
    monkeypatch: pytest.MonkeyPatch,
    recorded_sleeps: list[float],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A cycle whose writes all fail logs an error, records nothing, returns False.

    The write exhausts its budget, the resulting ``CheckpointWriteError`` is
    caught at the cycle boundary and logged, no ``checkpoints`` row is recorded,
    and ``run_once`` returns ``False`` without raising — so object storage
    operations are never interrupted.

    Validates: Requirements 15.6
    """
    _patch_cycle_boundaries(monkeypatch)
    telegram = _FakeTelegram(
        fail_times=CHECKPOINT_WRITE_MAX_ATTEMPTS,
        error=TelegramUnavailableError("still down"),
    )
    factory = _RecordingSessionFactory()
    worker = _make_worker(telegram, factory)

    with caplog.at_level(logging.ERROR, logger=checkpoint_worker.logger.name):
        result = await worker.run_once()

    assert result is False
    assert len(telegram.calls) == CHECKPOINT_WRITE_MAX_ATTEMPTS
    # No checkpoints row was recorded (the record step is never reached).
    assert factory.session.added == []
    # The failure surfaced as an error log entry, not a propagated exception.
    assert any(
        record.levelno == logging.ERROR and "checkpoint write failed" in record.message
        for record in caplog.records
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))

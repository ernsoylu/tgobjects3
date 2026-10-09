"""Unit tests for the Upload Worker's retry/backoff policy (Requirement 6.14).

The design's "Retry and backoff" rule says a Telegram error during upload moves
the generation to ``RETRY_WAIT``, increments the job's attempt counter, and
schedules the next attempt after an exponential backoff that starts at
:data:`~gateway.app.jobs.upload_worker.INITIAL_BACKOFF_SECONDS` and doubles each
attempt — unless the error is a
:class:`~gateway.app.telegram.errors.FloodWaitError`, in which case Telegram's
requested wait overrides the computed backoff. After
:data:`~gateway.app.jobs.upload_worker.MAX_UPLOAD_ATTEMPTS` attempts the
generation is marked ``FAILED`` and the staged file is retained.

These tests exercise that policy in isolation:

* :func:`backoff_seconds` is a pure function, so its 1 s/2 s/4 s/… doubling is
  asserted directly across the attempt range.
* :meth:`UploadWorker._handle_telegram_error` is driven with a fake session
  factory that records the ``update(...).values(...)`` applied to the generation
  and ``upload_jobs`` rows (recovered from each statement's compiled params) and
  with the module clock pinned, so the scheduled ``next_attempt_at`` can be
  compared against the expected backoff to the second. ``_utcnow`` is pinned via
  monkeypatch so the test needs no real waiting — the backoff is observed as a
  computed timestamp delta, not a slept interval.
* The ``FAILED``-after-exhaustion case additionally stages a real on-disk file
  and asserts the handler never removes it (``mark_failed`` only moves state),
  matching "the staged file is retained".

No real database or Telegram transport is involved: the retry/backoff decision
is pure index bookkeeping over a claimed job, so both boundaries are faked.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from gateway.app.db.models import Generation, UploadJob
from gateway.app.jobs import upload_worker
from gateway.app.jobs.upload_worker import (
    INITIAL_BACKOFF_SECONDS,
    MAX_UPLOAD_ATTEMPTS,
    ClaimedJob,
    UploadWorker,
    backoff_seconds,
)
from gateway.app.storage import generations as generations_storage
from gateway.app.telegram.errors import (
    FloodWaitError,
    TelegramUnavailableError,
)
from gateway.app.telegram.port import SessionState, TelegramMessage

# A clock the tests pin so a scheduled ``next_attempt_at`` is deterministic.
_FIXED_NOW = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _RecordingResult:
    """Result stand-in for the ``update`` statements the handler issues.

    ``generations._set_state`` reads ``result.rowcount`` to detect a missing
    generation row; a non-zero value here means the transition is always treated
    as applied to an existing row.
    """

    rowcount = 1


class _RecordingSession:
    """AsyncSession stand-in that records every ``update(...).values(...)``.

    The retry handler issues two kinds of statement: the generation state flip
    (via ``generations_storage.mark_retry_wait``/``mark_failed``) and the
    ``upload_jobs`` row update. Each is an ``Update`` whose target table and
    bound ``values`` are recovered from the compiled statement, so a test can
    assert the exact state tokens, attempt counter, ``next_attempt_at`` and
    ``last_error`` written without emulating PostgreSQL.
    """

    def __init__(self) -> None:
        self.generation_values: dict[str, Any] = {}
        self.job_values: dict[str, Any] = {}
        self.flushes = 0

    def begin(self) -> Any:
        @asynccontextmanager
        async def _begin() -> Any:
            yield self

        return _begin()

    async def execute(self, statement: Any) -> _RecordingResult:
        table = statement.table.name
        params = statement.compile().params
        if table == Generation.__tablename__:
            self.generation_values.update(params)
        elif table == UploadJob.__tablename__:
            self.job_values.update(params)
        else:  # pragma: no cover - defensive; no other table is touched.
            raise AssertionError(f"unexpected update against {table!r}")
        return _RecordingResult()

    async def flush(self) -> None:
        self.flushes += 1


class _RecordingSessionFactory:
    """Factory yielding a shared :class:`_RecordingSession` from ``begin()``.

    ``_handle_telegram_error`` opens ``session_factory()`` as an async context
    manager, then ``session.begin()`` as a second one. The same recording
    session is returned every time so the test reads back what the single
    transaction wrote; the session's ``begin`` is a no-op context manager (the
    fake applies writes immediately).
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


class _NullTelegram:
    """A :class:`TelegramPort` the retry handler never calls.

    ``_handle_telegram_error`` touches only the database; the worker still needs
    a port to be constructed, so this satisfies the protocol with methods that
    fail loudly if the retry path unexpectedly reaches Telegram.
    """

    async def send_document(
        self, channel_id: int, data: bytes, file_name: str, caption: str
    ) -> int:  # pragma: no cover - retry path never sends.
        raise AssertionError("retry handler must not send to Telegram")

    async def get_messages(
        self, channel_id: int, ids: list[int]
    ) -> list[TelegramMessage]:  # pragma: no cover
        raise AssertionError("retry handler must not read Telegram")

    async def download_document(
        self, channel_id: int, message_id: int
    ) -> bytes:  # pragma: no cover
        raise AssertionError("retry handler must not download from Telegram")

    async def delete_messages(
        self, channel_id: int, message_ids: list[int]
    ) -> None:  # pragma: no cover
        raise AssertionError("retry handler must not delete from Telegram")

    async def create_channel(self, title: str) -> int:  # pragma: no cover
        raise AssertionError("retry handler must not create channels")

    async def delete_channel(self, channel_id: int) -> None:  # pragma: no cover
        raise AssertionError("retry handler must not delete channels")

    async def get_session_state(self) -> SessionState:  # pragma: no cover
        return SessionState.CONNECTED

    async def is_connected(self) -> bool:  # pragma: no cover
        return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# A 16-byte master key (meets the TGOS3_MASTER_KEY minimum) for the worker's
# root-key derivation; the retry path never uses it, but construction does.
_MASTER_KEY = "0123456789abcdef"


def _make_worker(
    session_factory: _RecordingSessionFactory,
    *,
    max_attempts: int = MAX_UPLOAD_ATTEMPTS,
) -> UploadWorker:
    """Build an UploadWorker over the recording factory and the null port."""
    return UploadWorker(
        session_factory,  # type: ignore[arg-type]
        _NullTelegram(),
        _MASTER_KEY,
        max_attempts=max_attempts,
    )


def _make_job(*, attempts: int, staging_path: str | None = None) -> ClaimedJob:
    """Build a ClaimedJob carrying the attempt counter under test.

    Only the fields the retry handler reads matter — ``job_id``,
    ``generation_id``, ``attempts`` and ``staging_path`` — so the rest are given
    minimal, well-typed values.
    """
    return ClaimedJob(
        job_id=uuid.uuid4(),
        generation_id=uuid.uuid4(),
        bucket_id=uuid.uuid4(),
        channel_id=123,
        key="object-key",
        size=0,
        sha256=b"\x00" * 32,
        kek_wrapped=b"",
        chunk_records=512,
        staging_path=staging_path,
        content_type=None,
        user_meta={},
        etag=None,
        checksums={},
        last_modified=_FIXED_NOW,
        generation_number=0,
        attempts=attempts,
    )


# ---------------------------------------------------------------------------
# backoff_seconds — the pure exponential policy
# ---------------------------------------------------------------------------


def test_backoff_starts_at_initial_and_doubles() -> None:
    """The first retry waits INITIAL_BACKOFF_SECONDS; each later one doubles."""
    assert backoff_seconds(1) == INITIAL_BACKOFF_SECONDS
    assert backoff_seconds(2) == INITIAL_BACKOFF_SECONDS * 2
    assert backoff_seconds(3) == INITIAL_BACKOFF_SECONDS * 4
    assert backoff_seconds(4) == INITIAL_BACKOFF_SECONDS * 8
    assert backoff_seconds(5) == INITIAL_BACKOFF_SECONDS * 16


def test_backoff_clamps_non_positive_attempt_to_initial() -> None:
    """A zero/negative attempt number never underflows below the initial wait."""
    assert backoff_seconds(0) == INITIAL_BACKOFF_SECONDS
    assert backoff_seconds(-5) == INITIAL_BACKOFF_SECONDS


# ---------------------------------------------------------------------------
# _handle_telegram_error — retry scheduling (not yet exhausted)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("attempts_before", "expected_delay"),
    [
        (0, INITIAL_BACKOFF_SECONDS),  # 1st failure -> 1s
        (1, INITIAL_BACKOFF_SECONDS * 2),  # 2nd failure -> 2s
        (2, INITIAL_BACKOFF_SECONDS * 4),  # 3rd failure -> 4s
        (3, INITIAL_BACKOFF_SECONDS * 8),  # 4th failure -> 8s
    ],
)
async def test_transient_error_schedules_exponential_backoff(
    monkeypatch: pytest.MonkeyPatch,
    attempts_before: int,
    expected_delay: float,
) -> None:
    """A transient error moves the generation to RETRY_WAIT and backs off.

    The job returns to QUEUED with the attempt counter incremented and
    ``next_attempt_at`` set to the pinned now plus the exponential backoff for
    the new attempt number.

    Validates: Requirements 6.14
    """
    monkeypatch.setattr(upload_worker, "_utcnow", lambda: _FIXED_NOW)
    factory = _RecordingSessionFactory()
    worker = _make_worker(factory)
    job = _make_job(attempts=attempts_before)

    await worker._handle_telegram_error(
        job, TelegramUnavailableError("transport down")
    )

    session = factory.session
    # Generation flips to RETRY_WAIT, never FAILED, while attempts remain.
    assert session.generation_values["state"] == generations_storage.STATE_RETRY_WAIT
    # Job returns to the queue with an incremented attempt counter.
    assert session.job_values["state"] == generations_storage.JOB_STATE_QUEUED
    assert session.job_values["attempts"] == attempts_before + 1
    assert session.job_values["last_error"] == "transport down"
    # The scheduled wait equals the exponential backoff for the new attempt.
    scheduled = session.job_values["next_attempt_at"]
    assert (scheduled - _FIXED_NOW).total_seconds() == expected_delay


async def test_flood_wait_overrides_computed_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A FloodWaitError schedules Telegram's requested wait, not the doubling.

    Validates: Requirements 6.14
    """
    monkeypatch.setattr(upload_worker, "_utcnow", lambda: _FIXED_NOW)
    factory = _RecordingSessionFactory()
    worker = _make_worker(factory)
    # Second failure would compute a 2 s backoff; the flood wait asks for 42 s.
    job = _make_job(attempts=1)

    await worker._handle_telegram_error(job, FloodWaitError(retry_after=42))

    session = factory.session
    assert session.generation_values["state"] == generations_storage.STATE_RETRY_WAIT
    assert session.job_values["state"] == generations_storage.JOB_STATE_QUEUED
    scheduled = session.job_values["next_attempt_at"]
    assert (scheduled - _FIXED_NOW).total_seconds() == 42.0


# ---------------------------------------------------------------------------
# _handle_telegram_error — exhaustion (FAILED + staged file retained)
# ---------------------------------------------------------------------------


async def test_final_attempt_marks_failed_without_rescheduling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The attempt that reaches the budget marks FAILED, not RETRY_WAIT.

    With ``MAX_UPLOAD_ATTEMPTS`` attempts allowed, the failure that takes the
    counter to that bound flips the generation and the job to FAILED and records
    no ``next_attempt_at`` (nothing is rescheduled).

    Validates: Requirements 6.14
    """
    monkeypatch.setattr(upload_worker, "_utcnow", lambda: _FIXED_NOW)
    factory = _RecordingSessionFactory()
    worker = _make_worker(factory)
    # attempts_before == MAX - 1, so this failure is the Nth and exhausts.
    job = _make_job(attempts=MAX_UPLOAD_ATTEMPTS - 1)

    await worker._handle_telegram_error(
        job, TelegramUnavailableError("still down")
    )

    session = factory.session
    assert session.generation_values["state"] == generations_storage.STATE_FAILED
    assert session.job_values["state"] == upload_worker.JOB_STATE_FAILED
    assert session.job_values["attempts"] == MAX_UPLOAD_ATTEMPTS
    assert session.job_values["last_error"] == "still down"
    # Nothing is rescheduled once the generation is terminal.
    assert "next_attempt_at" not in session.job_values


async def test_failed_generation_retains_staged_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """On exhaustion the staged body is retained for inspection/retry.

    ``mark_failed`` only moves the generation's state; the retry handler never
    touches the staging file, so a real on-disk staged body is still present
    after the generation is marked FAILED.

    Validates: Requirements 6.14
    """
    monkeypatch.setattr(upload_worker, "_utcnow", lambda: _FIXED_NOW)
    staged = tmp_path / "staged-body.bin"
    staged.write_bytes(b"durable-on-disk body awaiting upload")

    factory = _RecordingSessionFactory()
    worker = _make_worker(factory)
    job = _make_job(
        attempts=MAX_UPLOAD_ATTEMPTS - 1, staging_path=str(staged)
    )

    await worker._handle_telegram_error(
        job, TelegramUnavailableError("still down")
    )

    assert factory.session.generation_values["state"] == (
        generations_storage.STATE_FAILED
    )
    # The staged file is left in place for operator-initiated retry.
    assert staged.exists()
    assert staged.read_bytes() == b"durable-on-disk body awaiting upload"


async def test_custom_max_attempts_is_respected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker configured with a lower budget fails earlier.

    With ``max_attempts=2`` the first failure still reschedules (attempt 1 < 2)
    but the second exhausts the budget and marks FAILED.

    Validates: Requirements 6.14
    """
    monkeypatch.setattr(upload_worker, "_utcnow", lambda: _FIXED_NOW)

    # First failure: attempt 1 of 2 -> still retrying.
    first_factory = _RecordingSessionFactory()
    first_worker = _make_worker(first_factory, max_attempts=2)
    await first_worker._handle_telegram_error(
        _make_job(attempts=0), TelegramUnavailableError("down")
    )
    assert first_factory.session.generation_values["state"] == (
        generations_storage.STATE_RETRY_WAIT
    )

    # Second failure: attempt 2 of 2 -> exhausted, FAILED.
    second_factory = _RecordingSessionFactory()
    second_worker = _make_worker(second_factory, max_attempts=2)
    await second_worker._handle_telegram_error(
        _make_job(attempts=1), TelegramUnavailableError("down")
    )
    assert second_factory.session.generation_values["state"] == (
        generations_storage.STATE_FAILED
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))

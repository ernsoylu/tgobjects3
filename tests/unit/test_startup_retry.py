"""Example-based tests for the startup database connection-retry logic.

Covers the connect step of the gateway startup sequence in
:func:`gateway.app.db.session.wait_for_database` (Requirement 1.5):

    WHEN the Gateway starts and PostgreSQL is not yet available, THE Gateway
    SHALL retry the connection up to 10 times at 3-second intervals; IF all 10
    attempts fail, THEN THE Gateway SHALL exit with a non-zero exit code and
    emit an error message indicating that the database is unreachable.

Two end-to-end scenarios are asserted against the real retry loop:

* **Recovers on the last attempt** — PostgreSQL is unavailable for 9 attempts
  and answers on attempt 10, so ``wait_for_database`` returns normally and the
  gateway proceeds to migrate/recover.
* **Exhausts the budget** — PostgreSQL fails all 10 attempts, so
  ``wait_for_database`` raises :class:`DatabaseUnreachableError`. That typed
  error is what the FastAPI lifespan lets propagate so ``main()`` exits with a
  non-zero code and a DB-unreachable message (``gateway/app/main.py``). The test
  pins both the non-zero-exit contract (the raised typed error) and the
  "database is unreachable" message.

The database is replaced with a fake async engine whose ``connect()`` fails a
configured number of times before succeeding; the real ``SELECT 1`` probe runs
against it. The retry interval is replaced with a recording stand-in injected in
place of ``asyncio.sleep`` so the production 3-second interval is asserted
exactly while no real time elapses.

**Validates: Requirements 1.5**
"""

from __future__ import annotations

from types import TracebackType
from typing import Any, cast

import pytest
from gateway.app.db import session as db_session
from gateway.app.db.session import (
    DB_CONNECT_MAX_ATTEMPTS,
    DB_CONNECT_RETRY_INTERVAL_SECONDS,
    DatabaseUnreachableError,
    wait_for_database,
)
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _connection_error() -> OperationalError:
    """Build a SQLAlchemy error of the kind a down PostgreSQL raises.

    ``wait_for_database`` only retries on :class:`SQLAlchemyError` / ``OSError``;
    using a real :class:`~sqlalchemy.exc.OperationalError` keeps the fake honest
    about which failures the loop is contracted to tolerate.
    """
    return OperationalError("SELECT 1", None, Exception("connection refused"))


class _FakeConnection:
    """Async-context-manager stand-in for a borrowed engine connection.

    Mirrors the slice of the connection surface the probe touches: it is entered
    with ``async with`` and its :meth:`execute` runs the ``SELECT 1`` probe.
    """

    def __init__(self) -> None:
        self.executed: list[str] = []

    async def __aenter__(self) -> _FakeConnection:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    async def execute(self, statement: Any) -> None:
        self.executed.append(str(statement))


class _FakeEngine:
    """A fake :class:`AsyncEngine` whose ``connect()`` fails then succeeds.

    ``fail_count`` leading calls to :meth:`connect` raise a connection error
    (PostgreSQL not yet available); every later call yields a working
    :class:`_FakeConnection`. The number of attempts is recorded so a test can
    assert exactly how many connects the retry loop made.
    """

    def __init__(self, fail_count: int) -> None:
        self._fail_count = fail_count
        self.connect_calls = 0

    def connect(self) -> _FakeConnection:
        self.connect_calls += 1
        if self.connect_calls <= self._fail_count:
            raise _connection_error()
        return _FakeConnection()


class _RecordingSleep:
    """An injectable ``asyncio.sleep`` that records waits and never really waits.

    Each requested interval is appended to :attr:`intervals` and the call returns
    immediately, so the retry schedule is asserted exactly without any real time
    elapsing.
    """

    def __init__(self) -> None:
        self.intervals: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.intervals.append(seconds)


@pytest.fixture
def recording_sleep(monkeypatch: pytest.MonkeyPatch) -> _RecordingSleep:
    """Replace ``asyncio.sleep`` inside the session module with a recorder."""
    sleep = _RecordingSleep()
    monkeypatch.setattr(db_session.asyncio, "sleep", sleep)
    return sleep


# ---------------------------------------------------------------------------
# Recovers before the budget is exhausted (gateway proceeds)
# ---------------------------------------------------------------------------


async def test_unavailable_for_nine_attempts_then_available_proceeds(
    recording_sleep: _RecordingSleep,
) -> None:
    """9 failures then success on attempt 10: the gateway proceeds.

    ``wait_for_database`` returns normally (no exception), which is what lets
    startup continue to the migrate/recover steps. Exactly 10 connects are made
    and exactly 9 retry waits occur, each at the 3-second interval.
    """
    engine = _FakeEngine(fail_count=DB_CONNECT_MAX_ATTEMPTS - 1)

    await wait_for_database(cast(AsyncEngine, engine))

    # Succeeded on the tenth and final attempt.
    assert engine.connect_calls == DB_CONNECT_MAX_ATTEMPTS
    # Nine failures -> nine waits; the successful attempt adds none.
    assert recording_sleep.intervals == [
        DB_CONNECT_RETRY_INTERVAL_SECONDS
    ] * (DB_CONNECT_MAX_ATTEMPTS - 1)
    assert all(
        interval == 3.0 for interval in recording_sleep.intervals
    )


async def test_available_on_first_attempt_proceeds_without_waiting(
    recording_sleep: _RecordingSleep,
) -> None:
    """A ready database connects on the first try and never sleeps."""
    engine = _FakeEngine(fail_count=0)

    await wait_for_database(cast(AsyncEngine, engine))

    assert engine.connect_calls == 1
    assert recording_sleep.intervals == []


# ---------------------------------------------------------------------------
# Budget exhausted (gateway exits non-zero)
# ---------------------------------------------------------------------------


async def test_ten_consecutive_failures_raises_unreachable(
    recording_sleep: _RecordingSleep,
) -> None:
    """10 straight failures exhaust the budget and raise the fatal typed error.

    ``DatabaseUnreachableError`` is the signal ``main()`` lets propagate to a
    non-zero process exit (``gateway/app/main.py``). The message names the
    database as unreachable, satisfying the "emit an error message indicating
    that the database is unreachable" half of Requirement 1.5. Ten connects are
    attempted, with a retry wait only *between* attempts (nine waits), so the
    loop does not sleep after the final failure.
    """
    engine = _FakeEngine(fail_count=DB_CONNECT_MAX_ATTEMPTS)

    with pytest.raises(DatabaseUnreachableError, match="unreachable") as exc_info:
        await wait_for_database(cast(AsyncEngine, engine))

    # Every attempt in the budget was spent.
    assert engine.connect_calls == DB_CONNECT_MAX_ATTEMPTS
    # No wait after the last failure: nine inter-attempt waits for ten tries.
    assert recording_sleep.intervals == [
        DB_CONNECT_RETRY_INTERVAL_SECONDS
    ] * (DB_CONNECT_MAX_ATTEMPTS - 1)
    # The original connection error is preserved as the cause for diagnostics.
    assert isinstance(exc_info.value.__cause__, OperationalError)


async def test_unreachable_error_is_runtime_error() -> None:
    """The fatal startup error is a RuntimeError subclass (fail-fast contract).

    ``main()`` relies on this propagating out of the lifespan as an unhandled
    exception to drive the non-zero exit; it must not be a quietly-caught
    library exception.
    """
    assert issubclass(DatabaseUnreachableError, RuntimeError)


# ---------------------------------------------------------------------------
# Retry-policy defaults match the requirement (10 attempts @ 3 s)
# ---------------------------------------------------------------------------


def test_retry_policy_defaults_match_requirement() -> None:
    """The documented defaults are 10 attempts at 3-second intervals."""
    assert DB_CONNECT_MAX_ATTEMPTS == 10
    assert DB_CONNECT_RETRY_INTERVAL_SECONDS == 3.0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))

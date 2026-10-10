"""Property test for the two-factor abort invariant (:mod:`gateway.app.admin.setup`).

This module hosts **Property 2** of the admin-dashboard design: *wrong-code
attempts abort after exactly 3 failures with a single ``reset()``*. The design's
"interactive Telegram login" sequence diagram places the attempt counting, abort,
and ``reset()`` in ``admin/setup.py`` itself (:func:`_submit_two_factor`,
:func:`_abort_login`); the Telethon adapter only raises
:class:`~gateway.app.admin.setup.TwoFactorFailedError` for a wrong code. This
test therefore drives the *real* setup router over its ASGI surface with a fake
:class:`~gateway.app.admin.setup.TelegramLoginPort` whose ``submit_code`` fails a
configurable number of times before (optionally) accepting a correct code, and
asserts the whole invariant end-to-end:

* While the running failure count is ``< MAX_TWO_FACTOR_ATTEMPTS`` (3), each wrong
  code keeps the step prompting — HTTP 200 with ``needs_code=True`` and
  ``attempts_remaining`` decremented by exactly one per failure (Requirement 2.4).
* The 3rd failure exceeds the limit: the step aborts with HTTP 401, calls the
  port's ``reset()`` **exactly once**, and clears the login state so a subsequent
  ``submit_code`` with no fresh ``start_login`` is rejected with HTTP 409
  (Requirements 2.4, 2.5).

Hypothesis varies the number of consecutive wrong codes (and whether a correct
code is interleaved before exhaustion) across ≥ 100 iterations so the invariant
is checked over the whole relevant input space, per the design's "Backend unit"
testing strategy.

Following the sibling wizard unit tests (:mod:`tests.unit.test_setup_wizard`),
these tests avoid a real database and the unpinned ``httpx``/``TestClient``: the
router is driven directly over ASGI by a small in-process driver, and the
SQLAlchemy session factory is a hand-written fake. Only the ``/setup/telegram``
step is exercised, so the fake session is never actually queried here.

Validates: Requirements 2.4, 2.5
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, cast

from fastapi import FastAPI
from gateway.app.admin.setup import (
    MAX_TWO_FACTOR_ATTEMPTS,
    TelegramLoginResult,
    TwoFactorFailedError,
    create_setup_router,
)
from gateway.app.config import Settings
from gateway.app.db.session import AsyncSessionFactory
from gateway.app.telegram.port import SessionState, TelegramMessage, TelegramPort
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from starlette.types import ASGIApp, Message, Scope

_MASTER_KEY = "unit-test-master-key-0123456789"
_CORRECT_CODE = "right-code"


# ---------------------------------------------------------------------------
# Fakes: a session factory that is never queried, and the two Telegram ports
# ---------------------------------------------------------------------------


class _FakeSessionFactory:
    """A stand-in for :class:`AsyncSessionFactory`; the telegram step never reads it.

    The two-factor path exercised here issues no database query, so ``session()``
    yields an object that asserts if anything unexpectedly touches it.
    """

    @asynccontextmanager
    async def session(self) -> AsyncIterator[Any]:
        yield _UnusedSession()


class _UnusedSession:
    """A session that fails loudly if the two-factor path ever queries the DB."""

    async def execute(self, statement: Any) -> Any:  # pragma: no cover - defensive
        raise AssertionError("the two-factor step must not query the database")

    def add(self, obj: Any) -> None:  # pragma: no cover - defensive
        raise AssertionError("the two-factor step must not write the database")


class _FakeTelegramPort:
    """A :class:`TelegramPort` the two-factor step never calls.

    Present only to satisfy the context the router factory reads; every method
    asserts because none of them is on the ``/setup/telegram`` path.
    """

    async def send_document(
        self, channel_id: int, data: bytes, file_name: str, caption: str
    ) -> int:  # pragma: no cover - not used by the two-factor step
        raise AssertionError("send_document is not used by the two-factor step")

    async def get_messages(
        self, channel_id: int, ids: list[int]
    ) -> list[TelegramMessage]:  # pragma: no cover - not used
        raise AssertionError("get_messages is not used by the two-factor step")

    async def download_document(
        self, channel_id: int, message_id: int
    ) -> bytes:  # pragma: no cover - not used
        raise AssertionError("download_document is not used by the two-factor step")

    async def delete_messages(
        self, channel_id: int, message_ids: list[int]
    ) -> None:  # pragma: no cover - not used
        raise AssertionError("delete_messages is not used by the two-factor step")

    async def create_channel(
        self, title: str
    ) -> int:  # pragma: no cover - not used
        raise AssertionError("create_channel is not used by the two-factor step")

    async def delete_channel(
        self, channel_id: int
    ) -> None:  # pragma: no cover - not used
        raise AssertionError("delete_channel is not used by the two-factor step")

    async def get_session_state(
        self,
    ) -> SessionState:  # pragma: no cover - not used
        return SessionState.CONNECTED

    async def is_connected(self) -> bool:  # pragma: no cover - not used
        return True


class _FakeLoginPort:
    """A :class:`TelegramLoginPort` whose ``submit_code`` fails a fixed number of times.

    ``start_login`` always asks for a code. The first ``fail_count`` calls to
    ``submit_code`` raise :class:`TwoFactorFailedError` so the wizard counts each
    as a failed attempt; any call after that authenticates (returning session
    credentials). ``reset`` records how many times the wizard discarded the
    in-progress login — the invariant demands this is exactly one on abort.
    """

    def __init__(self, fail_count: int) -> None:
        self._fail_count = fail_count
        self.reset_calls = 0
        self.submit_calls = 0

    async def start_login(
        self, api_id: int, api_hash: str, login: str
    ) -> TelegramLoginResult:
        return TelegramLoginResult(authenticated=False, needs_code=True)

    async def submit_code(self, code: str) -> TelegramLoginResult:
        self.submit_calls += 1
        if self.submit_calls <= self._fail_count:
            raise TwoFactorFailedError
        return TelegramLoginResult(
            authenticated=True,
            needs_code=False,
            session_credentials="session-blob",
        )

    async def reset(self) -> None:
        self.reset_calls += 1


# ---------------------------------------------------------------------------
# Context + app wiring
# ---------------------------------------------------------------------------


@dataclass
class _FakeContext:
    """The subset of :class:`~gateway.app.main.GatewayContext` the wizard reads."""

    settings: Settings
    session_factory: AsyncSessionFactory
    telegram: TelegramPort


def _settings() -> Settings:
    """Build a validated :class:`Settings` with the test master key."""
    return Settings(
        TGOS3_MASTER_KEY=_MASTER_KEY,
        DATABASE_URL="postgresql+asyncpg://localhost/tgos3",
    )


def _setup_app(login_port: _FakeLoginPort) -> FastAPI:
    """Mount the real setup router wired to the given fake login port."""
    context = _FakeContext(
        settings=_settings(),
        session_factory=cast(AsyncSessionFactory, _FakeSessionFactory()),
        telegram=cast(TelegramPort, _FakeTelegramPort()),
    )
    app = FastAPI()
    app.include_router(
        create_setup_router(
            context=cast(Any, context),
            login_factory=lambda _settings: login_port,
        )
    )
    return app


def _telegram_begin() -> dict[str, Any]:
    """A credentials POST body that begins a fresh login."""
    return {"api_id": 12345, "api_hash": "deadbeef", "login": "+15551234567"}


def _telegram_code(code: str) -> dict[str, Any]:
    """A credentials POST body that submits a two-factor code."""
    return {**_telegram_begin(), "code": code}


# ---------------------------------------------------------------------------
# In-process ASGI driver (no httpx / TestClient dependency)
# ---------------------------------------------------------------------------


class _Response:
    """A collected ASGI HTTP response: status, headers and body bytes."""

    def __init__(self) -> None:
        self.status: int = 0
        self.headers: dict[str, str] = {}
        self.body: bytes = b""

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


async def _request(
    app: ASGIApp,
    method: str,
    path: str,
    *,
    body: Any = None,
) -> _Response:
    """Drive one HTTP request through ``app`` over ASGI and collect the response."""
    raw_headers: list[tuple[bytes, bytes]] = []
    payload = b"" if body is None else json.dumps(body).encode("utf-8")
    if body is not None:
        raw_headers.append((b"content-type", b"application/json"))
        raw_headers.append((b"content-length", str(len(payload)).encode()))

    raw_path, _, query = path.partition("?")
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "path": raw_path,
        "raw_path": raw_path.encode(),
        "query_string": query.encode("latin-1"),
        "root_path": "",
        "scheme": "http",
        "headers": raw_headers,
        "client": ("testclient", 12345),
        "server": ("testserver", 80),
    }
    response = _Response()
    body_sent = False

    async def receive() -> Message:
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {"type": "http.request", "body": payload, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        if message["type"] == "http.response.start":
            response.status = int(message["status"])
            response.headers = {
                k.decode("latin-1").lower(): v.decode("latin-1")
                for k, v in message.get("headers", [])
            }
        elif message["type"] == "http.response.body":
            response.body += bytes(message.get("body", b""))

    await app(scope, receive, send)
    return response


# ---------------------------------------------------------------------------
# Property 2: abort after exactly 3 failures with a single reset()
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(wrong_codes=st.integers(min_value=0, max_value=12))
async def test_wrong_codes_abort_after_three_failures_with_single_reset(
    wrong_codes: int,
) -> None:
    """Property 2: wrong codes keep prompting until the 3rd failure aborts once.

    For any number of consecutive wrong codes submitted to the Telegram setup
    step, the step keeps prompting while the running failure count is below
    :data:`MAX_TWO_FACTOR_ATTEMPTS`, and the failure that reaches the limit
    aborts with HTTP 401, calls the port's ``reset()`` exactly once, and clears
    the login state (a subsequent bare ``submit_code`` is rejected with 409).

    Validates: Requirements 2.4, 2.5
    """
    # The fake rejects exactly the wrong codes submitted; a correct code after
    # them (submission ``wrong_codes + 1``) would authenticate — but only if the
    # step has not already aborted at the 3rd failure.
    login = _FakeLoginPort(fail_count=wrong_codes)
    app = _setup_app(login)

    # Begin login: the step asks for a code and the attempt budget is full.
    begin = await _request(app, "POST", "/setup/telegram", body=_telegram_begin())
    assert begin.status == 200
    assert begin.json()["needs_code"] is True
    assert begin.json()["attempts_remaining"] == MAX_TWO_FACTOR_ATTEMPTS

    aborted = False
    for submitted in range(1, wrong_codes + 1):
        resp = await _request(
            app, "POST", "/setup/telegram", body=_telegram_code(f"wrong-{submitted}")
        )
        if submitted < MAX_TWO_FACTOR_ATTEMPTS:
            # Below the limit: keep prompting, one fewer attempt each time.
            assert resp.status == 200
            assert resp.json()["needs_code"] is True
            assert resp.json()["authenticated"] is False
            assert (
                resp.json()["attempts_remaining"]
                == MAX_TWO_FACTOR_ATTEMPTS - submitted
            )
            assert login.reset_calls == 0
        else:
            # The failure that reaches the limit aborts with 401, resets once.
            assert resp.status == 401
            assert login.submit_calls == MAX_TWO_FACTOR_ATTEMPTS
            assert login.reset_calls == 1
            aborted = True
            break

    if not aborted:
        # Fewer than 3 wrong codes were submitted: no abort, no reset, and the
        # login is still awaiting a code — a correct code now authenticates.
        assert login.reset_calls == 0
        ok = await _request(
            app, "POST", "/setup/telegram", body=_telegram_code(_CORRECT_CODE)
        )
        assert ok.status == 200
        assert ok.json()["authenticated"] is True
        return

    # Having aborted, the login state is cleared: a code with no fresh
    # start_login is rejected and triggers neither another submit nor reset.
    stray = await _request(
        app, "POST", "/setup/telegram", body=_telegram_code(_CORRECT_CODE)
    )
    assert stray.status == 409
    assert login.submit_calls == MAX_TWO_FACTOR_ATTEMPTS
    assert login.reset_calls == 1


@settings(
    max_examples=100,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(wrong_before_correct=st.integers(min_value=0, max_value=2))
async def test_correct_code_before_limit_authenticates_without_reset(
    wrong_before_correct: int,
) -> None:
    """A correct code before the 3rd failure authenticates and never resets.

    While the running failure count stays below the limit, the step keeps
    prompting; a correct code submitted at that point authenticates with no
    ``reset()`` and leaves no residual abort.

    Validates: Requirements 2.4, 2.5
    """
    login = _FakeLoginPort(fail_count=wrong_before_correct)
    app = _setup_app(login)

    await _request(app, "POST", "/setup/telegram", body=_telegram_begin())

    for submitted in range(1, wrong_before_correct + 1):
        resp = await _request(
            app, "POST", "/setup/telegram", body=_telegram_code(f"wrong-{submitted}")
        )
        assert resp.status == 200
        assert resp.json()["needs_code"] is True
        assert (
            resp.json()["attempts_remaining"] == MAX_TWO_FACTOR_ATTEMPTS - submitted
        )

    ok = await _request(
        app, "POST", "/setup/telegram", body=_telegram_code(_CORRECT_CODE)
    )
    assert ok.status == 200
    assert ok.json()["authenticated"] is True
    assert login.reset_calls == 0

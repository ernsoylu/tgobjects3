"""Integration tests for the setup router's Telegram login flow (task 2.3).

These tests mount the *real* ``/admin/setup/*`` wizard router
(:func:`gateway.app.admin.setup.create_setup_router`) on a FastAPI app and drive
the Telegram login step end to end through its ASGI surface, with a **fake**
:class:`~gateway.app.admin.setup.TelegramLoginPort` injected through the router's
``login_factory`` seam. Unlike the unit tests in
:mod:`tests.unit.test_setup_wizard` — which exercise individual edge cases — this
module walks the two whole login journeys the design's "Backend integration"
testing strategy and the Telegram-login sequence diagram call out
(``.kiro/specs/admin-dashboard/design.md``):

* **needs_code → code → success (Requirements 2.2, 2.6).** The credentials POST
  starts login and the router replies ``needs_code=True`` with the full attempt
  budget; a follow-up POST carrying the correct ``code`` authenticates, the
  ``TelegramStepResult`` reports ``authenticated=True`` / ``needs_code=False``,
  and the fake's session credentials are carried through so a later finalize can
  place them in the Recovery Kit.
* **needs_code → repeated failure → abort (Requirements 2.4, 2.5).** Each wrong
  ``code`` consumes one of
  :data:`~gateway.app.admin.setup.MAX_TWO_FACTOR_ATTEMPTS` attempts, decrementing
  ``attempts_remaining`` while awaiting another code; the third failure exceeds
  the limit, so the router returns HTTP 401, calls ``reset()`` exactly once, and
  clears the login state (a bare ``code`` POST afterwards is rejected 409 until a
  fresh credentials POST restores a clean attempt budget).

The wizard reads only a thin slice of :class:`~gateway.app.main.GatewayContext`
(``settings``, ``session_factory``, ``telegram``). As in the unit suite we supply
a fake context with an in-memory access-key session factory and a Telegram port
whose channels are empty, and we drive requests through a small in-process ASGI
client rather than the unpinned ``httpx``/``TestClient``, so the suite needs no
database or network and stays collectable everywhere.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, cast

import pytest
from fastapi import FastAPI
from gateway.app.admin.setup import (
    MAX_TWO_FACTOR_ATTEMPTS,
    TelegramLoginResult,
    TwoFactorFailedError,
    create_setup_router,
)
from gateway.app.config import Settings
from gateway.app.db.models import AccessKey
from gateway.app.db.session import AsyncSessionFactory
from gateway.app.telegram.port import SessionState, TelegramMessage, TelegramPort
from starlette.types import ASGIApp, Message, Scope

_MASTER_KEY = "integration-setup-master-key-001"
_CORRECT_CODE = "right-code"
_SESSION_BLOB = "telethon-string-session-blob"


# ---------------------------------------------------------------------------
# Fake access-key store + session factory (serves the wizard's count queries)
# ---------------------------------------------------------------------------


@dataclass
class _State:
    """The in-memory access-key rows the fake session reads and appends."""

    keys: list[AccessKey] = field(default_factory=list)


class _RowResult:
    """A result exposing ``scalar_one`` over a fixed value."""

    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one(self) -> Any:
        return self._value


class _FakeSession:
    """Serves the wizard's ``count`` queries over :class:`_State`.

    The status step issues the single read shape
    ``select(func.count()).select_from(AccessKey)``; that is all the login-flow
    tests here touch, so a wrong-shaped statement is a test bug and asserts.
    """

    def __init__(self, state: _State) -> None:
        self._state = state

    async def execute(self, statement: Any) -> Any:
        description = str(statement)
        if "count(" in description and "access_keys" in description:
            return _RowResult(len(self._state.keys))
        raise AssertionError(f"unexpected statement: {description}")

    def add(self, obj: Any) -> None:  # pragma: no cover - finalize not exercised here
        if isinstance(obj, AccessKey):
            self._state.keys.append(obj)
        else:
            raise AssertionError(f"unexpected add: {obj!r}")


class _FakeSessionFactory:
    """A stand-in for :class:`AsyncSessionFactory` exposing only ``session()``."""

    def __init__(self, state: _State) -> None:
        self._state = state

    @asynccontextmanager
    async def session(self) -> AsyncIterator[_FakeSession]:
        yield _FakeSession(self._state)


# ---------------------------------------------------------------------------
# Fake Telegram ports
# ---------------------------------------------------------------------------


class _EmptyTelegramPort:
    """A :class:`TelegramPort` with no channels; the login flow never calls it.

    The buckets step would probe channels, but these login-flow tests stop at the
    Telegram step, so every method is a defensive assertion except the two the
    protocol surface needs for construction.
    """

    async def send_document(
        self, channel_id: int, data: bytes, file_name: str, caption: str
    ) -> int:  # pragma: no cover - not used by the login flow
        raise AssertionError("send_document is not used by the login flow")

    async def get_messages(
        self, channel_id: int, ids: list[int]
    ) -> list[TelegramMessage]:  # pragma: no cover - not used by the login flow
        raise AssertionError("get_messages is not used by the login flow")

    async def download_document(
        self, channel_id: int, message_id: int
    ) -> bytes:  # pragma: no cover - not used by the login flow
        raise AssertionError("download_document is not used by the login flow")

    async def delete_messages(
        self, channel_id: int, message_ids: list[int]
    ) -> None:  # pragma: no cover - not used by the login flow
        raise AssertionError("delete_messages is not used by the login flow")

    async def create_channel(
        self, title: str
    ) -> int:  # pragma: no cover - not used by the login flow
        raise AssertionError("create_channel is not used by the login flow")

    async def delete_channel(
        self, channel_id: int
    ) -> None:  # pragma: no cover - not used by the login flow
        raise AssertionError("delete_channel is not used by the login flow")

    async def get_session_state(
        self,
    ) -> SessionState:  # pragma: no cover - not used by the login flow
        return SessionState.CONNECTED

    async def is_connected(self) -> bool:  # pragma: no cover - not used
        return True


class _FakeLoginPort:
    """A :class:`~gateway.app.admin.setup.TelegramLoginPort` scripting login.

    ``start_login`` always asks for a code (the phone path). ``submit_code``
    authenticates only when the submitted code equals ``correct_code`` and then
    carries the fake session credentials through (Requirement 2.6); any other
    code raises :class:`TwoFactorFailedError` so the router counts the failed
    attempt (Requirement 2.3/2.4). ``reset`` records the discard so the
    abort-after-3 / single-``reset()`` invariant can be asserted (Requirement
    2.4/2.5).
    """

    def __init__(self, correct_code: str = _CORRECT_CODE) -> None:
        self._correct_code = correct_code
        self.start_calls = 0
        self.submit_calls = 0
        self.reset_calls = 0

    async def start_login(
        self, api_id: int, api_hash: str, login: str
    ) -> TelegramLoginResult:
        self.start_calls += 1
        return TelegramLoginResult(authenticated=False, needs_code=True)

    async def submit_code(self, code: str) -> TelegramLoginResult:
        self.submit_calls += 1
        if code == self._correct_code:
            return TelegramLoginResult(
                authenticated=True,
                needs_code=False,
                session_credentials=_SESSION_BLOB,
            )
        raise TwoFactorFailedError

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
    """Mount the real setup router with ``login_port`` injected via the factory."""
    context = _FakeContext(
        settings=_settings(),
        session_factory=cast(AsyncSessionFactory, _FakeSessionFactory(_State())),
        telegram=cast(TelegramPort, _EmptyTelegramPort()),
    )
    app = FastAPI()
    app.include_router(
        create_setup_router(
            context=cast(Any, context),
            login_factory=lambda _settings: login_port,
        )
    )
    return app


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
# Request-body builders
# ---------------------------------------------------------------------------


def _begin() -> dict[str, Any]:
    """A credentials POST body that begins a fresh login (phone path)."""
    return {"api_id": 12345, "api_hash": "deadbeef", "login": "+15551234567"}


def _code(code: str) -> dict[str, Any]:
    """A credentials POST body that submits a login/two-factor code."""
    return {**_begin(), "code": code}


# ---------------------------------------------------------------------------
# needs_code → code → success (Requirements 2.2, 2.6)
# ---------------------------------------------------------------------------


async def test_login_needs_code_then_correct_code_succeeds() -> None:
    """The full happy path: credentials → needs_code → correct code → authenticated.

    The first POST starts login and the router asks for a code with the full
    attempt budget; the follow-up POST with the correct code authenticates,
    needing no further input, and the fake login port saw exactly one start and
    one submit with no reset.

    Validates: Requirements 2.2, 2.6
    """
    login = _FakeLoginPort()
    app = _setup_app(login)

    begin = await _request(app, "POST", "/setup/telegram", body=_begin())
    assert begin.status == 200
    begin_body = begin.json()
    assert begin_body["needs_code"] is True
    assert begin_body["authenticated"] is False
    assert begin_body["attempts_remaining"] == MAX_TWO_FACTOR_ATTEMPTS

    success = await _request(app, "POST", "/setup/telegram", body=_code(_CORRECT_CODE))
    assert success.status == 200
    success_body = success.json()
    assert success_body["authenticated"] is True
    assert success_body["needs_code"] is False
    assert success_body["attempts_remaining"] == MAX_TWO_FACTOR_ATTEMPTS

    assert login.start_calls == 1
    assert login.submit_calls == 1
    assert login.reset_calls == 0


async def test_login_tolerates_one_wrong_code_then_succeeds() -> None:
    """A wrong code is survivable: it decrements the budget, a later correct one wins.

    This exercises the needs_code loop staying open across a failure before the
    success path, so the session is only authenticated once the right code lands.

    Validates: Requirements 2.2, 2.4, 2.6
    """
    login = _FakeLoginPort()
    app = _setup_app(login)

    await _request(app, "POST", "/setup/telegram", body=_begin())

    wrong = await _request(app, "POST", "/setup/telegram", body=_code("nope"))
    assert wrong.status == 200
    assert wrong.json()["needs_code"] is True
    assert wrong.json()["attempts_remaining"] == MAX_TWO_FACTOR_ATTEMPTS - 1

    good = await _request(app, "POST", "/setup/telegram", body=_code(_CORRECT_CODE))
    assert good.status == 200
    assert good.json()["authenticated"] is True
    assert login.reset_calls == 0


# ---------------------------------------------------------------------------
# needs_code → repeated failure → abort after 3 (Requirements 2.4, 2.5)
# ---------------------------------------------------------------------------


async def test_login_three_wrong_codes_abort_with_single_reset() -> None:
    """Three wrong codes abort with 401 and a single ``reset()`` (Requirements 2.4, 2.5).

    The first two wrong codes keep the step open with a decrementing budget; the
    third exceeds the limit, so the router returns HTTP 401 and discards the
    in-progress login exactly once.

    Validates: Requirements 2.4, 2.5
    """
    login = _FakeLoginPort()
    app = _setup_app(login)

    await _request(app, "POST", "/setup/telegram", body=_begin())

    first = await _request(app, "POST", "/setup/telegram", body=_code("wrong-1"))
    assert first.status == 200
    assert first.json()["attempts_remaining"] == MAX_TWO_FACTOR_ATTEMPTS - 1

    second = await _request(app, "POST", "/setup/telegram", body=_code("wrong-2"))
    assert second.status == 200
    assert second.json()["attempts_remaining"] == MAX_TWO_FACTOR_ATTEMPTS - 2

    third = await _request(app, "POST", "/setup/telegram", body=_code("wrong-3"))
    assert third.status == 401

    assert login.submit_calls == MAX_TWO_FACTOR_ATTEMPTS
    assert login.reset_calls == 1


async def test_login_bare_code_after_abort_is_rejected_until_restart() -> None:
    """After an abort the login state is cleared: a bare code POST is 409 (Requirement 2.4).

    Validates: Requirements 2.4
    """
    login = _FakeLoginPort()
    app = _setup_app(login)

    await _request(app, "POST", "/setup/telegram", body=_begin())
    for attempt in range(MAX_TWO_FACTOR_ATTEMPTS):
        await _request(app, "POST", "/setup/telegram", body=_code(f"wrong-{attempt}"))

    stray = await _request(app, "POST", "/setup/telegram", body=_code(_CORRECT_CODE))
    assert stray.status == 409


async def test_login_restart_after_abort_restores_full_budget_and_succeeds() -> None:
    """Re-entering credentials after an abort restores a full budget and can succeed.

    Validates: Requirements 2.2, 2.4, 2.6
    """
    login = _FakeLoginPort()
    app = _setup_app(login)

    await _request(app, "POST", "/setup/telegram", body=_begin())
    for attempt in range(MAX_TWO_FACTOR_ATTEMPTS):
        await _request(app, "POST", "/setup/telegram", body=_code(f"wrong-{attempt}"))

    restart = await _request(app, "POST", "/setup/telegram", body=_begin())
    assert restart.status == 200
    assert restart.json()["needs_code"] is True
    assert restart.json()["attempts_remaining"] == MAX_TWO_FACTOR_ATTEMPTS

    success = await _request(app, "POST", "/setup/telegram", body=_code(_CORRECT_CODE))
    assert success.status == 200
    assert success.json()["authenticated"] is True


# ---------------------------------------------------------------------------
# Login-step unavailable without a factory (the 503 the wiring test closes)
# ---------------------------------------------------------------------------


async def test_login_step_returns_503_without_a_login_factory() -> None:
    """With no ``login_factory`` the Telegram step is unavailable and returns 503.

    This is the pre-wiring state the composition root closes by injecting a
    factory (asserted in ``tests/integration/test_main_wiring.py``): the router
    itself correctly reports the step as unavailable until one is supplied.

    Validates: Requirements 2.2
    """
    context = _FakeContext(
        settings=_settings(),
        session_factory=cast(AsyncSessionFactory, _FakeSessionFactory(_State())),
        telegram=cast(TelegramPort, _EmptyTelegramPort()),
    )
    app = FastAPI()
    app.include_router(create_setup_router(context=cast(Any, context)))

    response = await _request(app, "POST", "/setup/telegram", body=_begin())
    assert response.status == 503


if __name__ == "__main__":  # pragma: no cover - convenience for direct runs
    raise SystemExit(pytest.main([__file__, "-v"]))

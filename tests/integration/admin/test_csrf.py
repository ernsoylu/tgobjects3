"""Backend integration test for CSRF enforcement (task 4.4, Property 3).

This module is the *backend half* of the design's **Property 3** (``A mutating
request without the bootstrapped CSRF token is rejected and applies no change``,
``.kiro/specs/admin-dashboard/design.md``). Where the frontend companion
(:mod:`console` Vitest unit over ``client.ts``/``AuthProvider``) proves the
Console only ever *sends* the bootstrapped token on unsafe methods, this test
proves the server *enforces* it: it mounts the real admin router
(:func:`gateway.app.admin.router.create_admin_router`) and drives it over its
ASGI surface, asserting the two directions the property names.

The exercised mutating endpoint is ``POST /admin/keys`` — the access-key create
(Requirement 17.2). It is a genuine state-mutating admin request backed by the
access-key store, so it can be driven with a fake
:class:`~gateway.app.db.session.AsyncSessionFactory` (no PostgreSQL, no
Telegram): the "resource" whose change the property tracks is the set of
``access_keys`` rows, which the fake session appends on a successful create and
leaves untouched on a rejected one.

What the router enforces (Requirement 11.6 / design error table row for 403):

* Every admin request requires HTTP Basic auth; a successful Basic auth mints a
  per-session CSRF token and returns it in the ``X-CSRF-Token`` *response*
  header (the "bootstrap").
* A state-mutating request (POST/PUT/DELETE/PATCH) must echo that token in the
  ``X-CSRF-Token`` *request* header; a missing or mismatched token is rejected
  with HTTP 403 **before** the handler runs — so the targeted resource is
  unchanged.

So the test asserts:

* **Before bootstrap** (a mutating POST with correct Basic auth but *no* CSRF
  token) → HTTP 403, and the access-key store gained no row.
* **After bootstrap** (a GET surfaces ``X-CSRF-Token``; the same mutating POST
  carries that token) → HTTP 201, and the store gained exactly one row.

Validates: Requirements 11.6
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, cast

import pytest
from fastapi import FastAPI
from gateway.app.admin.router import (
    ADMIN_PASSWORD_ENV,
    ADMIN_USERNAME,
    CSRF_HEADER,
    create_admin_router,
)
from gateway.app.config import Settings
from gateway.app.db.models import AccessKey
from gateway.app.db.session import AsyncSessionFactory
from gateway.app.storage.staging import StagingReservation
from starlette.types import ASGIApp, Message, Scope

_MASTER_KEY = "integration-csrf-master-key-0001"
_ADMIN_PASSWORD = "s3cret-admin-password"


# ---------------------------------------------------------------------------
# Fake access-key store + session factory
# ---------------------------------------------------------------------------
#
# ``POST /admin/keys`` only ``add``s an :class:`AccessKey`; ``GET /admin/keys``
# (used here as the cheap authenticated read that bootstraps the token) issues a
# single ``select(AccessKey)`` the fake answers from the same in-memory list.


@dataclass
class _State:
    """The in-memory ``access_keys`` rows — the resource Property 3 tracks."""

    keys: list[AccessKey] = field(default_factory=list)


class _ScalarsResult:
    """A result exposing ``scalars().all()`` over a fixed sequence."""

    def __init__(self, values: list[Any]) -> None:
        self._values = values

    def scalars(self) -> _ScalarsResult:
        return self

    def all(self) -> list[Any]:
        return list(self._values)


class _FakeSession:
    """Serves the access-key list read and records appends on create.

    The key endpoints touch exactly two statement shapes here: the list query
    (``select(AccessKey)...``) and the create ``add``. Any other statement is a
    test bug and asserts loudly rather than silently passing.
    """

    def __init__(self, state: _State) -> None:
        self._state = state

    async def execute(self, statement: Any) -> Any:
        description = str(statement)
        if "access_keys" in description and description.lstrip().upper().startswith(
            "SELECT"
        ):
            return _ScalarsResult(list(self._state.keys))
        raise AssertionError(f"unexpected statement: {description}")

    def add(self, obj: Any) -> None:
        if isinstance(obj, AccessKey):
            self._state.keys.append(obj)
        else:  # pragma: no cover - defensive
            raise AssertionError(f"unexpected add: {obj!r}")


class _FakeSessionFactory:
    """A stand-in for :class:`AsyncSessionFactory` exposing only ``session()``.

    The admin router's write path opens the session as an ``async with`` context
    manager and relies on the manager committing on a clean exit; the fake simply
    yields a session over the shared :class:`_State`, so an appended row is
    visible to the next request without a real database.
    """

    def __init__(self, state: _State) -> None:
        self._state = state

    @asynccontextmanager
    async def session(self) -> AsyncIterator[_FakeSession]:
        yield _FakeSession(self._state)


# ---------------------------------------------------------------------------
# Context + app wiring
# ---------------------------------------------------------------------------


@dataclass
class _FakeContext:
    """The slice of :class:`~gateway.app.main.GatewayContext` the router reads.

    ``create_admin_router`` reads ``settings`` (for the master key the key-wrap
    uses and the runtime-settings seed), ``session_factory`` (the access-key
    store), and ``staging`` (the live reservation the settings PUT cross-checks).
    """

    settings: Settings
    session_factory: AsyncSessionFactory
    staging: StagingReservation


def _settings() -> Settings:
    """Build a validated :class:`Settings` with the test master key."""
    return Settings(
        TGOS3_MASTER_KEY=_MASTER_KEY,
        DATABASE_URL="postgresql+asyncpg://localhost/tgos3",
    )


@pytest.fixture
def _admin_password_env() -> Iterator[None]:
    """Set ``TGOS3_ADMIN_PASSWORD`` for the duration of a test.

    ``create_admin_router`` reads the admin password from the environment at
    construction time (Requirement 17.5/17.6) and refuses to build without it, so
    the router must be constructed while this variable is present. The original
    value is restored afterwards so the process environment is left untouched.
    """
    previous = os.environ.get(ADMIN_PASSWORD_ENV)
    os.environ[ADMIN_PASSWORD_ENV] = _ADMIN_PASSWORD
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(ADMIN_PASSWORD_ENV, None)
        else:
            os.environ[ADMIN_PASSWORD_ENV] = previous


def _admin_app(state: _State) -> FastAPI:
    """Mount the real admin router over the fake access-key store."""
    context = _FakeContext(
        settings=_settings(),
        session_factory=cast(AsyncSessionFactory, _FakeSessionFactory(state)),
        staging=StagingReservation(_settings().STAGING_MAX_SIZE),
    )
    app = FastAPI()
    app.include_router(create_admin_router(context=cast(Any, context)))
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
    headers: dict[str, str] | None = None,
    body: Any = None,
) -> _Response:
    """Drive one HTTP request through ``app`` over ASGI and collect the response."""
    raw_headers: list[tuple[bytes, bytes]] = []
    for name, value in (headers or {}).items():
        raw_headers.append((name.lower().encode("latin-1"), value.encode("latin-1")))

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


def _basic_auth(password: str = _ADMIN_PASSWORD) -> str:
    """Return the ``Authorization`` header value for admin Basic auth."""
    import base64

    raw = f"{ADMIN_USERNAME}:{password}".encode()
    return "Basic " + base64.b64encode(raw).decode("ascii")


# ---------------------------------------------------------------------------
# Property 3 (backend half): mutating request rejected without CSRF token
# ---------------------------------------------------------------------------


async def test_mutating_request_without_csrf_token_is_rejected_and_changes_nothing(
    _admin_password_env: None,
) -> None:
    """A pre-bootstrap mutating POST is 403 and the access-key store is unchanged.

    With correct Basic auth but *no* ``X-CSRF-Token``, ``POST /admin/keys`` is
    rejected with HTTP 403 before the create handler runs, so no ``AccessKey`` row
    is appended — the targeted resource is unchanged (Property 3, Requirement
    11.6).

    Validates: Requirements 11.6
    """
    state = _State()
    app = _admin_app(state)

    rejected = await _request(
        app,
        "POST",
        "/admin/keys",
        headers={"Authorization": _basic_auth()},
        body={"label": "ci"},
    )

    assert rejected.status == 403
    assert state.keys == []


async def test_mutating_request_with_bootstrapped_csrf_token_succeeds(
    _admin_password_env: None,
) -> None:
    """The same POST, carrying the bootstrapped token, creates exactly one key.

    A GET first surfaces the per-session CSRF token in the ``X-CSRF-Token``
    response header (the bootstrap); replaying the ``POST /admin/keys`` with that
    token in the request header passes the CSRF gate, the handler runs, and the
    access-key store gains exactly one row (Property 3 success direction,
    Requirement 11.6).

    Validates: Requirements 11.6
    """
    state = _State()
    app = _admin_app(state)

    bootstrap = await _request(
        app,
        "GET",
        "/admin/keys",
        headers={"Authorization": _basic_auth()},
    )
    assert bootstrap.status == 200
    token = bootstrap.headers.get(CSRF_HEADER.lower())
    assert token, "a successful read must surface the CSRF token to bootstrap from"

    created = await _request(
        app,
        "POST",
        "/admin/keys",
        headers={"Authorization": _basic_auth(), CSRF_HEADER: token},
        body={"label": "ci"},
    )

    assert created.status == 201
    body = created.json()
    assert body["secret_access_key"], "create must return the one-time secret"
    assert len(state.keys) == 1
    assert state.keys[0].label == "ci"


async def test_mismatched_csrf_token_is_rejected_and_changes_nothing(
    _admin_password_env: None,
) -> None:
    """A bootstrapped-but-wrong token is still 403 with no resource change.

    Bootstrapping the real token and then presenting a *different* value on the
    mutating request must be rejected exactly like the missing-token case: HTTP
    403 and no appended row. This closes the "or with a mismatched token" clause
    of Property 3.

    Validates: Requirements 11.6
    """
    state = _State()
    app = _admin_app(state)

    bootstrap = await _request(
        app,
        "GET",
        "/admin/keys",
        headers={"Authorization": _basic_auth()},
    )
    assert bootstrap.status == 200
    assert bootstrap.headers.get(CSRF_HEADER.lower())

    rejected = await _request(
        app,
        "POST",
        "/admin/keys",
        headers={"Authorization": _basic_auth(), CSRF_HEADER: "not-the-real-token"},
        body={"label": "ci"},
    )

    assert rejected.status == 403
    assert state.keys == []


if __name__ == "__main__":  # pragma: no cover - convenience for direct runs
    raise SystemExit(pytest.main([__file__, "-v"]))

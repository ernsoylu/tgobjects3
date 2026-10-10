"""Property test for session-credentials → Recovery Kit (:mod:`gateway.app.admin.setup`).

This module hosts **Property 4** of the admin-dashboard design: *a login that
completes successfully carries its Telegram ``session_credentials`` into the
Recovery Kit, and the kit's master-key verifier is a verifier — never the
submitted master key*.

The design places the Recovery Kit assembly in ``admin/setup.py`` itself
(:func:`gateway.app.admin.setup._build_recovery_kit`): the finalize step copies
the authenticated login's ``session_credentials`` straight into
``recovery_kit.telegram_session`` and derives ``recovery_kit.master_key_verifier``
by wrapping a fixed known plaintext
(:data:`~gateway.app.admin.setup._MASTER_KEY_VERIFIER_PLAINTEXT`) under the root
key derived from the master key — so a recovering operator can confirm they hold
the correct master key (the unwrap succeeds) without the kit ever carrying the
key itself.

This test therefore drives the *real* setup router over its ASGI surface through
a complete wizard run — telegram login → buckets → config (with the master key)
→ finalize — with a fake :class:`~gateway.app.admin.setup.TelegramLoginPort` that
returns the generated ``session_credentials`` on authentication, and the
hand-written fake session factory from the sibling wizard tests (no real
PostgreSQL or Telegram). Hypothesis varies, across ≥ 100 iterations:

* the login shape — direct bot-token-style authentication (``start_login``
  returns ``authenticated=True``) and the code/2FA path (``needs_code`` then a
  correct ``submit_code``), since Property 4 covers both;
* the opaque ``session_credentials`` string the port returns;
* the submitted master key (≥ 16 characters); and
* the bucket→channel assignments.

and asserts the finalize response's ``recovery_kit.telegram_session`` equals the
login's ``session_credentials`` and ``recovery_kit.master_key_verifier`` never
equals the submitted master key.

Following the sibling wizard tests (:mod:`tests.unit.test_setup_wizard`,
:mod:`tests.unit.admin.test_setup_two_factor`), these tests avoid a real database
and the unpinned ``httpx``/``TestClient``: the router is driven directly over
ASGI by a small in-process driver and the SQLAlchemy session factory is a
hand-written fake serving the wizard's access-key ``count`` queries.

Validates: Requirements 2.6, 11.2
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, cast

from fastapi import FastAPI
from gateway.app.admin.setup import (
    MASTER_KEY_MIN_LENGTH,
    TelegramLoginResult,
    create_setup_router,
)
from gateway.app.config import Settings
from gateway.app.db.models import AccessKey
from gateway.app.db.session import AsyncSessionFactory
from gateway.app.telegram.port import SessionState, TelegramMessage, TelegramPort
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from starlette.types import ASGIApp, Message, Scope

_STAGING_SIZE = 64 * 1024 * 1024
_CACHE_SIZE = 32 * 1024 * 1024

# A validated :class:`Settings` needs a master key independent of the one the
# wizard's config step collects (the latter is what Property 4 is about).
_SETTINGS_MASTER_KEY = "unit-test-master-key-0123456789"


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
    """Serves the wizard's ``count`` queries over :class:`_State` and records adds.

    The finalize step issues the read shape
    ``select(func.count()).select_from(AccessKey)`` and then ``add``s the created
    key; both route against the shared state so the single-finalize guard sees a
    truthful count.
    """

    def __init__(self, state: _State) -> None:
        self._state = state

    async def execute(self, statement: Any) -> Any:
        description = str(statement)
        if "count(" in description and "access_keys" in description:
            return _RowResult(len(self._state.keys))
        raise AssertionError(f"unexpected statement: {description}")

    def add(self, obj: Any) -> None:
        if isinstance(obj, AccessKey):
            self._state.keys.append(obj)
        else:  # pragma: no cover - defensive
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
    """A :class:`TelegramPort` whose every channel is empty (no bucket manifest).

    The buckets step probes each channel for an existing manifest; with no
    messages every assignment is recorded directly, which is all Property 4 needs
    (it is about the login credentials and master-key verifier, not adoption).
    """

    async def send_document(
        self, channel_id: int, data: bytes, file_name: str, caption: str
    ) -> int:  # pragma: no cover - not used by this property
        raise AssertionError("send_document is not used by the recovery-kit test")

    async def get_messages(
        self, channel_id: int, ids: list[int]
    ) -> list[TelegramMessage]:
        return []

    async def download_document(
        self, channel_id: int, message_id: int
    ) -> bytes:  # pragma: no cover - no manifest to download
        raise AssertionError("download_document is not reached with empty channels")

    async def delete_messages(
        self, channel_id: int, message_ids: list[int]
    ) -> None:  # pragma: no cover - not used
        raise AssertionError("delete_messages is not used by the recovery-kit test")

    async def create_channel(
        self, title: str
    ) -> int:  # pragma: no cover - not used
        raise AssertionError("create_channel is not used by the recovery-kit test")

    async def delete_channel(
        self, channel_id: int
    ) -> None:  # pragma: no cover - not used
        raise AssertionError("delete_channel is not used by the recovery-kit test")

    async def get_session_state(
        self,
    ) -> SessionState:  # pragma: no cover - not used
        return SessionState.CONNECTED

    async def is_connected(self) -> bool:  # pragma: no cover - not used
        return True


class _FakeLoginPort:
    """A :class:`TelegramLoginPort` that authenticates with scripted credentials.

    When ``direct`` is true, ``start_login`` authenticates outright (the
    bot-token shape); otherwise it asks for a code and ``submit_code``
    authenticates. Either way the authenticated result carries
    ``session_credentials`` so the finalize step can copy them into the Recovery
    Kit (Property 4).
    """

    def __init__(self, *, session_credentials: str, direct: bool) -> None:
        self._session_credentials = session_credentials
        self._direct = direct
        self.reset_calls = 0

    async def start_login(
        self, api_id: int, api_hash: str, login: str
    ) -> TelegramLoginResult:
        if self._direct:
            return TelegramLoginResult(
                authenticated=True,
                needs_code=False,
                session_credentials=self._session_credentials,
            )
        return TelegramLoginResult(authenticated=False, needs_code=True)

    async def submit_code(self, code: str) -> TelegramLoginResult:
        return TelegramLoginResult(
            authenticated=True,
            needs_code=False,
            session_credentials=self._session_credentials,
        )

    async def reset(self) -> None:  # pragma: no cover - not used on the happy path
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
    """Build a validated :class:`Settings` with a fixed startup master key."""
    return Settings(
        TGOS3_MASTER_KEY=_SETTINGS_MASTER_KEY,
        DATABASE_URL="postgresql+asyncpg://localhost/tgos3",
    )


def _setup_app(state: _State, login_port: _FakeLoginPort) -> FastAPI:
    """Mount the real setup router wired to the given fakes."""
    context = _FakeContext(
        settings=_settings(),
        session_factory=cast(AsyncSessionFactory, _FakeSessionFactory(state)),
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


def _telegram_begin() -> dict[str, Any]:
    """A credentials POST body that begins a fresh login."""
    return {"api_id": 12345, "api_hash": "deadbeef", "login": "+15551234567"}


def _telegram_code(code: str) -> dict[str, Any]:
    """A credentials POST body that submits a login/two-factor code."""
    return {**_telegram_begin(), "code": code}


def _config_body(master_key: str) -> dict[str, Any]:
    """A valid config step body carrying the given master key (Requirement 2.2)."""
    return {
        "staging_path": "/data/staging",
        "staging_max_size": _STAGING_SIZE,
        "cache_path": "/data/cache",
        "cache_max_size": _CACHE_SIZE,
        "master_key": master_key,
    }


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
# Hypothesis strategies
# ---------------------------------------------------------------------------

#: Opaque Telegram session blobs: the port returns these verbatim and the kit
#: must carry them unchanged. Non-empty (a successful login's credentials are
#: non-empty per Property 4) and drawn from printable text to stay JSON-safe.
_session_credentials = st.text(
    alphabet=st.characters(min_codepoint=0x20, max_codepoint=0x7E),
    min_size=1,
    max_size=120,
)

#: Submitted master keys of at least the minimum length (Requirement 2.2). Drawn
#: from printable text and bounded so the config step accepts them.
_master_keys = st.text(
    alphabet=st.characters(min_codepoint=0x20, max_codepoint=0x7E),
    min_size=MASTER_KEY_MIN_LENGTH,
    max_size=64,
)

#: Bucket→channel assignments: distinct bucket names mapped to channel ids.
_assignments = st.lists(
    st.tuples(
        st.text(
            alphabet=st.characters(min_codepoint=0x61, max_codepoint=0x7A),
            min_size=1,
            max_size=16,
        ),
        st.integers(min_value=-(10**13), max_value=-1),
    ),
    min_size=1,
    max_size=4,
    unique_by=lambda pair: pair[0],
)


# ---------------------------------------------------------------------------
# Property 4: session credentials carried into the Recovery Kit
# ---------------------------------------------------------------------------


@settings(
    max_examples=150,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    session_credentials=_session_credentials,
    master_key=_master_keys,
    direct=st.booleans(),
    assignments=_assignments,
)
async def test_recovery_kit_carries_session_credentials_and_verifier(
    session_credentials: str,
    master_key: str,
    direct: bool,
    assignments: list[tuple[str, int]],
) -> None:
    """Property 4: a successful login's credentials reach the kit; verifier != key.

    For any successful login — direct bot-token-style authentication or the
    code/2FA path — with any opaque ``session_credentials``, any submitted master
    key of at least the minimum length, and any bucket assignments, a complete
    wizard run's finalize response carries ``recovery_kit.telegram_session`` equal
    to the login's ``session_credentials`` and a ``recovery_kit.master_key_verifier``
    that is never equal to the submitted master key.

    Validates: Requirements 2.6, 11.2
    """
    state = _State()
    login = _FakeLoginPort(session_credentials=session_credentials, direct=direct)
    app = _setup_app(state, login)

    # Telegram login: direct authentication, or needs_code → correct code.
    begin = await _request(app, "POST", "/setup/telegram", body=_telegram_begin())
    assert begin.status == 200
    if direct:
        assert begin.json()["authenticated"] is True
    else:
        assert begin.json()["needs_code"] is True
        coded = await _request(
            app, "POST", "/setup/telegram", body=_telegram_code("any-code")
        )
        assert coded.status == 200
        assert coded.json()["authenticated"] is True

    # Buckets: every channel is empty, so each assignment is recorded directly.
    buckets_resp = await _request(
        app,
        "POST",
        "/setup/buckets",
        body={
            "assignments": [
                {"bucket_name": name, "channel_id": channel}
                for name, channel in assignments
            ]
        },
    )
    assert buckets_resp.status == 200
    assert buckets_resp.json()["requires_choice"] is False

    # Config: carries the submitted master key.
    config_resp = await _request(
        app, "POST", "/setup/config", body=_config_body(master_key)
    )
    assert config_resp.status == 200
    assert config_resp.json()["accepted"] is True

    # Finalize: the Recovery Kit must carry the session credentials and a verifier.
    final = await _request(app, "POST", "/setup/finalize")
    assert final.status == 201
    kit = final.json()["recovery_kit"]

    # The login's session credentials are carried through verbatim.
    assert kit["telegram_session"] == session_credentials
    # The verifier is a verifier, never the submitted master key itself.
    assert kit["master_key_verifier"] != master_key
    assert kit["master_key_verifier"]
    # And the bucket assignments round-trip into the kit.
    assert kit["buckets"] == [
        {"bucket_name": name, "channel_id": channel}
        for name, channel in assignments
    ]

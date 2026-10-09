"""Unit tests for the guided setup wizard API (:mod:`gateway.app.admin.setup`, task 42.2).

Drive the ``POST /admin/setup/*`` wizard end-to-end through its ASGI surface,
exercising the Requirement 2 edge cases task 42.2 calls out:

* **Two-factor retry exhaustion (Requirements 2.3, 2.4).** Each wrong login code
  consumes one of :data:`~gateway.app.admin.setup.MAX_TWO_FACTOR_ATTEMPTS`
  attempts; the first two failures leave the step awaiting another code with the
  remaining-attempt count decremented, while the third failure aborts the login
  step with HTTP 401, resets the interactive login, and returns the operator to
  credential entry (a fresh credentials POST then starts a clean attempt budget).
* **Adopt vs reinitialise (Requirement 2.7).** A channel that already carries a
  decodable ``tgo3-k-`` bucket manifest requires an adopt/reinitialise choice:
  with no choice the entry is flagged ``requires_choice`` and nothing is
  recorded; ``adopt`` records the existing bucket silently; ``reinitialise``
  records it flagged and surfaces the reinitialise warning. An empty channel is
  assigned directly with no manifest flag.
* **Secret shown exactly once (Requirement 2.6).** The finalize step returns the
  first access key's plaintext secret once; the stored row holds only the
  AES-Key-Wrapped secret (never the plaintext) and no later read re-exposes it.
* **Field-bound validation (Requirement 2.2).** The config step rejects
  staging/cache sizes outside their ranges and a master key shorter than the
  minimum with HTTP 422 before anything is stored.

Like :mod:`tests.unit.test_admin_router` and :mod:`tests.unit.test_observability`,
these tests avoid a real database and the unpinned ``httpx``/``TestClient``: the
setup router is driven directly over ASGI by a small in-process driver, the
SQLAlchemy session factory is a hand-written fake serving the wizard's two
access-key ``count`` queries over in-memory rows, and the Telegram boundary is
driven by fakes satisfying :class:`~gateway.app.telegram.port.TelegramPort` and
:class:`~gateway.app.admin.setup.TelegramLoginPort`.
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, cast

from fastapi import FastAPI
from gateway.app.admin.setup import (
    MASTER_KEY_MIN_LENGTH,
    MAX_TWO_FACTOR_ATTEMPTS,
    TelegramLoginResult,
    TwoFactorFailedError,
    create_setup_router,
)
from gateway.app.config import Settings
from gateway.app.db.models import AccessKey
from gateway.app.db.session import AsyncSessionFactory
from gateway.app.format import bucket_manifest
from gateway.app.format.key_wrap import derive_root_key, unwrap
from gateway.app.format.types import BucketManifest
from gateway.app.telegram.port import SessionState, TelegramMessage, TelegramPort
from starlette.types import ASGIApp, Message, Scope

_MASTER_KEY = "unit-test-master-key-0123456789"
_STAGING_SIZE = 64 * 1024 * 1024
_CACHE_SIZE = 32 * 1024 * 1024


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

    The status and finalize steps issue the single read shape
    ``select(func.count()).select_from(AccessKey)``; the finalize step also
    ``add``s the created key. Both are routed against the shared state so a
    finalize-then-status within a test observes the new key.
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


class _FakeTelegramPort:
    """A :class:`TelegramPort` whose channels replay a fixed message list.

    Only ``get_messages`` and ``download_document`` are exercised by the buckets
    step's manifest probe; the remaining methods are present to satisfy the
    protocol and assert if an unexpected path reaches them.
    """

    def __init__(
        self,
        messages: dict[int, list[TelegramMessage]],
        documents: dict[tuple[int, int], bytes],
    ) -> None:
        self._messages = messages
        self._documents = documents

    async def send_document(
        self, channel_id: int, data: bytes, file_name: str, caption: str
    ) -> int:  # pragma: no cover - not used by the wizard
        raise AssertionError("send_document is not used by the setup wizard")

    async def get_messages(
        self, channel_id: int, ids: list[int]
    ) -> list[TelegramMessage]:
        present = self._messages.get(channel_id, [])
        wanted = set(ids)
        return [m for m in present if m.message_id in wanted]

    async def download_document(self, channel_id: int, message_id: int) -> bytes:
        return self._documents[(channel_id, message_id)]

    async def delete_messages(
        self, channel_id: int, message_ids: list[int]
    ) -> None:  # pragma: no cover - not used by the wizard
        raise AssertionError("delete_messages is not used by the setup wizard")

    async def create_channel(
        self, title: str
    ) -> int:  # pragma: no cover - not used by the wizard
        raise AssertionError("create_channel is not used by the setup wizard")

    async def delete_channel(
        self, channel_id: int
    ) -> None:  # pragma: no cover - not used by the wizard
        raise AssertionError("delete_channel is not used by the setup wizard")

    async def get_session_state(
        self,
    ) -> SessionState:  # pragma: no cover - not used by the wizard
        return SessionState.CONNECTED

    async def is_connected(self) -> bool:  # pragma: no cover - not used
        return True


class _FakeLoginPort:
    """A :class:`TelegramLoginPort` scripting the 2FA retry/abort path (2.3/2.4).

    ``start_login`` always asks for a code. ``submit_code`` authenticates only
    when the submitted code equals ``correct_code``; every other code raises
    :class:`TwoFactorFailedError` so the wizard counts the failed attempt.
    ``reset`` records that the aborted login was discarded (Requirement 2.4).
    """

    def __init__(self, correct_code: str = "right-code") -> None:
        self._correct_code = correct_code
        self.reset_calls = 0
        self.submit_calls = 0

    async def start_login(
        self, api_id: int, api_hash: str, login: str
    ) -> TelegramLoginResult:
        return TelegramLoginResult(authenticated=False, needs_code=True)

    async def submit_code(self, code: str) -> TelegramLoginResult:
        self.submit_calls += 1
        if code == self._correct_code:
            return TelegramLoginResult(
                authenticated=True,
                needs_code=False,
                session_credentials="session-blob",
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


def _setup_app(
    state: _State,
    telegram: TelegramPort,
    *,
    login_port: _FakeLoginPort | None = None,
) -> FastAPI:
    """Mount the setup router on a FastAPI app with the given fakes."""
    context = _FakeContext(
        settings=_settings(),
        session_factory=cast(AsyncSessionFactory, _FakeSessionFactory(state)),
        telegram=telegram,
    )
    login_factory = (
        (lambda _settings: login_port) if login_port is not None else None
    )
    app = FastAPI()
    app.include_router(
        create_setup_router(
            context=cast(Any, context), login_factory=login_factory
        )
    )
    return app


def _empty_telegram() -> _FakeTelegramPort:
    """A Telegram port with no channels (every probed channel is empty)."""
    return _FakeTelegramPort(messages={}, documents={})


def _manifest_telegram(channel_id: int) -> _FakeTelegramPort:
    """A Telegram port whose ``channel_id`` carries a decodable bucket manifest."""
    manifest = BucketManifest(
        bucket_name="adopted", kek_wrapped=b"\x00" * 40, kek_version=1
    )
    payload = bucket_manifest.serialise(manifest)
    message = TelegramMessage(
        message_id=1,
        caption="tgo3-k-1",
        document_id=555,
        file_reference=b"ref",
        dc_id=2,
    )
    return _FakeTelegramPort(
        messages={channel_id: [message]},
        documents={(channel_id, 1): payload},
    )


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


def _telegram_begin() -> dict[str, Any]:
    """A credentials POST body that begins a fresh login."""
    return {"api_id": 12345, "api_hash": "deadbeef", "login": "+15551234567"}


def _telegram_code(code: str) -> dict[str, Any]:
    """A credentials POST body that submits a two-factor code."""
    return {**_telegram_begin(), "code": code}


def _config_body(**overrides: Any) -> dict[str, Any]:
    """A valid config step body, with field overrides for bound tests."""
    body = {
        "staging_path": "/data/staging",
        "staging_max_size": _STAGING_SIZE,
        "cache_path": "/data/cache",
        "cache_max_size": _CACHE_SIZE,
        "master_key": _MASTER_KEY,
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# Two-factor retry exhaustion (Requirements 2.3, 2.4)
# ---------------------------------------------------------------------------


async def test_two_factor_begin_asks_for_code() -> None:
    """Submitting credentials starts login and asks for a code (Requirement 2.3)."""
    login = _FakeLoginPort()
    app = _setup_app(_State(), _empty_telegram(), login_port=login)

    response = await _request(
        app, "POST", "/setup/telegram", body=_telegram_begin()
    )
    assert response.status == 200
    body = response.json()
    assert body["needs_code"] is True
    assert body["authenticated"] is False
    assert body["attempts_remaining"] == MAX_TWO_FACTOR_ATTEMPTS


async def test_two_factor_wrong_codes_decrement_until_abort() -> None:
    """Three wrong codes abort with 401 and reset to credential entry (2.3/2.4)."""
    login = _FakeLoginPort(correct_code="right-code")
    app = _setup_app(_State(), _empty_telegram(), login_port=login)

    await _request(app, "POST", "/setup/telegram", body=_telegram_begin())

    # First wrong code: one attempt consumed, still awaiting a code.
    first = await _request(
        app, "POST", "/setup/telegram", body=_telegram_code("wrong-1")
    )
    assert first.status == 200
    assert first.json()["needs_code"] is True
    assert first.json()["attempts_remaining"] == MAX_TWO_FACTOR_ATTEMPTS - 1

    # Second wrong code: a second attempt consumed, one remaining.
    second = await _request(
        app, "POST", "/setup/telegram", body=_telegram_code("wrong-2")
    )
    assert second.status == 200
    assert second.json()["attempts_remaining"] == MAX_TWO_FACTOR_ATTEMPTS - 2

    # Third wrong code exceeds the limit: abort with 401 and discard the login.
    third = await _request(
        app, "POST", "/setup/telegram", body=_telegram_code("wrong-3")
    )
    assert third.status == 401
    assert login.submit_calls == 3
    assert login.reset_calls == 1


async def test_two_factor_submitting_code_after_abort_requires_restart() -> None:
    """After an abort, a bare code POST is a 409 until credentials are re-sent (2.4)."""
    login = _FakeLoginPort(correct_code="right-code")
    app = _setup_app(_State(), _empty_telegram(), login_port=login)

    await _request(app, "POST", "/setup/telegram", body=_telegram_begin())
    for attempt in range(MAX_TWO_FACTOR_ATTEMPTS):
        await _request(
            app, "POST", "/setup/telegram", body=_telegram_code(f"wrong-{attempt}")
        )

    # The login step was reset: a follow-up code with no in-progress login is 409.
    stray = await _request(
        app, "POST", "/setup/telegram", body=_telegram_code("right-code")
    )
    assert stray.status == 409


async def test_two_factor_restart_resets_the_attempt_budget() -> None:
    """Re-entering credentials after an abort restores a full attempt budget (2.4)."""
    login = _FakeLoginPort(correct_code="right-code")
    app = _setup_app(_State(), _empty_telegram(), login_port=login)

    await _request(app, "POST", "/setup/telegram", body=_telegram_begin())
    for attempt in range(MAX_TWO_FACTOR_ATTEMPTS):
        await _request(
            app, "POST", "/setup/telegram", body=_telegram_code(f"wrong-{attempt}")
        )

    # Return to credential entry: the budget is full again.
    restart = await _request(
        app, "POST", "/setup/telegram", body=_telegram_begin()
    )
    assert restart.status == 200
    assert restart.json()["attempts_remaining"] == MAX_TWO_FACTOR_ATTEMPTS

    # And a correct code now authenticates on the fresh attempt.
    ok = await _request(
        app, "POST", "/setup/telegram", body=_telegram_code("right-code")
    )
    assert ok.status == 200
    assert ok.json()["authenticated"] is True


# ---------------------------------------------------------------------------
# Adopt vs reinitialise for a channel with existing manifests (Requirement 2.7)
# ---------------------------------------------------------------------------


async def test_buckets_empty_channel_assigned_directly() -> None:
    """A channel with no manifest is assigned with no choice required (2.7)."""
    app = _setup_app(_State(), _empty_telegram())

    response = await _request(
        app,
        "POST",
        "/setup/buckets",
        body={"assignments": [{"bucket_name": "photos", "channel_id": -100100}]},
    )
    assert response.status == 200
    body = response.json()
    assert body["requires_choice"] is False
    [entry] = body["buckets"]
    assert entry["existing_manifest"] is False
    assert entry["requires_choice"] is False
    assert entry["warning"] is None


async def test_buckets_existing_manifest_without_choice_requires_choice() -> None:
    """An existing manifest with no choice flags requires_choice (Requirement 2.7)."""
    channel = -100200
    app = _setup_app(_State(), _manifest_telegram(channel))

    response = await _request(
        app,
        "POST",
        "/setup/buckets",
        body={"assignments": [{"bucket_name": "docs", "channel_id": channel}]},
    )
    assert response.status == 200
    body = response.json()
    assert body["requires_choice"] is True
    [entry] = body["buckets"]
    assert entry["existing_manifest"] is True
    assert entry["requires_choice"] is True
    assert entry["warning"]  # the operator sees the reinitialise warning


async def test_buckets_adopt_existing_manifest_records_silently() -> None:
    """Adopting an existing manifest records the bucket with no warning (2.7)."""
    channel = -100300
    app = _setup_app(_State(), _manifest_telegram(channel))

    response = await _request(
        app,
        "POST",
        "/setup/buckets",
        body={
            "assignments": [
                {
                    "bucket_name": "archive",
                    "channel_id": channel,
                    "on_existing": "adopt",
                }
            ]
        },
    )
    assert response.status == 200
    body = response.json()
    assert body["requires_choice"] is False
    [entry] = body["buckets"]
    assert entry["existing_manifest"] is True
    assert entry["requires_choice"] is False
    assert entry["warning"] is None


async def test_buckets_reinitialise_existing_manifest_warns() -> None:
    """Reinitialising an existing manifest records it and warns (Requirement 2.7)."""
    channel = -100400
    app = _setup_app(_State(), _manifest_telegram(channel))

    response = await _request(
        app,
        "POST",
        "/setup/buckets",
        body={
            "assignments": [
                {
                    "bucket_name": "scratch",
                    "channel_id": channel,
                    "on_existing": "reinitialise",
                }
            ]
        },
    )
    assert response.status == 200
    body = response.json()
    assert body["requires_choice"] is False
    [entry] = body["buckets"]
    assert entry["existing_manifest"] is True
    assert entry["requires_choice"] is False
    assert "inaccessible" in entry["warning"]


# ---------------------------------------------------------------------------
# Secret shown exactly once + Recovery Kit (Requirements 2.5, 2.6)
# ---------------------------------------------------------------------------


async def _complete_through_config(
    app: ASGIApp, telegram_channel: int
) -> None:
    """Drive the wizard through telegram → buckets → config, leaving finalize ready."""
    await _request(app, "POST", "/setup/telegram", body=_telegram_begin())
    await _request(
        app, "POST", "/setup/telegram", body=_telegram_code("right-code")
    )
    await _request(
        app,
        "POST",
        "/setup/buckets",
        body={
            "assignments": [
                {"bucket_name": "photos", "channel_id": telegram_channel}
            ]
        },
    )
    await _request(app, "POST", "/setup/config", body=_config_body())


async def test_finalize_returns_secret_once_and_stores_wrapped() -> None:
    """Finalize returns the plaintext secret once; storage holds only the wrap (2.6)."""
    state = _State()
    channel = -100500
    login = _FakeLoginPort(correct_code="right-code")
    app = _setup_app(state, _empty_telegram(), login_port=login)
    await _complete_through_config(app, channel)

    response = await _request(app, "POST", "/setup/finalize")
    assert response.status == 201
    result = response.json()
    secret = result["secret_access_key"]
    assert secret

    # Exactly one key was stored and it never holds the plaintext secret.
    [stored] = state.keys
    assert secret not in stored.secret_enc
    root_key = derive_root_key(_MASTER_KEY)
    unwrapped = unwrap(root_key, base64.b64decode(stored.secret_enc))
    assert unwrapped.decode("utf-8") == secret

    # The secret appears nowhere outside this single finalize response: once the
    # key exists, status reports setup is no longer required and never re-emits it.
    status_resp = await _request(app, "POST", "/setup/status")
    assert status_resp.json()["setup_required"] is False
    assert "secret_access_key" not in status_resp.body.decode("utf-8")


async def test_finalize_recovery_kit_carries_verifier_not_master_key() -> None:
    """The Recovery Kit carries a master-key verifier, never the key itself (2.5)."""
    state = _State()
    channel = -100600
    login = _FakeLoginPort(correct_code="right-code")
    app = _setup_app(state, _empty_telegram(), login_port=login)
    await _complete_through_config(app, channel)

    response = await _request(app, "POST", "/setup/finalize")
    assert response.status == 201
    kit = response.json()["recovery_kit"]

    # The master key never appears in the kit; the verifier is present instead.
    assert _MASTER_KEY not in json.dumps(kit)
    assert kit["master_key_verifier"]
    assert kit["telegram_session"] == "session-blob"
    assert kit["buckets"] == [{"bucket_name": "photos", "channel_id": channel}]


async def test_finalize_is_409_when_a_key_already_exists() -> None:
    """Finalize refuses once an access key exists — the secret is a one-time event (2.6)."""
    state = _State()
    state.keys.append(
        AccessKey(
            access_key_id="AKIAEXISTING00000001",
            secret_enc="d2hhdGV2ZXI=",
            label=None,
            grants=None,
            disabled=False,
        )
    )
    channel = -100700
    login = _FakeLoginPort(correct_code="right-code")
    app = _setup_app(state, _empty_telegram(), login_port=login)
    await _complete_through_config(app, channel)

    response = await _request(app, "POST", "/setup/finalize")
    assert response.status == 409


# ---------------------------------------------------------------------------
# Config field-bound validation (Requirement 2.2)
# ---------------------------------------------------------------------------


async def test_config_rejects_staging_size_below_minimum() -> None:
    """A staging size below 1 MiB is rejected with 422 (Requirement 2.2)."""
    app = _setup_app(_State(), _empty_telegram())
    response = await _request(
        app, "POST", "/setup/config", body=_config_body(staging_max_size=1024)
    )
    assert response.status == 422


async def test_config_rejects_cache_size_above_maximum() -> None:
    """A cache size above 100 GiB is rejected with 422 (Requirement 2.2)."""
    app = _setup_app(_State(), _empty_telegram())
    response = await _request(
        app,
        "POST",
        "/setup/config",
        body=_config_body(cache_max_size=200 * 1024**3),
    )
    assert response.status == 422


async def test_config_rejects_short_master_key() -> None:
    """A master key shorter than the minimum length is rejected with 422 (2.2)."""
    app = _setup_app(_State(), _empty_telegram())
    short_key = "x" * (MASTER_KEY_MIN_LENGTH - 1)
    response = await _request(
        app, "POST", "/setup/config", body=_config_body(master_key=short_key)
    )
    assert response.status == 422


async def test_config_accepts_values_at_the_bounds() -> None:
    """A master key at the minimum length and in-range sizes are accepted (2.2)."""
    app = _setup_app(_State(), _empty_telegram())
    response = await _request(
        app,
        "POST",
        "/setup/config",
        body=_config_body(master_key="y" * MASTER_KEY_MIN_LENGTH),
    )
    assert response.status == 200
    assert response.json()["accepted"] is True

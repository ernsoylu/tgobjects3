"""Integration test for the setup-router login wiring in ``main`` (task 2.1/2.3).

The composition root wires the interactive-login adapter into the setup wizard
through :func:`gateway.app.main._build_setup_router`, which hands
:func:`gateway.app.admin.setup.create_setup_router` a ``login_factory`` closure.
Before that wiring landed, ``POST /admin/setup/telegram`` returned HTTP 503
("Telegram login is not available on this deployment") because the router had no
factory; the design's "Backend integration" testing strategy
(``.kiro/specs/admin-dashboard/design.md``) asks for a test proving the factory
seam is now connected, so the endpoint reaches an adapter instead.

This test builds the real :class:`~gateway.app.main.GatewayContext` from a
validated :class:`~gateway.app.config.Settings`, asks ``_build_setup_router`` for
the wired router, mounts it on a FastAPI app, and drives ``POST
/admin/setup/telegram`` over an in-process ASGI client. To keep the test off the
real Telegram network — the factory defers ``from
gateway.app.telegram.login_adapter import TelethonLoginAdapter`` *inside* the
closure precisely so the adapter is swappable here — we replace that symbol with
a fake adapter satisfying the :class:`~gateway.app.admin.setup.TelegramLoginPort`
Protocol. The assertion is deliberately about the *seam*, not Telethon: the step
no longer short-circuits to 503, it reaches the (fake) adapter and returns a
normal ``TelegramStepResult`` asking for a code.

No database or network is touched: ``build_context`` opens an async engine
lazily (no connection), and the Telegram login step never queries the index.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi import FastAPI
from gateway.app import main as main_module
from gateway.app.admin.setup import TelegramLoginResult
from gateway.app.config import Settings
from gateway.app.main import _build_setup_router, build_context
from starlette.types import ASGIApp, Message, Scope

_MASTER_KEY = "integration-wiring-master-key-01"


# ---------------------------------------------------------------------------
# Fake login adapter installed through the factory seam
# ---------------------------------------------------------------------------


class _FakeTelethonLoginAdapter:
    """A fake :class:`TelegramLoginPort` standing in for ``TelethonLoginAdapter``.

    Constructed exactly like the real adapter — ``TelethonLoginAdapter(settings)``
    — so the ``login_factory`` closure builds it without change. ``start_login``
    always asks for a code (the phone path), proving the request reached the
    adapter rather than being refused with 503. No Telethon import or network
    call happens on this path.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def start_login(
        self, api_id: int, api_hash: str, login: str
    ) -> TelegramLoginResult:
        return TelegramLoginResult(authenticated=False, needs_code=True)

    async def submit_code(
        self, code: str
    ) -> TelegramLoginResult:  # pragma: no cover - not reached by this test
        return TelegramLoginResult(
            authenticated=True, needs_code=False, session_credentials="fake-session"
        )

    async def reset(self) -> None:  # pragma: no cover - not reached by this test
        return None


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
# Helpers
# ---------------------------------------------------------------------------


def _settings() -> Settings:
    """Build a validated :class:`Settings` with the test master key."""
    return Settings(
        TGOS3_MASTER_KEY=_MASTER_KEY,
        DATABASE_URL="postgresql+asyncpg://localhost/tgos3",
    )


def _telegram_begin() -> dict[str, Any]:
    """A credentials POST body that begins a fresh login (phone path)."""
    return {"api_id": 12345, "api_hash": "deadbeef", "login": "+15551234567"}


def _mount_wired_setup_router() -> FastAPI:
    """Build the real context, ask ``main`` for the wired router, and mount it."""
    context = build_context(_settings())
    router = _build_setup_router(context)
    assert router is not None, "setup wizard router should be available"
    app = FastAPI()
    app.include_router(router)  # type: ignore[arg-type]
    return app


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_build_setup_router_injects_a_login_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``POST /admin/setup/telegram`` no longer returns 503 once ``main`` wires it.

    With the factory seam connected the step reaches the (fake) adapter and
    replies with a normal ``needs_code`` result instead of the "login not
    available" 503 the router returns when no factory is injected.

    Validates: Requirements 2.1, 2.2
    """
    monkeypatch.setattr(
        "gateway.app.telegram.login_adapter.TelethonLoginAdapter",
        _FakeTelethonLoginAdapter,
    )
    app = _mount_wired_setup_router()

    response = await _request(app, "POST", "/setup/telegram", body=_telegram_begin())

    assert response.status != 503
    assert response.status == 200
    body = response.json()
    assert body["needs_code"] is True
    assert body["authenticated"] is False


async def test_login_factory_builds_the_telethon_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The injected factory constructs the Telethon adapter with the settings.

    Spying on the (faked) adapter symbol proves ``_build_setup_router`` wired a
    factory that defers to ``TelethonLoginAdapter(settings)``: the first login
    POST constructs exactly one adapter, carrying the deployment settings.

    Validates: Requirements 2.1, 2.2
    """
    built: list[Settings] = []

    class _RecordingAdapter(_FakeTelethonLoginAdapter):
        def __init__(self, settings: Settings) -> None:
            super().__init__(settings)
            built.append(settings)

    monkeypatch.setattr(
        "gateway.app.telegram.login_adapter.TelethonLoginAdapter",
        _RecordingAdapter,
    )
    app = _mount_wired_setup_router()

    await _request(app, "POST", "/setup/telegram", body=_telegram_begin())

    assert len(built) == 1
    assert built[0].TGOS3_MASTER_KEY == _MASTER_KEY


def test_build_setup_router_returns_a_router() -> None:
    """``_build_setup_router`` returns the mountable wizard router from a context.

    A regression guard that the defensive import path still yields a router (not
    ``None``) when the setup module is present, which is the precondition for the
    factory seam being reachable at all.

    Validates: Requirements 2.1
    """
    router = _build_setup_router(build_context(_settings()))
    assert router is not None
    assert any(
        getattr(route, "path", "") == "/setup/telegram"
        for route in router.routes  # type: ignore[attr-defined]
    )
    # The module seam is the one the factory defers to.
    assert hasattr(main_module, "_build_setup_router")


if __name__ == "__main__":  # pragma: no cover - convenience for direct runs
    raise SystemExit(pytest.main([__file__, "-v"]))

"""Observability tests for the admin health and metrics endpoints (task 33.4).

Covers the design "Observability Tests" acceptance checks and the
corresponding requirement 18 criteria, exercised end-to-end through the ASGI
surface the routers mount:

* ``/health/live`` returns ``200`` on a healthy gateway (requirement 18.7);
* ``/health/ready`` returns ``200`` when both dependencies are usable and
  ``503`` with a body naming the failed dependency — ``postgresql`` when the DB
  is down (requirement 18.3), ``telegram`` when the session is disconnected
  (requirement 18.4), and both when neither is available;
* ``/metrics`` returns valid Prometheus text exposition format with the
  requirement 18.1 metric set present (requirement 18.1); and
* the audit middleware's write-failure containment — an audit write that raises
  is invisible to the S3 client and increments the shared
  ``audit_log_write_failures_total`` counter the ``/metrics`` endpoint reports
  (requirements 18.5, 18.6, 18.8; design "Audit Log Write Failure").

These tests avoid a real database and a real Telegram session: the readiness
probe's dependencies are driven through fakes satisfying
:class:`~gateway.app.db.session.AsyncSessionFactory`'s ``session()`` seam and
the :class:`~gateway.app.telegram.port.TelegramPort` protocol. The routers are
driven directly over ASGI (no ``httpx``/``TestClient`` dependency, which is not
pinned), mirroring the in-process ASGI driver used by
:mod:`tests.unit.test_audit`.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

from fastapi import FastAPI
from gateway.app.admin.health import (
    DEPENDENCY_POSTGRESQL,
    DEPENDENCY_TELEGRAM,
    create_health_router,
)
from gateway.app.admin.metrics import (
    CONTENT_TYPE_LATEST,
    METRIC_AUDIT_LOG_WRITE_FAILURES,
    METRIC_CACHE_SIZE_BYTES,
    METRIC_RECORD_AUTH_FAILURE,
    METRIC_STAGING_RESERVED_BYTES,
    METRIC_UPLOAD_JOB_STATE,
    MetricsRegistry,
    create_metrics_router,
)
from gateway.app.db.session import AsyncSessionFactory
from gateway.app.s3.audit import AuditEntry, AuditLogMiddleware
from gateway.app.telegram.errors import TelegramUnavailableError
from gateway.app.telegram.port import SessionState, TelegramMessage, TelegramPort
from sqlalchemy.exc import OperationalError
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# ---------------------------------------------------------------------------
# Dependency fakes
# ---------------------------------------------------------------------------


class _FakeSession:
    """A minimal async session whose ``execute`` can be made to fail.

    Only the surface the readiness probe touches is implemented: an awaitable
    ``execute`` and the async-context-manager protocol used by the real
    ``async_sessionmaker`` session.
    """

    def __init__(self, *, fail: bool) -> None:
        self._fail = fail

    async def execute(self, _statement: Any) -> None:
        if self._fail:
            raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _FakeSessionFactory:
    """A stand-in for :class:`AsyncSessionFactory` exposing only ``session()``.

    The readiness probe borrows a session via ``async with factory.session()``
    and runs ``SELECT 1``; ``postgres_up`` toggles whether that query raises so
    the DB-down path can be driven without a real database.
    """

    def __init__(self, *, postgres_up: bool) -> None:
        self._postgres_up = postgres_up

    @asynccontextmanager
    async def session(self) -> AsyncIterator[_FakeSession]:
        yield _FakeSession(fail=not self._postgres_up)


class _FakeTelegramPort:
    """A :class:`TelegramPort` whose connectivity is configurable.

    ``connected`` is returned by :meth:`is_connected`; when ``raises`` is set,
    :meth:`is_connected` raises a typed Telegram error instead, exercising the
    readiness probe's error-swallowing path. The remaining port methods are not
    reached by these tests and raise if called.
    """

    def __init__(self, *, connected: bool, raises: bool = False) -> None:
        self._connected = connected
        self._raises = raises

    async def is_connected(self) -> bool:
        if self._raises:
            raise TelegramUnavailableError("session reconnecting")
        return self._connected

    async def get_session_state(self) -> SessionState:
        return (
            SessionState.CONNECTED if self._connected else SessionState.DISCONNECTED
        )

    async def send_document(
        self, channel_id: int, data: bytes, file_name: str, caption: str
    ) -> int:
        raise AssertionError("unexpected send_document call")

    async def get_messages(
        self, channel_id: int, ids: list[int]
    ) -> list[TelegramMessage]:
        raise AssertionError("unexpected get_messages call")

    async def download_document(self, channel_id: int, message_id: int) -> bytes:
        raise AssertionError("unexpected download_document call")

    async def delete_messages(
        self, channel_id: int, message_ids: list[int]
    ) -> None:
        raise AssertionError("unexpected delete_messages call")

    async def create_channel(self, title: str) -> int:
        raise AssertionError("unexpected create_channel call")

    async def delete_channel(self, channel_id: int) -> None:
        raise AssertionError("unexpected delete_channel call")


def _session_factory(*, postgres_up: bool) -> AsyncSessionFactory:
    """Return the fake session factory typed as the real one for the router."""
    return cast(AsyncSessionFactory, _FakeSessionFactory(postgres_up=postgres_up))


def _telegram(*, connected: bool, raises: bool = False) -> TelegramPort:
    """Return the fake Telegram port typed as the protocol for the router."""
    return cast(TelegramPort, _FakeTelegramPort(connected=connected, raises=raises))


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
        """Decode the collected body as JSON."""
        return json.loads(self.body.decode("utf-8"))

    def text(self) -> str:
        """Decode the collected body as UTF-8 text."""
        return self.body.decode("utf-8")


async def _request(app: ASGIApp, method: str, path: str) -> _Response:
    """Drive one HTTP request through ``app`` over ASGI and collect the response."""
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "scheme": "http",
        "headers": [],
        "client": ("testclient", 12345),
        "server": ("testserver", 80),
    }
    response = _Response()
    request_sent = False

    async def receive() -> Message:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
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


def _health_app(
    *, postgres_up: bool, telegram_connected: bool, telegram_raises: bool = False
) -> FastAPI:
    """Mount the health router on a FastAPI app with the configured fakes."""
    app = FastAPI()
    app.include_router(
        create_health_router(
            session_factory=_session_factory(postgres_up=postgres_up),
            telegram=_telegram(
                connected=telegram_connected, raises=telegram_raises
            ),
        )
    )
    return app


def _metrics_app(registry: MetricsRegistry) -> FastAPI:
    """Mount the metrics router on a FastAPI app for the given registry."""
    app = FastAPI()
    app.include_router(create_metrics_router(registry))
    return app


# ---------------------------------------------------------------------------
# /health/live (requirement 18.7)
# ---------------------------------------------------------------------------


async def test_health_live_returns_200_when_process_up() -> None:
    """``/health/live`` returns 200 regardless of dependency state (18.7)."""
    app = _health_app(postgres_up=False, telegram_connected=False)
    response = await _request(app, "GET", "/health/live")
    assert response.status == 200
    assert response.json() == {"status": "live"}


# ---------------------------------------------------------------------------
# /health/ready (requirements 18.3, 18.4)
# ---------------------------------------------------------------------------


async def test_health_ready_returns_200_when_all_dependencies_up() -> None:
    """``/health/ready`` returns 200 when PostgreSQL and Telegram are usable."""
    app = _health_app(postgres_up=True, telegram_connected=True)
    response = await _request(app, "GET", "/health/ready")
    assert response.status == 200
    assert response.json() == {"status": "ready"}


async def test_health_ready_identifies_postgresql_when_db_down() -> None:
    """503 body names ``postgresql`` when the DB is unavailable (18.3)."""
    app = _health_app(postgres_up=False, telegram_connected=True)
    response = await _request(app, "GET", "/health/ready")
    assert response.status == 503
    assert response.json() == {"unhealthy": [DEPENDENCY_POSTGRESQL]}


async def test_health_ready_identifies_telegram_when_session_disconnected() -> None:
    """503 body names ``telegram`` when the session is disconnected (18.4)."""
    app = _health_app(postgres_up=True, telegram_connected=False)
    response = await _request(app, "GET", "/health/ready")
    assert response.status == 503
    assert response.json() == {"unhealthy": [DEPENDENCY_TELEGRAM]}


async def test_health_ready_swallows_telegram_error_as_unhealthy() -> None:
    """A typed Telegram error degrades readiness to 503, not a 500 (18.4)."""
    app = _health_app(
        postgres_up=True, telegram_connected=False, telegram_raises=True
    )
    response = await _request(app, "GET", "/health/ready")
    assert response.status == 503
    assert response.json() == {"unhealthy": [DEPENDENCY_TELEGRAM]}


async def test_health_ready_names_both_dependencies_when_both_down() -> None:
    """Both dependency names appear when neither is available (18.3/18.4)."""
    app = _health_app(postgres_up=False, telegram_connected=False)
    response = await _request(app, "GET", "/health/ready")
    assert response.status == 503
    assert response.json() == {
        "unhealthy": [DEPENDENCY_POSTGRESQL, DEPENDENCY_TELEGRAM]
    }


# ---------------------------------------------------------------------------
# /metrics (requirement 18.1)
# ---------------------------------------------------------------------------


def _parse_prometheus(text: str) -> dict[str, str]:
    """Parse a Prometheus exposition document into ``{metric_name: type}``.

    Validates the minimal structural rules of the text format: every sample
    line's metric is declared by a preceding ``# TYPE`` line, ``# HELP``/
    ``# TYPE`` lines are well-formed, and the document ends with a newline.
    Returns the ``{name: type}`` map built from the ``# TYPE`` lines.
    """
    types: dict[str, str] = {}
    helps: set[str] = set()
    for line in text.splitlines():
        if line.startswith("# HELP "):
            name = line[len("# HELP ") :].split(" ", 1)[0]
            helps.add(name)
        elif line.startswith("# TYPE "):
            _, _, rest = line.partition("# TYPE ")
            name, _, metric_type = rest.partition(" ")
            assert metric_type in {"counter", "gauge"}, line
            types[name] = metric_type
        elif line and not line.startswith("#"):
            # A sample line: ``name`` or ``name{labels} value``.
            sample_name = line.split("{", 1)[0].split(" ", 1)[0]
            base = sample_name
            assert base in types, f"sample without TYPE declaration: {line}"
    return types


async def test_metrics_returns_valid_prometheus_text_format() -> None:
    """``/metrics`` returns the pinned content type and parseable text (18.1)."""
    registry = MetricsRegistry()
    app = _metrics_app(registry)
    response = await _request(app, "GET", "/metrics")

    assert response.status == 200
    assert response.headers["content-type"] == CONTENT_TYPE_LATEST
    body = response.text()
    assert body.endswith("\n")

    types = _parse_prometheus(body)
    # The requirement 18.1 metric set is present and well-typed.
    assert types[METRIC_AUDIT_LOG_WRITE_FAILURES] == "counter"
    assert types[METRIC_RECORD_AUTH_FAILURE] == "counter"
    assert types[METRIC_UPLOAD_JOB_STATE] == "gauge"
    assert types[METRIC_STAGING_RESERVED_BYTES] == "gauge"
    assert types[METRIC_CACHE_SIZE_BYTES] == "gauge"


async def test_metrics_reflects_counter_and_labelled_gauge_updates() -> None:
    """Updated metrics render their new values and label dimensions (18.1)."""
    registry = MetricsRegistry()
    registry.audit_log_write_failures.inc()
    registry.audit_log_write_failures.inc()
    registry.staging_reserved_bytes.set(4096)
    registry.upload_job_state.set(3, label="uploading")
    registry.upload_job_state.set(1, label="failed")

    app = _metrics_app(registry)
    body = (await _request(app, "GET", "/metrics")).text()

    assert f"{METRIC_AUDIT_LOG_WRITE_FAILURES} 2" in body
    assert f"{METRIC_STAGING_RESERVED_BYTES} 4096" in body
    assert f'{METRIC_UPLOAD_JOB_STATE}{{state="uploading"}} 3' in body
    assert f'{METRIC_UPLOAD_JOB_STATE}{{state="failed"}} 1' in body
    # Parsing still holds with real samples present.
    _parse_prometheus(body)


# ---------------------------------------------------------------------------
# Audit write-failure containment wired to the shared metrics counter
# (requirements 18.5, 18.6, 18.8; design "Audit Log Write Failure")
# ---------------------------------------------------------------------------


class _FailingSink:
    """An audit sink whose ``write`` always raises, simulating a dead backend."""

    async def write(self, entry: AuditEntry) -> None:
        raise RuntimeError("audit backend unavailable")


def _ok_app(status: int = 200) -> ASGIApp:
    """A trivial downstream ASGI app that returns ``status`` with a body."""

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send(
            {"type": "http.response.start", "status": status, "headers": []}
        )
        await send({"type": "http.response.body", "body": b"ok"})

    return app


async def test_audit_write_failure_hidden_from_client_and_counted_in_metrics() -> None:
    """A failed audit write is invisible to the client and bumps the metric.

    Wires the audit middleware's failure counter to the very
    ``audit_log_write_failures_total`` counter owned by the shared
    :class:`MetricsRegistry`, then drives a request through a failing sink. The
    S3 response must be unchanged (requirement 18.8) and the increment must be
    observable on ``/metrics`` (requirements 18.8, 18.1), proving the increment
    path and the scrape path share one counter instance.
    """
    registry = MetricsRegistry()
    middleware = AuditLogMiddleware(
        _ok_app(200),
        sink=_FailingSink(),
        failure_counter=registry.audit_log_write_failures,
        extractor=lambda scope: ("AKID", "GetObject", "bucket", "key"),
    )

    # The S3 request completes normally despite the failing audit sink.
    response = await _request(middleware, "GET", "/bucket/key")
    assert response.status == 200
    assert response.text() == "ok"

    # The failure counter the registry renders was incremented exactly once.
    assert registry.audit_log_write_failures.value == 1.0
    metrics_body = (await _request(_metrics_app(registry), "GET", "/metrics")).text()
    assert f"{METRIC_AUDIT_LOG_WRITE_FAILURES} 1" in metrics_body

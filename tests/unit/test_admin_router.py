"""Unit tests for the admin API router (:mod:`gateway.app.admin.router`, task 41.2).

Drive the admin router end-to-end through its ASGI surface, exercising the
Console-facing endpoints the design's "Admin API" section lists and the two
guarantees task 41.2 calls out explicitly:

* **Settings update (Requirements 19.6/19.7).** A valid runtime-settings change
  is applied and visible on the next read; a staging-size reduction below the
  bytes currently reserved by in-flight uploads is rejected with HTTP 409 and
  leaves the stored value unchanged.
* **CSRF validation (Requirement 17.7).** A state-mutating request (POST / PUT /
  DELETE) with a missing or mismatched ``X-CSRF-Token`` is rejected with HTTP
  403; the same request carrying the session token returned by a prior
  authenticated read succeeds.

Representative read endpoints (``GET /admin/buckets``, ``/admin/objects``,
``/admin/jobs``, ``/admin/keys``) and the Basic-auth gate (Requirement 17.5) are
covered alongside, so the auth → CSRF → handler path is exercised as a whole.

Like :mod:`tests.unit.test_observability` and :mod:`tests.unit.test_audit`, these
tests avoid a real database and the unpinned ``httpx``/``TestClient``: the router
is driven directly over ASGI by a small in-process driver, and the SQLAlchemy
session factory is a hand-written fake that routes the router's handful of
``select`` statements against in-memory rows (the same approach as
:mod:`tests.unit.test_copy_object`). The staging reservation is the real
:class:`~gateway.app.storage.staging.StagingReservation`, since its
``reserved_bytes`` is exactly the cross-check the settings handler consults.
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from fastapi import FastAPI
from gateway.app.admin.router import (
    ADMIN_PASSWORD_ENV,
    CSRF_HEADER,
    AdminAuthNotConfiguredError,
    create_admin_router,
)
from gateway.app.db.models import AccessKey, Bucket, Generation, UploadJob
from gateway.app.db.session import AsyncSessionFactory
from gateway.app.format.key_wrap import derive_root_key, unwrap
from gateway.app.storage import generations as generations_storage
from gateway.app.storage.staging import StagingReservation
from starlette.types import ASGIApp, Message, Scope

_ADMIN_PASSWORD = "s3cr3t-admin-password"
_MASTER_KEY = "unit-test-master-key-0123456789"


# ---------------------------------------------------------------------------
# In-memory DB state and the fake session that serves the router's queries
# ---------------------------------------------------------------------------


@dataclass
class _State:
    """The in-memory rows the fake session reads and mutates."""

    buckets: list[Bucket] = field(default_factory=list)
    generations: list[Generation] = field(default_factory=list)
    jobs: list[UploadJob] = field(default_factory=list)
    keys: list[AccessKey] = field(default_factory=list)


class _ScalarsResult:
    """A ``.scalars().all()`` result over a fixed list of ORM rows."""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _ScalarsResult:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)


class _RowResult:
    """A result exposing ``one`` / ``scalar_one`` / ``scalar_one_or_none``."""

    def __init__(self, value: Any) -> None:
        self._value = value

    def one(self) -> Any:
        return self._value

    def scalar_one(self) -> Any:
        return self._value

    def scalar_one_or_none(self) -> Any:
        return self._value


def _param(params: dict[str, Any], name: str) -> Any:
    """Return a bound parameter by name, tolerating SQLAlchemy's numbering."""
    if name in params:
        return params[name]
    base = name.rsplit("_", 1)[0]
    for key, value in params.items():
        if key.startswith(base):
            return value
    raise KeyError(name)


class _FakeSession:
    """Routes the admin router's ``select`` statements against :class:`_State`.

    The router issues a small, fixed set of read shapes; each is told apart by
    the selected table in ``str(statement)`` and its bound parameters. Writes
    (``add`` / ``delete``) mutate the shared state so a create-then-list or a
    delete-then-list within a test observes the change.
    """

    def __init__(self, state: _State) -> None:
        self._state = state

    async def execute(self, statement: Any) -> Any:
        description = str(statement)
        params = statement.compile().params

        if "count(generations.id)" in description and "sum(generations.size)" in description:
            # list_buckets committed (count, stored_bytes) tuple over a bucket.
            bucket_id = _param(params, "bucket_id_1")
            committed = [
                g
                for g in self._state.generations
                if g.bucket_id == bucket_id
                and g.state == generations_storage.STATE_COMMITTED
            ]
            total = sum(g.size for g in committed)
            return _RowResult((len(committed), total))

        if "count(generations.id)" in description:
            # list_buckets pending-write count over a bucket.
            bucket_id = _param(params, "bucket_id_1")
            pending_states = {
                generations_storage.STATE_STAGED,
                generations_storage.STATE_UPLOADING,
                generations_storage.STATE_RETRY_WAIT,
            }
            pending = [
                g
                for g in self._state.generations
                if g.bucket_id == bucket_id and g.state in pending_states
            ]
            return _RowResult(len(pending))

        if "FROM buckets" in description:
            if "name" in "".join(params):
                name = _param(params, "name_1")
                match = next(
                    (b for b in self._state.buckets if b.name == name), None
                )
                return _RowResult(match)
            rows = sorted(self._state.buckets, key=lambda b: b.name)
            return _ScalarsResult(rows)

        if "FROM generations" in description:
            if any(k.startswith("id") for k in params):
                gen_id = _param(params, "id_1")
                match = next(
                    (g for g in self._state.generations if g.id == gen_id), None
                )
                return _RowResult(match)
            # Object browser: COMMITTED, prefix + start_after filtered, ordered.
            bucket_id = _param(params, "bucket_id_1")
            rows = [
                g
                for g in self._state.generations
                if g.bucket_id == bucket_id
                and g.state == generations_storage.STATE_COMMITTED
            ]
            rows = _apply_object_filters(rows, description, params)
            rows.sort(key=lambda g: g.key)
            return _ScalarsResult(rows)

        if "FROM upload_jobs" in description:
            if any(k.startswith("id") for k in params):
                job_id = _param(params, "id_1")
                match = next(
                    (j for j in self._state.jobs if j.id == job_id), None
                )
                return _RowResult(match)
            rows = sorted(self._state.jobs, key=lambda j: str(j.id))
            return _ScalarsResult(rows)

        if "FROM access_keys" in description:
            if any(k.startswith("access_key_id") for k in params):
                akid = _param(params, "access_key_id_1")
                match = next(
                    (k for k in self._state.keys if k.access_key_id == akid), None
                )
                return _RowResult(match)
            rows = sorted(self._state.keys, key=lambda k: k.access_key_id)
            return _ScalarsResult(rows)

        raise AssertionError(f"unexpected statement: {description}")

    def add(self, obj: Any) -> None:
        if isinstance(obj, AccessKey):
            self._state.keys.append(obj)
        else:  # pragma: no cover - defensive
            raise AssertionError(f"unexpected add: {obj!r}")

    async def delete(self, obj: Any) -> None:
        if isinstance(obj, AccessKey):
            self._state.keys = [
                k for k in self._state.keys if k.access_key_id != obj.access_key_id
            ]
        else:  # pragma: no cover - defensive
            raise AssertionError(f"unexpected delete: {obj!r}")


def _apply_object_filters(
    rows: list[Generation], description: str, params: dict[str, Any]
) -> list[Generation]:
    """Apply the object-browser prefix and ``start_after`` filters, if present."""
    for key, value in params.items():
        if key.startswith("key_") and "LIKE" in description.upper() and value.endswith("%"):
            prefix = value[:-1]
            rows = [g for g in rows if g.key.startswith(prefix)]
        elif key.startswith("key_"):
            rows = [g for g in rows if g.key > value]
    return rows


class _FakeSessionFactory:
    """A stand-in for :class:`AsyncSessionFactory` exposing only ``session()``."""

    def __init__(self, state: _State) -> None:
        self._state = state

    @asynccontextmanager
    async def session(self) -> AsyncIterator[_FakeSession]:
        yield _FakeSession(self._state)


@dataclass
class _FakeSettings:
    """The subset of :class:`~gateway.app.config.Settings` the router reads."""

    TGOS3_MASTER_KEY: str = _MASTER_KEY
    STAGING_MAX_SIZE: int = 16 * 1024 * 1024
    CACHE_MAX_SIZE: int = 8 * 1024 * 1024
    CACHE_HALF_LIFE: int = 3600
    READ_AHEAD: int = 4
    UPLOAD_CONCURRENCY: int = 2


@dataclass
class _FakeContext:
    """The subset of :class:`~gateway.app.main.GatewayContext` the router reads."""

    session_factory: AsyncSessionFactory
    staging: StagingReservation
    settings: _FakeSettings


def _context(state: _State, staging: StagingReservation) -> Any:
    """Build a fake gateway context the router factory accepts."""
    return _FakeContext(
        session_factory=cast(AsyncSessionFactory, _FakeSessionFactory(state)),
        staging=staging,
        settings=_FakeSettings(),
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
    headers: dict[str, str] | None = None,
    body: Any = None,
) -> _Response:
    """Drive one HTTP request through ``app`` over ASGI and collect the response."""
    raw_headers: list[tuple[bytes, bytes]] = [
        (k.lower().encode("latin-1"), v.encode("latin-1"))
        for k, v in (headers or {}).items()
    ]
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


def _basic_auth(username: str = "admin", password: str = _ADMIN_PASSWORD) -> str:
    """Build an HTTP Basic ``Authorization`` header value."""
    token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    return f"Basic {token}"


def _admin_app(state: _State, staging: StagingReservation) -> FastAPI:
    """Mount the admin router on a FastAPI app with the given fakes."""
    app = FastAPI()
    app.include_router(create_admin_router(context=_context(state, staging)))
    return app


# ---------------------------------------------------------------------------
# Row factories
# ---------------------------------------------------------------------------


def _bucket(name: str) -> Bucket:
    return Bucket(
        id=uuid.uuid4(),
        name=name,
        channel_id=-100123,
        kek_wrapped=b"\x00" * 40,
        kek_version=1,
        durability="standard",
        created_by_us=True,
        status="active",
    )


def _generation(
    bucket_id: uuid.UUID,
    key: str,
    *,
    state: str = generations_storage.STATE_COMMITTED,
    size: int = 10,
) -> Generation:
    return Generation(
        id=uuid.uuid4(),
        bucket_id=bucket_id,
        key=key,
        kind="put",
        blob_id=None,
        size=size,
        etag=f'"{key}-etag"',
        checksums=None,
        content_type="application/octet-stream",
        user_meta=None,
        last_modified=datetime(2024, 1, 1, tzinfo=UTC),
        state=state,
        commit_message_id=None,
        staging_path=None,
    )


def _job(
    generation_id: uuid.UUID,
    *,
    state: str,
    attempts: int = 1,
    last_error: str | None = None,
) -> UploadJob:
    return UploadJob(
        id=uuid.uuid4(),
        generation_id=generation_id,
        state=state,
        attempts=attempts,
        next_attempt_at=None,
        last_error=last_error,
    )


# ---------------------------------------------------------------------------
# Router construction / Basic auth (Requirements 17.5, 17.6)
# ---------------------------------------------------------------------------


def test_create_router_requires_admin_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No configured admin password is fatal at construction (Requirement 17.6)."""
    monkeypatch.delenv(ADMIN_PASSWORD_ENV, raising=False)
    with pytest.raises(AdminAuthNotConfiguredError):
        create_admin_router(context=_context(_State(), StagingReservation(1024)))


@pytest.fixture
def _admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure the admin password for the duration of a test."""
    monkeypatch.setenv(ADMIN_PASSWORD_ENV, _ADMIN_PASSWORD)


async def test_read_endpoint_requires_basic_auth(_admin_env: None) -> None:
    """A read without Basic credentials is rejected with 401 (Requirement 17.5)."""
    app = _admin_app(_State(), StagingReservation(1024))
    response = await _request(app, "GET", "/admin/buckets")
    assert response.status == 401
    assert response.headers.get("www-authenticate") == "Basic"


async def test_read_endpoint_rejects_wrong_password(_admin_env: None) -> None:
    """A read with the wrong password is rejected with 401 (Requirement 17.5)."""
    app = _admin_app(_State(), StagingReservation(1024))
    response = await _request(
        app,
        "GET",
        "/admin/buckets",
        headers={"Authorization": _basic_auth(password="wrong")},
    )
    assert response.status == 401


async def test_authenticated_read_returns_csrf_token(_admin_env: None) -> None:
    """A successful read mints and returns the session CSRF token (17.7)."""
    app = _admin_app(_State(), StagingReservation(1024))
    response = await _request(
        app, "GET", "/admin/buckets", headers={"Authorization": _basic_auth()}
    )
    assert response.status == 200
    assert response.headers.get(CSRF_HEADER.lower())


# ---------------------------------------------------------------------------
# GET /admin/buckets (Requirement 19.1)
# ---------------------------------------------------------------------------


async def test_list_buckets_reports_per_bucket_stats(_admin_env: None) -> None:
    """Bucket stats count COMMITTED objects, sum bytes, and count pending (19.1)."""
    state = _State()
    bucket = _bucket("photos")
    state.buckets.append(bucket)
    state.generations.extend(
        [
            _generation(bucket.id, "a.txt", size=100),
            _generation(bucket.id, "b.txt", size=50),
            _generation(
                bucket.id,
                "c.txt",
                state=generations_storage.STATE_STAGED,
                size=999,
            ),
        ]
    )
    app = _admin_app(state, StagingReservation(1024))

    response = await _request(
        app, "GET", "/admin/buckets", headers={"Authorization": _basic_auth()}
    )
    assert response.status == 200
    [stats] = response.json()
    assert stats["name"] == "photos"
    assert stats["object_count"] == 2
    assert stats["stored_bytes"] == 150
    assert stats["pending_write_count"] == 1


# ---------------------------------------------------------------------------
# GET /admin/objects (Requirement 19.2)
# ---------------------------------------------------------------------------


async def test_list_objects_returns_committed_objects(_admin_env: None) -> None:
    """The object browser lists a bucket's COMMITTED objects in key order (19.2)."""
    state = _State()
    bucket = _bucket("docs")
    state.buckets.append(bucket)
    state.generations.extend(
        [
            _generation(bucket.id, "beta"),
            _generation(bucket.id, "alpha"),
            _generation(
                bucket.id, "gamma", state=generations_storage.STATE_STAGED
            ),
        ]
    )
    app = _admin_app(state, StagingReservation(1024))

    response = await _request(
        app,
        "GET",
        "/admin/objects?bucket=docs",
        headers={"Authorization": _basic_auth()},
    )
    assert response.status == 200
    page = response.json()
    assert page["bucket"] == "docs"
    assert [o["key"] for o in page["objects"]] == ["alpha", "beta"]
    assert page["next_start_after"] is None


async def test_list_objects_unknown_bucket_is_404(_admin_env: None) -> None:
    """Browsing a missing bucket is a 404 (Requirement 19.2)."""
    app = _admin_app(_State(), StagingReservation(1024))
    response = await _request(
        app,
        "GET",
        "/admin/objects?bucket=missing",
        headers={"Authorization": _basic_auth()},
    )
    assert response.status == 404


# ---------------------------------------------------------------------------
# GET /admin/jobs + POST /admin/jobs/{id}/retry (Requirement 19.3)
# ---------------------------------------------------------------------------


async def test_list_jobs_returns_job_rows(_admin_env: None) -> None:
    """The jobs view lists upload jobs with state and last error (19.3)."""
    state = _State()
    gen = _generation(uuid.uuid4(), "x", state=generations_storage.STATE_FAILED)
    state.generations.append(gen)
    state.jobs.append(
        _job(gen.id, state=generations_storage.STATE_FAILED, last_error="boom")
    )
    app = _admin_app(state, StagingReservation(1024))

    response = await _request(
        app, "GET", "/admin/jobs", headers={"Authorization": _basic_auth()}
    )
    assert response.status == 200
    [job] = response.json()
    assert job["state"] == generations_storage.STATE_FAILED
    assert job["last_error"] == "boom"


async def test_retry_failed_job_transitions_to_staged(_admin_env: None) -> None:
    """Retrying a FAILED job returns its generation to STAGED (19.3)."""
    state = _State()
    gen = _generation(uuid.uuid4(), "x", state=generations_storage.STATE_FAILED)
    state.generations.append(gen)
    job = _job(gen.id, state=generations_storage.STATE_FAILED, last_error="boom")
    state.jobs.append(job)
    app = _admin_app(state, StagingReservation(1024))

    token = await _csrf_token(app)
    response = await _request(
        app,
        "POST",
        f"/admin/jobs/{job.id}/retry",
        headers={"Authorization": _basic_auth(), CSRF_HEADER: token},
    )
    assert response.status == 200
    assert response.json()["state"] == generations_storage.STATE_STAGED
    assert gen.state == generations_storage.STATE_STAGED
    assert job.state == generations_storage.JOB_STATE_QUEUED
    assert job.last_error is None


async def test_retry_non_failed_job_is_409(_admin_env: None) -> None:
    """Only a FAILED job may be retried; any other state is a 409 (19.3)."""
    state = _State()
    gen = _generation(uuid.uuid4(), "x", state=generations_storage.STATE_UPLOADING)
    state.generations.append(gen)
    job = _job(gen.id, state=generations_storage.STATE_UPLOADING)
    state.jobs.append(job)
    app = _admin_app(state, StagingReservation(1024))

    token = await _csrf_token(app)
    response = await _request(
        app,
        "POST",
        f"/admin/jobs/{job.id}/retry",
        headers={"Authorization": _basic_auth(), CSRF_HEADER: token},
    )
    assert response.status == 409


# ---------------------------------------------------------------------------
# GET / PUT /admin/settings (Requirements 19.5-19.7)
# ---------------------------------------------------------------------------


async def test_get_settings_returns_runtime_values(_admin_env: None) -> None:
    """GET returns the five operator-editable runtime values (19.5)."""
    app = _admin_app(_State(), StagingReservation(16 * 1024 * 1024))
    response = await _request(
        app, "GET", "/admin/settings", headers={"Authorization": _basic_auth()}
    )
    assert response.status == 200
    body = response.json()
    assert body["staging_max_size"] == 16 * 1024 * 1024
    assert body["upload_concurrency"] == 2


async def test_put_settings_applies_valid_change(_admin_env: None) -> None:
    """A valid settings change is applied and visible on the next read (19.5)."""
    app = _admin_app(_State(), StagingReservation(16 * 1024 * 1024))
    token = await _csrf_token(app)
    new_values = {
        "staging_max_size": 32 * 1024 * 1024,
        "cache_max_size": 8 * 1024 * 1024,
        "cache_half_life": 7200,
        "read_ahead": 8,
        "upload_concurrency": 4,
    }
    put = await _request(
        app,
        "PUT",
        "/admin/settings",
        headers={"Authorization": _basic_auth(), CSRF_HEADER: token},
        body=new_values,
    )
    assert put.status == 200

    get = await _request(
        app, "GET", "/admin/settings", headers={"Authorization": _basic_auth()}
    )
    assert get.json() == new_values


async def test_put_settings_rejects_staging_below_reserved(_admin_env: None) -> None:
    """A staging reduction below the reserved bytes is rejected and not applied (19.6/19.7)."""
    staging = StagingReservation(16 * 1024 * 1024)
    handle = await staging.reserve(4 * 1024 * 1024)
    assert handle is not None
    app = _admin_app(_State(), staging)
    token = await _csrf_token(app)

    put = await _request(
        app,
        "PUT",
        "/admin/settings",
        headers={"Authorization": _basic_auth(), CSRF_HEADER: token},
        body={
            "staging_max_size": 1 * 1024 * 1024,  # below the 4 MiB reserved
            "cache_max_size": 8 * 1024 * 1024,
            "cache_half_life": 3600,
            "read_ahead": 4,
            "upload_concurrency": 2,
        },
    )
    assert put.status == 409

    # The stored value is unchanged — the original 16 MiB survives the rejection.
    get = await _request(
        app, "GET", "/admin/settings", headers={"Authorization": _basic_auth()}
    )
    assert get.json()["staging_max_size"] == 16 * 1024 * 1024


async def test_put_settings_out_of_range_is_422(_admin_env: None) -> None:
    """A value outside the model's range is rejected by validation (19.6)."""
    app = _admin_app(_State(), StagingReservation(16 * 1024 * 1024))
    token = await _csrf_token(app)
    put = await _request(
        app,
        "PUT",
        "/admin/settings",
        headers={"Authorization": _basic_auth(), CSRF_HEADER: token},
        body={
            "staging_max_size": 16 * 1024 * 1024,
            "cache_max_size": 8 * 1024 * 1024,
            "cache_half_life": 3600,
            "read_ahead": 999,  # exceeds the le=32 bound
            "upload_concurrency": 2,
        },
    )
    assert put.status == 422


# ---------------------------------------------------------------------------
# CSRF validation on write endpoints (Requirement 17.7)
# ---------------------------------------------------------------------------


async def test_write_without_csrf_token_is_403(_admin_env: None) -> None:
    """A mutating request with no CSRF token is rejected with 403 (17.7)."""
    app = _admin_app(_State(), StagingReservation(16 * 1024 * 1024))
    response = await _request(
        app,
        "PUT",
        "/admin/settings",
        headers={"Authorization": _basic_auth()},
        body={
            "staging_max_size": 16 * 1024 * 1024,
            "cache_max_size": 8 * 1024 * 1024,
            "cache_half_life": 3600,
            "read_ahead": 4,
            "upload_concurrency": 2,
        },
    )
    assert response.status == 403


async def test_write_with_mismatched_csrf_token_is_403(_admin_env: None) -> None:
    """A mutating request with a wrong CSRF token is rejected with 403 (17.7)."""
    app = _admin_app(_State(), StagingReservation(16 * 1024 * 1024))
    # Establish a session (mint the real token) but present a different one.
    await _csrf_token(app)
    response = await _request(
        app,
        "POST",
        "/admin/keys",
        headers={"Authorization": _basic_auth(), CSRF_HEADER: "not-the-token"},
        body={"label": "ci"},
    )
    assert response.status == 403


async def test_write_with_valid_csrf_token_succeeds(_admin_env: None) -> None:
    """A mutating request echoing the session token succeeds (17.7)."""
    state = _State()
    app = _admin_app(state, StagingReservation(16 * 1024 * 1024))
    token = await _csrf_token(app)
    response = await _request(
        app,
        "POST",
        "/admin/keys",
        headers={"Authorization": _basic_auth(), CSRF_HEADER: token},
        body={"label": "ci"},
    )
    assert response.status == 201
    assert len(state.keys) == 1


# ---------------------------------------------------------------------------
# Access-key management (Requirement 17.2)
# ---------------------------------------------------------------------------


async def test_create_key_returns_secret_once_and_stores_wrapped(
    _admin_env: None,
) -> None:
    """Create returns the plaintext secret once; storage holds only the wrap (17.2)."""
    state = _State()
    app = _admin_app(state, StagingReservation(16 * 1024 * 1024))
    token = await _csrf_token(app)

    response = await _request(
        app,
        "POST",
        "/admin/keys",
        headers={"Authorization": _basic_auth(), CSRF_HEADER: token},
        body={"label": "backup"},
    )
    assert response.status == 201
    created = response.json()
    secret = created["secret_access_key"]
    assert created["label"] == "backup"

    # The stored row never holds the plaintext: it round-trips through the wrap.
    [stored] = state.keys
    assert secret not in stored.secret_enc
    root_key = derive_root_key(_MASTER_KEY)
    unwrapped = unwrap(root_key, base64.b64decode(stored.secret_enc))
    assert unwrapped.decode("utf-8") == secret

    # The list view never exposes the secret.
    listed = await _request(
        app, "GET", "/admin/keys", headers={"Authorization": _basic_auth()}
    )
    [entry] = listed.json()
    assert entry["access_key_id"] == created["access_key_id"]
    assert "secret_access_key" not in entry


async def test_delete_key_removes_it(_admin_env: None) -> None:
    """Deleting an existing key returns 204 and drops the row (Requirement 17)."""
    state = _State()
    key = AccessKey(
        access_key_id="AKIAEXAMPLE000000001",
        secret_enc="d2hhdGV2ZXI=",
        label=None,
        grants=None,
        disabled=False,
    )
    state.keys.append(key)
    app = _admin_app(state, StagingReservation(16 * 1024 * 1024))
    token = await _csrf_token(app)

    response = await _request(
        app,
        "DELETE",
        f"/admin/keys/{key.access_key_id}",
        headers={"Authorization": _basic_auth(), CSRF_HEADER: token},
    )
    assert response.status == 204
    assert state.keys == []


async def test_delete_unknown_key_is_404(_admin_env: None) -> None:
    """Deleting a missing key is a 404 (Requirement 17)."""
    app = _admin_app(_State(), StagingReservation(16 * 1024 * 1024))
    token = await _csrf_token(app)
    response = await _request(
        app,
        "DELETE",
        "/admin/keys/AKIADOESNOTEXIST0001",
        headers={"Authorization": _basic_auth(), CSRF_HEADER: token},
    )
    assert response.status == 404


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _csrf_token(app: ASGIApp) -> str:
    """Perform an authenticated read and return the minted session CSRF token."""
    response = await _request(
        app, "GET", "/admin/settings", headers={"Authorization": _basic_auth()}
    )
    token = response.headers.get(CSRF_HEADER.lower())
    assert token, "expected a CSRF token on an authenticated read"
    return token

"""Bucket CRUD lifecycle integration tests (Requirements 4.2, 4.3, 4.7).

This integration test exercises the gateway's bucket-level operations end to end
against a *real* PostgreSQL 17 instance (started with ``testcontainers[postgres]``)
and the in-memory Telegram stub that stands in for per-bucket channels through
the :class:`~gateway.app.telegram.port.TelegramPort` boundary.

The behaviour under test is the design's bucket lifecycle contract
(``gateway/app/s3/operations/bucket_ops.py``):

* **CreateBucket (Requirement 4.2)** creates a private Telegram channel, writes
  a BucketManifest message to it, and inserts the ``buckets`` row — in that
  order — then returns HTTP 200. The stub channel records the manifest; the real
  :class:`~gateway.app.db.models.Bucket` row is written to the real index.
* **HeadBucket (Requirement 4.4, exercised as the create's observable effect)**
  returns HTTP 200 (no body) for a bucket that exists.
* **DeleteBucket on an empty bucket (Requirement 4.7)** deletes the channel,
  removes the row, and returns HTTP 204.
* **DeleteBucket on a non-empty bucket (Requirement 4.3)** returns HTTP 409 and
  mutates nothing — neither the channel nor the row.

Both scenarios drive the *production* handlers (:class:`CreateBucketHandler`,
:class:`HeadBucketHandler`, :class:`DeleteBucketHandler`) wired to a real
:class:`~gateway.app.db.session.AsyncSessionFactory` and the stub transport. To
make a bucket non-empty the test performs a full synchronous-durability PutObject
through the real :class:`~gateway.app.s3.operations.put_object.PutObjectHandler`
driven by the production :class:`~gateway.app.main.WorkerCommitDriver` and
:class:`~gateway.app.jobs.upload_worker.UploadWorker`, so the committed
generation that blocks the delete is a genuine object write, not a seeded row.

The suite needs a Docker daemon to launch PostgreSQL. When no container runtime
is available (CI without Docker, or a sandbox) the module skips cleanly rather
than failing, so ``make lint`` / ``make typecheck`` stay meaningful without a
daemon present. The Telegram side never touches the network.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from gateway.app.config import Settings
from gateway.app.db.models import Bucket
from gateway.app.db.session import AsyncSessionFactory
from gateway.app.jobs.upload_worker import UploadWorker
from gateway.app.main import WorkerCommitDriver
from gateway.app.s3 import router as s3_router
from gateway.app.s3.operations.bucket_ops import (
    CreateBucketHandler,
    DeleteBucketHandler,
    HeadBucketHandler,
)
from gateway.app.s3.operations.put_object import PutObjectHandler
from gateway.app.storage.staging import StagingReservation
from gateway.app.telegram.port import TelegramPort
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.requests import Request

from tests.integration.fault_injection import FaultInjectingTelegramPort

# ``testcontainers`` is an integration-only dependency and starting a container
# requires a Docker daemon. Guard the import so the module can be collected
# everywhere and skip with a clear reason when the package is absent.
try:
    from testcontainers.postgres import PostgresContainer

    _TESTCONTAINERS_IMPORT_ERROR: str | None = None
except Exception as exc:  # pragma: no cover - exercised only without the extra
    PostgresContainer = None  # type: ignore[assignment,misc]
    _TESTCONTAINERS_IMPORT_ERROR = str(exc)


# Repository root: tests/integration/<file>.py -> repo root is two parents up.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_ALEMBIC_INI = _REPO_ROOT / "alembic.ini"

#: Postgres image pinned to the major version the design mandates (17+).
_POSTGRES_IMAGE = "postgres:17-alpine"

#: Master key used to derive the root key that wraps bucket KEKs and seals
#: records. Satisfies the >=16-char ``TGOS3_MASTER_KEY`` contract.
_MASTER_KEY = "integration-test-master-key-0001"

#: The empty bucket exercised through the create/head/delete happy path.
_EMPTY_BUCKET = "bucket-crud-empty"

#: The bucket made non-empty by a committed object so delete is refused.
_NONEMPTY_BUCKET = "bucket-crud-nonempty"
_OBJECT_KEY = "nested/path/object.bin"

#: A small multi-record body so the PUT exercises the real write path without
#: being large. ``record_codec.MAX_PLAINTEXT`` is 1,048,560 bytes.
_OBJECT_BODY = (b"tgobjects3-bucket-crud-payload!" * 10_000)[:300_000]

#: Caption prefix the gateway writes for a BucketManifest message
#: (``tgo3-k-<version>``), mirrored from the design "Telegram Message Format".
_MANIFEST_PREFIX = "tgo3-k-"


# ---------------------------------------------------------------------------
# A minimal in-process request object for driving operation handlers
# ---------------------------------------------------------------------------


def _build_asgi_request(
    method: str,
    path: str,
    *,
    headers: dict[str, str],
    body: bytes,
) -> Request:
    """Build a real Starlette :class:`Request` streaming ``body``.

    The operation handlers read the body through ``request.stream()`` and the
    request headers through ``request.headers`` — both are the genuine Starlette
    surfaces, so constructing a real ``Request`` over an ASGI scope exercises the
    same code paths a live uvicorn request would, without standing up a server.
    """
    raw_headers = [
        (name.lower().encode("latin-1"), value.encode("latin-1"))
        for name, value in headers.items()
    ]
    scope: dict[str, Any] = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "path": path,
        "raw_path": path.encode("latin-1"),
        "query_string": b"",
        "headers": raw_headers,
        "client": ("127.0.0.1", 12345),
        "server": ("localhost", 9000),
        "scheme": "http",
    }

    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    return Request(scope, receive)


def _make_context(
    operation: s3_router.S3Operation,
    *,
    method: str,
    settings: Settings,
    bucket: str,
    key: str | None = None,
    headers: dict[str, str] | None = None,
    body: bytes = b"",
) -> s3_router.S3HandlerContext:
    """Assemble the handler context the router would hand an operation handler.

    SigV4 has already run by the time a handler is invoked, so the test supplies
    a throwaway sentinel for ``auth``; the bucket and PutObject handlers never
    consult it (authentication is the router's concern, Requirement 6.1).
    """
    path = f"/{bucket}" if key is None else f"/{bucket}/{key}"
    request = _build_asgi_request(method, path, headers=headers or {}, body=body)
    return s3_router.S3HandlerContext(
        request=request,
        operation=operation,
        target=s3_router.ResolvedTarget(bucket=bucket, key=key),
        auth=cast("Any", object()),
        settings=settings,
    )


# ---------------------------------------------------------------------------
# PostgreSQL helpers (shared pattern with test_put_object_sync_durability)
# ---------------------------------------------------------------------------


def _async_url(sync_url: str) -> str:
    """Normalise a container connection URL to the ``asyncpg`` driver."""
    bare = sync_url.split("://", 1)[1]
    return "postgresql+asyncpg://" + bare


def _run_alembic_upgrade(async_url: str) -> None:
    """Run ``alembic upgrade head`` through the project's real ``env.py``.

    Creates the schema the bucket/write path uses, exactly as the gateway does
    at startup. Runs from the repo root so the relative ``script_location``
    resolves.
    """
    from alembic import command
    from alembic.config import Config

    config = Config(str(_ALEMBIC_INI))
    config.set_main_option("script_location", "gateway/app/db/migrations")

    previous_url = os.environ.get("DATABASE_URL")
    previous_cwd = Path.cwd()
    os.environ["DATABASE_URL"] = async_url
    try:
        os.chdir(_REPO_ROOT)
        command.upgrade(config, "head")
    finally:
        os.chdir(previous_cwd)
        if previous_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_url


def _build_settings(async_url: str) -> Settings:
    """Construct a frozen :class:`Settings` for the lifecycle run.

    Durability defaults to ``sync`` so the non-empty PutObject blocks until its
    generation is COMMITTED before the delete-refusal assertion runs; the
    staging/object ceilings sit well above the test body. Configuration is
    consumed only through this object, per the centralised-config convention.
    """
    return Settings(
        TGOS3_MASTER_KEY=_MASTER_KEY,
        DATABASE_URL=async_url,
        DEFAULT_DURABILITY="sync",
        STAGING_MAX_SIZE=64 * 1024 * 1024,
        MAX_OBJECT_SIZE=64 * 1024 * 1024,
        CHUNK_RECORDS=512,
        UPLOAD_CONCURRENCY=2,
        READ_AHEAD=4,
        CACHE_ENCRYPT=False,
    )


# ---------------------------------------------------------------------------
# Lifecycle driver
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _LifecycleResult:
    """The observable outcomes of the two bucket-lifecycle scenarios."""

    # Empty-bucket happy path.
    create_status: int
    head_status: int
    manifest_messages_after_create: int
    row_present_after_create: bool
    channel_present_after_create: bool
    delete_empty_status: int
    row_present_after_delete: bool
    channel_present_after_delete: bool

    # Non-empty bucket refusal.
    put_status: int
    delete_nonempty_status: int
    row_present_after_refused_delete: bool
    channel_present_after_refused_delete: bool


async def _run_lifecycle(async_url: str, tmp_path: Path) -> _LifecycleResult:
    """Drive both bucket-lifecycle scenarios and capture their observables.

    Wires the production bucket handlers and the full PutObject write path around
    a real session factory and the in-memory Telegram stub, then runs:

    1. the empty-bucket path — CreateBucket, HeadBucket, DeleteBucket; and
    2. the non-empty path — CreateBucket, a sync PutObject, then a DeleteBucket
       that must be refused with 409 while mutating nothing.
    """
    settings = _build_settings(async_url)
    telegram = FaultInjectingTelegramPort()

    staging_dir = tmp_path / "staging"

    factory = AsyncSessionFactory.create(async_url)
    engine = factory.engine
    try:
        create_handler = CreateBucketHandler(factory, cast(TelegramPort, telegram))
        head_handler = HeadBucketHandler(factory)
        delete_handler = DeleteBucketHandler(factory, cast(TelegramPort, telegram))

        # ---- Scenario 1: empty bucket create / head / delete -------------
        create_response = await create_handler(
            _make_context(
                s3_router.S3Operation.CREATE_BUCKET,
                method="PUT",
                settings=settings,
                bucket=_EMPTY_BUCKET,
            )
        )
        empty_channel_id = await _channel_id_for(engine, _EMPTY_BUCKET)
        manifest_count = (
            _count_manifest_messages(telegram, empty_channel_id)
            if empty_channel_id is not None
            else 0
        )
        row_after_create = await _bucket_row_exists(engine, _EMPTY_BUCKET)
        channel_after_create = _channel_exists(telegram, empty_channel_id)

        head_response = await head_handler(
            _make_context(
                s3_router.S3Operation.HEAD_BUCKET,
                method="HEAD",
                settings=settings,
                bucket=_EMPTY_BUCKET,
            )
        )

        delete_empty_response = await delete_handler(
            _make_context(
                s3_router.S3Operation.DELETE_BUCKET,
                method="DELETE",
                settings=settings,
                bucket=_EMPTY_BUCKET,
            )
        )
        row_after_delete = await _bucket_row_exists(engine, _EMPTY_BUCKET)
        channel_after_delete = _channel_exists(telegram, empty_channel_id)

        # ---- Scenario 2: non-empty bucket refuses delete -----------------
        await create_handler(
            _make_context(
                s3_router.S3Operation.CREATE_BUCKET,
                method="PUT",
                settings=settings,
                bucket=_NONEMPTY_BUCKET,
            )
        )
        nonempty_channel_id = await _channel_id_for(engine, _NONEMPTY_BUCKET)
        assert nonempty_channel_id is not None

        put_status = await _put_object(
            factory, telegram, settings, staging_dir
        )

        delete_nonempty_response = await delete_handler(
            _make_context(
                s3_router.S3Operation.DELETE_BUCKET,
                method="DELETE",
                settings=settings,
                bucket=_NONEMPTY_BUCKET,
            )
        )
        row_after_refused = await _bucket_row_exists(engine, _NONEMPTY_BUCKET)
        channel_after_refused = _channel_exists(telegram, nonempty_channel_id)
    finally:
        await factory.dispose()

    return _LifecycleResult(
        create_status=create_response.status_code,
        head_status=head_response.status_code,
        manifest_messages_after_create=manifest_count,
        row_present_after_create=row_after_create,
        channel_present_after_create=channel_after_create,
        delete_empty_status=delete_empty_response.status_code,
        row_present_after_delete=row_after_delete,
        channel_present_after_delete=channel_after_delete,
        put_status=put_status,
        delete_nonempty_status=delete_nonempty_response.status_code,
        row_present_after_refused_delete=row_after_refused,
        channel_present_after_refused_delete=channel_after_refused,
    )


async def _put_object(
    factory: AsyncSessionFactory,
    telegram: FaultInjectingTelegramPort,
    settings: Settings,
    staging_dir: Path,
) -> int:
    """Perform a full sync-durability PutObject into the non-empty bucket.

    Wires the production :class:`PutObjectHandler` to the real
    :class:`UploadWorker` through :class:`WorkerCommitDriver`, so the generation
    the delete-refusal observes is a genuinely committed object write.
    """
    worker = UploadWorker(
        factory.sessionmaker,
        cast(TelegramPort, telegram),
        settings.TGOS3_MASTER_KEY,
        upload_concurrency=settings.UPLOAD_CONCURRENCY,
        default_chunk_records=settings.CHUNK_RECORDS,
    )
    commit_driver = WorkerCommitDriver(factory, worker)
    put_handler = PutObjectHandler(
        session_factory=factory.sessionmaker,
        staging=StagingReservation(settings.STAGING_MAX_SIZE),
        staging_dir=staging_dir,
        commit_driver=commit_driver,
    )
    put_context = _make_context(
        s3_router.S3Operation.PUT_OBJECT,
        method="PUT",
        settings=settings,
        bucket=_NONEMPTY_BUCKET,
        key=_OBJECT_KEY,
        headers={
            "content-length": str(len(_OBJECT_BODY)),
            "content-type": "application/octet-stream",
        },
        body=_OBJECT_BODY,
    )
    response = await asyncio.wait_for(put_handler(put_context), timeout=30.0)
    return response.status_code


async def _channel_id_for(engine: AsyncEngine, bucket_name: str) -> int | None:
    """Return the ``channel_id`` recorded for a bucket, or ``None`` if absent."""
    async with engine.connect() as conn:
        result = await conn.execute(
            select(Bucket.channel_id).where(Bucket.name == bucket_name)
        )
        row = result.first()
        return None if row is None else int(row.channel_id)


async def _bucket_row_exists(engine: AsyncEngine, bucket_name: str) -> bool:
    """Return whether a ``buckets`` row for ``bucket_name`` is present."""
    async with engine.connect() as conn:
        result = await conn.execute(
            select(func.count())
            .select_from(Bucket)
            .where(Bucket.name == bucket_name)
        )
        return int(result.scalar_one()) > 0


def _channel_exists(
    telegram: FaultInjectingTelegramPort, channel_id: int | None
) -> bool:
    """Return whether the stub still holds a channel for ``channel_id``.

    The stub drops a channel from its store on ``delete_channel``; a deleted (or
    never-created) channel reports no messages even when inspected including
    dropped ones, which models a channel that is gone.
    """
    if channel_id is None:
        return False
    return telegram.channel_exists(channel_id)


def _count_manifest_messages(
    telegram: FaultInjectingTelegramPort, channel_id: int
) -> int:
    """Count BucketManifest messages (``tgo3-k-``) in the stub channel."""
    return sum(
        1
        for message in telegram.messages(channel_id)
        if message.caption.startswith(_MANIFEST_PREFIX)
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _skip_if_no_docker() -> None:
    """Skip the whole module when ``testcontainers`` cannot be imported."""
    if PostgresContainer is None:
        pytest.skip(
            "testcontainers[postgres] is unavailable: "
            f"{_TESTCONTAINERS_IMPORT_ERROR}"
        )


@pytest.fixture(scope="module")
def lifecycle(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_LifecycleResult]:
    """Start PostgreSQL 17, run both lifecycle scenarios once, yield the result.

    Module-scoped so the expensive container start, migration run, and the two
    scenarios happen once for every assertion in this file. Skips cleanly when
    Docker is unavailable so the suite stays collectable everywhere.
    """
    _skip_if_no_docker()

    try:
        container = PostgresContainer(_POSTGRES_IMAGE)
        container.start()
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"Could not start PostgreSQL test container: {exc}")

    try:
        async_url = _async_url(container.get_connection_url())
        _run_alembic_upgrade(async_url)
        tmp_path = tmp_path_factory.mktemp("bucket-crud")
        result = asyncio.run(_run_lifecycle(async_url, tmp_path))
        yield result
    finally:
        container.stop()


# ---------------------------------------------------------------------------
# Tests — empty bucket create / head / delete (Requirements 4.2, 4.7)
# ---------------------------------------------------------------------------


def test_create_bucket_returns_200(lifecycle: _LifecycleResult) -> None:
    """CreateBucket for a fresh name returns HTTP 200.

    Validates: Requirements 4.2
    """
    assert lifecycle.create_status == 200


def test_create_bucket_writes_channel_manifest_and_row(
    lifecycle: _LifecycleResult,
) -> None:
    """CreateBucket produces all three side effects: channel, manifest, row.

    The ordered create writes a private Telegram channel, a single BucketManifest
    message to it, and the ``buckets`` index row.

    Validates: Requirements 4.2
    """
    assert lifecycle.channel_present_after_create is True
    assert lifecycle.manifest_messages_after_create == 1
    assert lifecycle.row_present_after_create is True


def test_head_bucket_returns_200_for_existing_bucket(
    lifecycle: _LifecycleResult,
) -> None:
    """HeadBucket returns HTTP 200 for a bucket that exists.

    Validates: Requirements 4.4
    """
    assert lifecycle.head_status == 200


def test_delete_empty_bucket_returns_204(lifecycle: _LifecycleResult) -> None:
    """DeleteBucket on an empty bucket returns HTTP 204.

    Validates: Requirements 4.7
    """
    assert lifecycle.delete_empty_status == 204


def test_delete_empty_bucket_removes_channel_and_row(
    lifecycle: _LifecycleResult,
) -> None:
    """DeleteBucket on an empty bucket tears down both the channel and the row.

    Validates: Requirements 4.7
    """
    assert lifecycle.channel_present_after_delete is False
    assert lifecycle.row_present_after_delete is False


# ---------------------------------------------------------------------------
# Tests — non-empty bucket refuses delete (Requirement 4.3)
# ---------------------------------------------------------------------------


def test_put_into_nonempty_bucket_committed(lifecycle: _LifecycleResult) -> None:
    """The object used to make the bucket non-empty was committed (HTTP 200).

    This is the precondition for the delete-refusal assertions: a sync PUT only
    returns 200 once its generation is COMMITTED, so the bucket genuinely holds a
    live object when the delete is attempted.

    Validates: Requirements 4.3
    """
    assert lifecycle.put_status == 200


def test_delete_nonempty_bucket_returns_409(lifecycle: _LifecycleResult) -> None:
    """DeleteBucket on a bucket with a committed object returns HTTP 409.

    Validates: Requirements 4.3
    """
    assert lifecycle.delete_nonempty_status == 409


def test_refused_delete_mutates_nothing(lifecycle: _LifecycleResult) -> None:
    """A refused DeleteBucket leaves the channel and the index row untouched.

    Validates: Requirements 4.3
    """
    assert lifecycle.channel_present_after_refused_delete is True
    assert lifecycle.row_present_after_refused_delete is True

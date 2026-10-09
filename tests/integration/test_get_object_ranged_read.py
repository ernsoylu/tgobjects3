"""GetObject ranged-read round-trip (Requirements 8.2, 8.3).

This integration test exercises the gateway's ranged read path end to end against
a *real* PostgreSQL 17 instance (started with ``testcontainers[postgres]``) and an
in-memory Telegram stub that stands in for a bucket channel through the
:class:`~gateway.app.telegram.port.TelegramPort` boundary.

The guarantee under test is Requirement 8's ranged-read contract: when a ``Range``
header is present, the gateway maps the requested byte range to the minimal set of
Records that covers it (Requirement 8.2) and returns HTTP 206 Partial Content with
a ``Content-Range`` header matching the actual bytes served (Requirement 8.3). The
test writes a 10 MiB object through the real :class:`PutObjectHandler` wired to the
production write path (the same wiring as ``test_put_object_sync_durability``), then
issues a GetObject with a ``Range`` header whose byte range deliberately straddles
the boundary between record 0 and record 1 (record size =
``record_codec.MAX_PLAINTEXT`` = 1,048,560 bytes), and asserts that:

1. the GET returns HTTP 206 Partial Content (not a full 200);
2. the ``Content-Range`` header names exactly the requested, inclusive byte range
   and the object's true size (``bytes <start>-<end>/<size>``);
3. the ``Content-Length`` header equals the number of bytes in the range; and
4. the returned bytes are byte-for-byte the corresponding slice of the uploaded
   body — proving the two straddled records were downloaded, unsealed, and
   reassembled correctly across the record boundary.

The read path's collaborators are the production objects, not fakes: the
:class:`UploadWorker` seals real AES-256-GCM records and uploads them to the stub
channel, and the :class:`GetObjectHandler` downloads those same chunk documents
back, unseals the two straddled records, and slices them to the requested range.
Only the Telegram transport is a stub — the in-memory
:class:`FaultInjectingTelegramPort` from the fault-injection harness, driven here
with no faults configured so it behaves like a faithful channel store.

The suite needs a Docker daemon to launch PostgreSQL. When no container runtime is
available (CI without Docker, or a sandbox) the module skips cleanly rather than
failing, so ``make lint`` / ``make typecheck`` stay meaningful without a daemon
present. The Telegram side never touches the network.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from gateway.app.cache.store import RecordCache
from gateway.app.config import Settings
from gateway.app.db.session import AsyncSessionFactory
from gateway.app.format import record_codec
from gateway.app.format.key_wrap import derive_root_key, wrap
from gateway.app.jobs.upload_worker import UploadWorker
from gateway.app.main import WorkerCommitDriver
from gateway.app.s3 import router as s3_router
from gateway.app.s3.operations.get_object import GetObjectHandler
from gateway.app.s3.operations.put_object import PutObjectHandler
from gateway.app.storage.staging import StagingReservation
from gateway.app.telegram.port import TelegramPort
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

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

#: Master key used to derive the root key that wraps the bucket KEK and seals
#: records. Satisfies the >=16-char ``TGOS3_MASTER_KEY`` contract and keeps the
#: run deterministic.
_MASTER_KEY = "integration-test-master-key-0001"

#: The bucket the object is written to; its channel is backed by the stub.
_BUCKET_NAME = "ranged-read-bucket"
_OBJECT_KEY = "nested/path/ten-mib.bin"

#: A 10 MiB object body. At ``record_codec.MAX_PLAINTEXT`` (1,048,560) bytes per
#: record this packs into 10 full records plus a short final record, so a range
#: straddling the record 0/1 boundary genuinely exercises multi-record reads.
_OBJECT_SIZE = 10 * 1024 * 1024  # 10,485,760 bytes
_OBJECT_BODY = (b"tgobjects3-ranged-read-payload!!" * ((_OBJECT_SIZE // 32) + 1))[
    :_OBJECT_SIZE
]

#: Requested byte range. Record 0 covers bytes [0, 1,048,559] and record 1 begins
#: at 1,048,560, so [1,048,000, 1,049,000] straddles the record 0/1 boundary and
#: must be served from both records (Requirement 8.2). Bounds are inclusive.
_RANGE_START = 1_048_000
_RANGE_END = 1_049_000


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

    The operation handlers read the body through ``request.stream()`` and read
    request headers through ``request.headers`` — both are the genuine Starlette
    surfaces, so constructing a real ``Request`` over an ASGI scope exercises the
    same code paths a live uvicorn request would, without standing up a server.
    The ``receive`` callable yields the whole body in one ``http.request`` event
    and then signals end-of-stream.
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
    headers: dict[str, str],
    body: bytes,
    settings: Settings,
    bucket: str,
    key: str,
) -> s3_router.S3HandlerContext:
    """Assemble the handler context the router would hand an operation handler.

    SigV4 has already run by the time a handler is invoked, so the test supplies
    a placeholder :class:`~gateway.app.s3.sigv4.AuthResult`-shaped sentinel; the
    PutObject and GetObject handlers never consult ``auth`` (authentication is the
    router's concern, Requirement 6.1), so a throwaway object is sufficient and
    keeps the test focused on the ranged-read contract.
    """
    request = _build_asgi_request(method, f"/{bucket}/{key}", headers=headers, body=body)
    return s3_router.S3HandlerContext(
        request=request,
        operation=operation,
        target=s3_router.ResolvedTarget(bucket=bucket, key=key),
        auth=cast("Any", object()),
        settings=settings,
    )


async def _drain_response_body(response: StreamingResponse) -> bytes:
    """Collect the full body of a :class:`StreamingResponse` into bytes.

    A ranged GetObject returns a streaming response whose generator yields the
    requested records trimmed to the range; iterating it to exhaustion reproduces
    exactly the bytes a client would receive on the wire.
    """
    chunks: list[bytes] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk if isinstance(chunk, bytes) else bytes(chunk))
    return b"".join(chunks)


# ---------------------------------------------------------------------------
# PostgreSQL helpers (shared pattern with test_put_object_sync_durability)
# ---------------------------------------------------------------------------


def _async_url(sync_url: str) -> str:
    """Normalise a container connection URL to the ``asyncpg`` driver."""
    bare = sync_url.split("://", 1)[1]
    return "postgresql+asyncpg://" + bare


def _run_alembic_upgrade(async_url: str) -> None:
    """Run ``alembic upgrade head`` through the project's real ``env.py``.

    Creates the schema the write/read path uses, exactly as the gateway does at
    startup. Runs from the repo root so the relative ``script_location`` resolves.
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


async def _seed_bucket_row(
    engine: AsyncEngine, bucket_id: uuid.UUID, channel_id: int, kek_wrapped: bytes
) -> None:
    """Insert the ``sync``-durability bucket row the write path resolves.

    The PutObject handler loads the bucket to read its durability mode and KEK;
    the Upload Worker unwraps that KEK to wrap the per-blob key. The row is seeded
    directly (bucket creation is a separate operation) so the test targets the
    ranged read in isolation.
    """
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO buckets "
                "(id, name, channel_id, kek_wrapped, kek_version, durability, "
                " created_by_us, status) "
                "VALUES (:id, :name, :channel_id, :kek_wrapped, :kek_version, "
                " :durability, :created_by_us, :status)"
            ),
            {
                "id": bucket_id,
                "name": _BUCKET_NAME,
                "channel_id": channel_id,
                "kek_wrapped": kek_wrapped,
                "kek_version": 1,
                "durability": "sync",
                "created_by_us": True,
                "status": "active",
            },
        )


# ---------------------------------------------------------------------------
# Round-trip driver
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _RangedReadResult:
    """The observable outcome of one PUT-then-ranged-GET round-trip."""

    put_status: int
    get_status: int
    content_range: str | None
    content_length: str | None
    accept_ranges: str | None
    get_body: bytes


def _build_settings(async_url: str) -> Settings:
    """Construct a frozen :class:`Settings` for the round-trip.

    Durability defaults to ``sync`` so the PUT is durable before the GET runs; the
    staging and object ceilings are set above the 10 MiB body so the write path
    never trips the 507/413 guards. ``CHUNK_RECORDS`` is left large enough that the
    whole object packs into a single chunk document — the ranged read still fetches
    only the two straddled records out of that document. Configuration is consumed
    only through this object, per the project's centralised-config convention.
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


async def _run_round_trip(async_url: str, tmp_path: Path) -> _RangedReadResult:
    """Seed the bucket, PUT the 10 MiB object, then GET a straddling byte range.

    Wires the production write/read collaborators around a real session factory
    and the in-memory Telegram stub, then drives one PUT and one ranged GET through
    the real handlers. The commit boundary is advanced by the production
    :class:`WorkerCommitDriver`, which pumps the real :class:`UploadWorker` until
    the generation reaches a terminal state.
    """
    settings = _build_settings(async_url)
    telegram = FaultInjectingTelegramPort()
    channel_id = await telegram.create_channel("ranged-read-bucket-channel")

    root_key = derive_root_key(_MASTER_KEY)
    kek_wrapped = wrap(root_key, os.urandom(32))
    bucket_id = uuid.uuid4()

    staging_dir = tmp_path / "staging"
    cache_dir = tmp_path / "cache"

    factory = AsyncSessionFactory.create(async_url)
    engine = factory.engine
    try:
        await _seed_bucket_row(engine, bucket_id, channel_id, kek_wrapped)

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
        cache = RecordCache(settings, factory, cache_root=cache_dir)
        get_handler = GetObjectHandler(factory, cast(TelegramPort, telegram), cache, settings)

        # --- PUT (synchronous durability) ---------------------------------
        put_context = _make_context(
            s3_router.S3Operation.PUT_OBJECT,
            method="PUT",
            headers={
                "content-length": str(len(_OBJECT_BODY)),
                "content-type": "application/octet-stream",
            },
            body=_OBJECT_BODY,
            settings=settings,
            bucket=_BUCKET_NAME,
            key=_OBJECT_KEY,
        )
        put_response = await asyncio.wait_for(put_handler(put_context), timeout=60.0)

        # --- GET (ranged read spanning the record 0/1 boundary) -----------
        get_context = _make_context(
            s3_router.S3Operation.GET_OBJECT,
            method="GET",
            headers={"range": f"bytes={_RANGE_START}-{_RANGE_END}"},
            body=b"",
            settings=settings,
            bucket=_BUCKET_NAME,
            key=_OBJECT_KEY,
        )
        get_response = await get_handler(get_context)
        get_body = await _read_response_body(get_response)
    finally:
        await factory.dispose()

    return _RangedReadResult(
        put_status=put_response.status_code,
        get_status=get_response.status_code,
        content_range=get_response.headers.get("content-range"),
        content_length=get_response.headers.get("content-length"),
        accept_ranges=get_response.headers.get("accept-ranges"),
        get_body=get_body,
    )


async def _read_response_body(response: Response) -> bytes:
    """Return the full body of a GET response, streaming or buffered."""
    if isinstance(response, StreamingResponse):
        return await _drain_response_body(response)
    return bytes(getattr(response, "body", b""))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _skip_if_no_docker() -> None:
    """Skip the whole module when ``testcontainers`` cannot be imported."""
    if PostgresContainer is None:
        pytest.skip(
            f"testcontainers[postgres] is unavailable: {_TESTCONTAINERS_IMPORT_ERROR}"
        )


@pytest.fixture(scope="module")
def ranged_read(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_RangedReadResult]:
    """Start PostgreSQL 17, run one PUT-then-ranged-GET round-trip, yield the result.

    Module-scoped so the expensive container start, migration run, 10 MiB PUT and
    ranged GET happen once for every assertion in this file. Skips cleanly when
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
        tmp_path = tmp_path_factory.mktemp("ranged-read")
        result = asyncio.run(_run_round_trip(async_url, tmp_path))
        yield result
    finally:
        container.stop()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_put_precondition_returns_200(ranged_read: _RangedReadResult) -> None:
    """The 10 MiB object is durably stored before the ranged read runs.

    A successful sync PUT is the precondition for the ranged-read assertions: the
    object must exist and be readable back for the Range request to be meaningful.
    """
    assert ranged_read.put_status == 200


def test_range_spanning_two_records_returns_206(ranged_read: _RangedReadResult) -> None:
    """A Range request spanning two records returns HTTP 206 Partial Content.

    The requested range [1,048,000, 1,049,000] straddles the record 0/1 boundary
    (record size = ``MAX_PLAINTEXT`` = 1,048,560), so the gateway serves a partial
    response rather than the full object.

    Validates: Requirements 8.3
    """
    # Sanity-check the range genuinely crosses the record boundary this test
    # claims to exercise, so the assertion below cannot pass vacuously.
    assert _RANGE_START < record_codec.MAX_PLAINTEXT <= _RANGE_END
    assert ranged_read.get_status == 206


def test_content_range_matches_bytes_served(ranged_read: _RangedReadResult) -> None:
    """The ``Content-Range`` header names the requested range and true size.

    Requirement 8.3 ties the ``Content-Range`` header to the bytes actually served:
    ``bytes <start>-<end>/<size>`` with inclusive bounds. The gateway also
    advertises ``Accept-Ranges: bytes`` and a ``Content-Length`` equal to the
    range length.

    Validates: Requirements 8.3
    """
    expected_length = _RANGE_END - _RANGE_START + 1
    assert ranged_read.content_range == (
        f"bytes {_RANGE_START}-{_RANGE_END}/{_OBJECT_SIZE}"
    )
    assert ranged_read.content_length == str(expected_length)
    assert ranged_read.accept_ranges == "bytes"


def test_ranged_body_equals_source_slice(ranged_read: _RangedReadResult) -> None:
    """The returned bytes equal the corresponding slice of the uploaded body.

    Reassembling the two straddled records and trimming them to the inclusive range
    must reproduce ``body[start : end + 1]`` byte-for-byte. This proves the minimal
    covering record set (Requirement 8.2) was downloaded, unsealed, and sliced
    correctly across the record boundary.

    Validates: Requirements 8.2, 8.3
    """
    expected = _OBJECT_BODY[_RANGE_START : _RANGE_END + 1]
    assert len(ranged_read.get_body) == len(expected)
    assert ranged_read.get_body == expected

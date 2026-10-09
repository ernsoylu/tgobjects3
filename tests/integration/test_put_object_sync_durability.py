"""PutObject synchronous-durability round-trip (Requirements 6.13, 14.5).

This integration test exercises the gateway's write-then-read path end to end
against a *real* PostgreSQL 17 instance (started with ``testcontainers[postgres]``)
and an in-memory Telegram stub that stands in for a bucket channel through the
:class:`~gateway.app.telegram.port.TelegramPort` boundary.

The guarantee under test is the design's synchronous-durability contract: for a
bucket with ``sync`` durability a PutObject SHALL NOT return HTTP 200 until the
generation's Commit Record has been confirmed by Telegram and the object head has
advanced to it (Requirement 6.13; Requirement 14.5 phrases the same boundary as
"no 200 until the Commit Record message_id confirmation has been received"). The
test drives a full PUT through the real :class:`PutObjectHandler` wired to the
real :class:`~gateway.app.jobs.upload_worker.UploadWorker` (via the production
:class:`~gateway.app.main.WorkerCommitDriver`) and asserts that:

1. the PUT returns HTTP 200;
2. at the moment the 200 is produced the generation is already COMMITTED — the
   durability boundary — with its ``commit_message_id`` recorded and the object
   head pointing at it (the 200 is not returned at STAGED);
3. the Commit Record was actually written to the Telegram channel (the last of
   the three message types the write path emits, Requirement 14.1); and
4. a subsequent GetObject returns byte-for-byte the same body the client PUT,
   proving the records are durable and readable back through Telegram.

The write path's collaborators are the production objects, not fakes: the
:class:`UploadWorker` seals real AES-256-GCM records, sends the data chunks, the
Blob Manifest and the Commit Record to the stub channel, and advances the
generation to COMMITTED in one transaction; the :class:`GetObjectHandler` then
downloads those same chunk documents back and unseals them. Only the Telegram
transport is a stub — the in-memory :class:`FaultInjectingTelegramPort` from the
fault-injection harness, driven here with no faults configured so it behaves like
a faithful channel store.

The suite needs a Docker daemon to launch PostgreSQL. When no container runtime
is available (CI without Docker, or a sandbox) the module skips cleanly rather
than failing, so ``make lint`` / ``make typecheck`` stay meaningful without a
daemon present. The Telegram side never touches the network.
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
from gateway.app.db.models import Generation, ObjectHead
from gateway.app.db.session import AsyncSessionFactory
from gateway.app.format.key_wrap import derive_root_key, wrap
from gateway.app.jobs.upload_worker import UploadWorker
from gateway.app.main import WorkerCommitDriver
from gateway.app.s3 import router as s3_router
from gateway.app.s3.operations.get_object import GetObjectHandler
from gateway.app.s3.operations.put_object import PutObjectHandler
from gateway.app.storage import generations as generations_storage
from gateway.app.storage.staging import StagingReservation
from gateway.app.telegram.port import TelegramPort
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.requests import Request
from starlette.responses import StreamingResponse

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
_BUCKET_NAME = "sync-durability-bucket"
_OBJECT_KEY = "nested/path/object.bin"

#: A body a few records long so the write path exercises real chunk packing and
#: GET exercises multi-record reassembly, while staying small enough to run fast.
#: ``record_codec.MAX_PLAINTEXT`` is 1,048,560 bytes; ~2.5 records keeps the
#: object multi-record without being large.
_OBJECT_BODY = (b"tgobjects3-sync-durability-payload!" * 100_000)[:2_600_000]


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
    keeps the test focused on the durability boundary.
    """
    request = _build_asgi_request(
        method, f"/{bucket}/{key}", headers=headers, body=body
    )
    return s3_router.S3HandlerContext(
        request=request,
        operation=operation,
        target=s3_router.ResolvedTarget(bucket=bucket, key=key),
        auth=cast("Any", object()),
        settings=settings,
    )


async def _drain_response_body(response: StreamingResponse) -> bytes:
    """Collect the full body of a :class:`StreamingResponse` into bytes.

    GetObject returns a streaming response whose generator yields the object's
    records in order; iterating it to exhaustion reproduces exactly the bytes a
    client would receive on the wire.
    """
    chunks: list[bytes] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk if isinstance(chunk, bytes) else bytes(chunk))
    return b"".join(chunks)


# ---------------------------------------------------------------------------
# PostgreSQL helpers (shared pattern with test_recovery_round_trip)
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
    PutObject durability boundary in isolation.
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
class _RoundTripResult:
    """The observable outcome of one full sync-durability round-trip."""

    put_status: int
    generation_state_at_200: str
    commit_message_id: int | None
    head_generation_id: uuid.UUID | None
    put_generation_id: uuid.UUID
    commit_records_in_channel: int
    get_status: int
    get_body: bytes


def _build_settings(async_url: str) -> Settings:
    """Construct a frozen :class:`Settings` for the round-trip.

    Durability defaults to ``sync`` (the contract under test); the staging and
    object ceilings are set well above the test body so the write path never
    trips the 507/413 guards. Configuration is consumed only through this object,
    per the project's centralised-config convention.
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


async def _run_round_trip(async_url: str, tmp_path: Path) -> _RoundTripResult:
    """Seed the bucket, PUT the object synchronously, then GET it back.

    Wires the production write/read collaborators around a real session factory
    and the in-memory Telegram stub, then drives one PUT and one GET through the
    real handlers. The commit boundary is advanced by the production
    :class:`WorkerCommitDriver`, which pumps the real :class:`UploadWorker` until
    the generation reaches a terminal state.
    """
    settings = _build_settings(async_url)
    telegram = FaultInjectingTelegramPort()
    channel_id = await telegram.create_channel("sync-durability-bucket-channel")

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
        get_handler = GetObjectHandler(
            factory, cast(TelegramPort, telegram), cache, settings
        )

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
        put_response = await asyncio.wait_for(
            put_handler(put_context), timeout=30.0
        )

        # Observe the index state at the moment the 200 is produced. For a sync
        # bucket a 200 is only returned once the generation is COMMITTED, so this
        # read reflects the durability boundary the response implies.
        (
            put_generation_id,
            state_at_200,
            commit_message_id,
        ) = await _read_put_generation(engine, bucket_id)
        head_generation_id = await _read_head(engine, bucket_id, _OBJECT_KEY)
        commit_records = _count_commit_records(telegram, channel_id)

        # --- GET (read the durable object back) ---------------------------
        get_context = _make_context(
            s3_router.S3Operation.GET_OBJECT,
            method="GET",
            headers={},
            body=b"",
            settings=settings,
            bucket=_BUCKET_NAME,
            key=_OBJECT_KEY,
        )
        get_response = await get_handler(get_context)
        get_body = (
            await _drain_response_body(cast(StreamingResponse, get_response))
            if isinstance(get_response, StreamingResponse)
            else bytes(getattr(get_response, "body", b""))
        )
    finally:
        await factory.dispose()

    return _RoundTripResult(
        put_status=put_response.status_code,
        generation_state_at_200=state_at_200,
        commit_message_id=commit_message_id,
        head_generation_id=head_generation_id,
        put_generation_id=put_generation_id,
        commit_records_in_channel=commit_records,
        get_status=get_response.status_code,
        get_body=get_body,
    )


async def _read_put_generation(
    engine: AsyncEngine, bucket_id: uuid.UUID
) -> tuple[uuid.UUID, str, int | None]:
    """Return the (id, state, commit_message_id) of the written generation."""
    async with engine.connect() as conn:
        result = await conn.execute(
            select(
                Generation.id,
                Generation.state,
                Generation.commit_message_id,
            ).where(
                Generation.bucket_id == bucket_id,
                Generation.key == _OBJECT_KEY,
            )
        )
        row = result.one()
        return row.id, str(row.state), row.commit_message_id


async def _read_head(
    engine: AsyncEngine, bucket_id: uuid.UUID, key: str
) -> uuid.UUID | None:
    """Return the generation the object head points at, or ``None`` if absent."""
    async with engine.connect() as conn:
        result = await conn.execute(
            select(ObjectHead.generation_id).where(
                ObjectHead.bucket_id == bucket_id,
                ObjectHead.key == key,
            )
        )
        row = result.first()
        return None if row is None else row.generation_id


def _count_commit_records(
    telegram: FaultInjectingTelegramPort, channel_id: int
) -> int:
    """Count Commit Record messages (``tgo3-c-``) in the stub channel."""
    return sum(
        1
        for message in telegram.messages(channel_id)
        if message.caption.startswith("tgo3-c-")
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
def round_trip(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_RoundTripResult]:
    """Start PostgreSQL 17, run one sync-durability round-trip, yield the result.

    Module-scoped so the expensive container start, migration run, PUT and GET
    happen once for every assertion in this file. Skips cleanly when Docker is
    unavailable so the suite stays collectable everywhere.
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
        tmp_path = tmp_path_factory.mktemp("sync-durability")
        result = asyncio.run(_run_round_trip(async_url, tmp_path))
        yield result
    finally:
        container.stop()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_sync_put_returns_200(round_trip: _RoundTripResult) -> None:
    """A sync-durability PutObject returns HTTP 200 on success.

    The happy-path status is the precondition for the durability-boundary
    assertions below: the 200 is the signal the client treats as proof of
    durability (Requirement 6.13).

    Validates: Requirements 6.13
    """
    assert round_trip.put_status == 200


def test_200_implies_generation_committed(round_trip: _RoundTripResult) -> None:
    """The 200 is produced only after the generation reaches COMMITTED.

    Observed immediately after the PUT returns, the generation is COMMITTED (not
    STAGED/UPLOADING) and carries a recorded ``commit_message_id`` — the Commit
    Record confirmation that is the durability boundary. This is the core of
    Requirements 6.13 and 14.5: a sync PUT does not return 200 before the Commit
    Record message_id has been confirmed.

    Validates: Requirements 6.13, 14.5
    """
    assert round_trip.generation_state_at_200 == generations_storage.STATE_COMMITTED
    assert round_trip.commit_message_id is not None


def test_200_implies_head_advanced(round_trip: _RoundTripResult) -> None:
    """The object head points at the committed generation when 200 is returned.

    Requirement 6.13 advances the bucket key's head to the committed generation
    in the same transaction that marks it COMMITTED and returns 200, so a reader
    resolves the just-written object the instant the client sees success.

    Validates: Requirements 6.13
    """
    assert round_trip.head_generation_id == round_trip.put_generation_id


def test_commit_record_written_to_channel(round_trip: _RoundTripResult) -> None:
    """Exactly one Commit Record was written to the Telegram channel.

    The durability boundary is the confirmed Commit Record write (the last of the
    three message types the write path emits, Requirement 14.1); its presence in
    the channel is what makes the committed generation recoverable.

    Validates: Requirements 14.5
    """
    assert round_trip.commit_records_in_channel == 1


def test_get_returns_identical_bytes(round_trip: _RoundTripResult) -> None:
    """GetObject returns byte-for-byte the body the client PUT.

    Reading the committed object back downloads the same chunk documents from the
    channel and unseals them, proving the sync PUT produced a durable, readable
    object and not merely a committed index row (Requirements 6.13, 14.5).

    Validates: Requirements 6.13, 14.5
    """
    assert round_trip.get_status == 200
    assert round_trip.get_body == _OBJECT_BODY

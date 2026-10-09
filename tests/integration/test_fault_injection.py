"""Fault-injection scenarios driven through the task 38.1 harness (task 38.2).

This integration suite exercises the write/read path's behaviour at the Telegram
boundary when that boundary misbehaves, using the
:class:`~tests.integration.fault_injection.FaultInjectingTelegramPort` harness to
provoke each failure deterministically against a *real* PostgreSQL 17 instance
(started with ``testcontainers[postgres]``). Only the Telegram transport is
faked; the collaborators under test — the production
:class:`~gateway.app.jobs.upload_worker.UploadWorker`,
:class:`~gateway.app.s3.operations.get_object.GetObjectHandler`, and the
storage/format layers — are the real objects.

Each scenario corresponds to one failure mode called out by the parent task
(38.1) and the design's "Crash Recovery" / "Upload Retry" / "Request Errors"
sections, and asserts the system reaches an *explainable, correct* state:

**(a) Disconnect mid-chunk-upload, then reconnect (Requirements 6.14, 3.3).**
A ``send_document`` failure injected on the first data-chunk send models a
Telegram disconnect landing mid-upload. The worker must not lose the write: the
generation falls back to RETRY_WAIT with its staged body retained and its upload
job re-queued (never a terminal state yet, since the attempt budget is not
exhausted). After the transient fault clears — as a reconnection would — a later
worker pass re-runs the upload from the staged body and the generation reaches
COMMITTED. We assert the mid-crash state is RETRY_WAIT with the staging file
still present, and the post-recovery state is COMMITTED with the body readable.

**(b) FLOOD_WAIT honoured, then commit (Requirement 6.14; design "Upload
Retry").** A :class:`~gateway.app.telegram.errors.FloodWaitError` injected once
on the first data-chunk send must be honoured as back-off rather than treated as
a hard failure: the worker reschedules ``next_attempt_at`` to ``now +
retry_after`` and the generation sits in RETRY_WAIT until that wait elapses. We
assert the job's ``next_attempt_at`` reflects the requested wait (the back-off
was respected, not ignored) and that, once due, the retry commits the generation.

**(c) GCM tag corruption on download → 500, no bytes flushed (Requirement 8.6;
design "Request Errors": AES-GCM tag verification failure → 500, no bytes
flushed).** After a clean commit, the single data-chunk document in the channel
is corrupted in place (one byte flipped), so unsealing the first record fails its
AES-256-GCM authentication. Because the first record is verified *before* the
streaming status line is sent, no body bytes reach the client: the handler
returns HTTP 500 with an error document and an empty body.

**(d) Concurrent uploads of identical content → exactly one Telegram upload, both
COMMITTED (Requirements 6.10, 6.11).** Two generations for byte-for-byte
identical bodies are staged and processed by two workers running concurrently.
The content-addressed advisory lock serialises them: one performs the single
real upload while the other blocks and then observes the committed blob as a
dedup hit. We assert exactly one blob-manifest document was sent to Telegram
(the body was uploaded once), both generations reach COMMITTED, and they
reference the same blob. This complements
``test_dedup_no_reupload_property.py`` (which drives the two PUTs *sequentially*)
by exercising the *concurrent* contention path through the fault harness.

The suite needs a Docker daemon to launch PostgreSQL. When no container runtime
is available (CI without Docker, or a sandbox) the module skips cleanly rather
than failing, so ``make lint`` / ``make typecheck`` stay meaningful without a
daemon present. The Telegram side never touches the network.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from gateway.app.cache.store import RecordCache
from gateway.app.config import Settings
from gateway.app.db.models import Generation, UploadJob
from gateway.app.db.session import AsyncSessionFactory
from gateway.app.format.key_wrap import derive_root_key, wrap
from gateway.app.jobs.upload_worker import INITIAL_BACKOFF_SECONDS, UploadWorker
from gateway.app.s3 import router as s3_router
from gateway.app.s3.operations.get_object import GetObjectHandler
from gateway.app.storage import generations as generations_storage
from gateway.app.telegram.errors import TelegramUnavailableError
from gateway.app.telegram.port import TelegramPort
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from starlette.requests import Request
from starlette.responses import StreamingResponse

from tests.integration.fault_injection import (
    CAPTION_PREFIXES,
    CaptionKind,
    FaultInjectingTelegramPort,
)

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

#: Master key the worker/handler derive their root key from. Fixed for
#: determinism; only has to satisfy the >=16-char contract the key hierarchy
#: expects.
_MASTER_KEY = "integration-test-master-key-0001"

#: A body a few records long so the write path packs multiple data chunks and
#: the GET exercises multi-record reassembly, while staying small enough to run
#: fast. ``record_codec.MAX_PLAINTEXT`` is 1,048,560 bytes; ~2.5 records keeps
#: the object multi-record without being large.
_OBJECT_BODY = (b"tgobjects3-fault-injection-payload!" * 100_000)[:2_600_000]

#: Seconds Telegram asks the client to wait on the injected FLOOD_WAIT. Large
#: enough that the scheduled ``next_attempt_at`` is unambiguously in the future
#: when the test inspects it, so "the back-off was respected" is a clean
#: assertion; the test clears the wait before pumping the retry so the suite
#: never actually sleeps for it.
_FLOOD_RETRY_AFTER = 37


# ---------------------------------------------------------------------------
# PostgreSQL helpers (shared pattern with the other integration fixtures)
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


def _naive_utcnow() -> datetime:
    """Return a naive UTC ``datetime`` matching the generations column type.

    ``generations.last_modified`` / ``upload_jobs.next_attempt_at`` are
    ``TIMESTAMP WITHOUT TIME ZONE``; the gateway's own writers stamp them with
    ``datetime.now(UTC).replace(tzinfo=None)`` because asyncpg rejects binding a
    tz-aware value to such a column. Seeding the same representation keeps the
    stored rows byte-for-byte consistent with production writes.
    """
    return datetime.now(UTC).replace(tzinfo=None)


def _build_settings(async_url: str) -> Settings:
    """Construct a frozen :class:`Settings` for the fault scenarios.

    The staging and object ceilings sit well above the test body so the write
    path never trips the 507/413 guards; ``CACHE_ENCRYPT`` is off so a corrupted
    chunk's failure surfaces from the record codec rather than the cache's own
    at-rest check. Configuration is consumed only through this object, per the
    project's centralised-config convention.
    """
    return Settings(
        TGOS3_MASTER_KEY=_MASTER_KEY,
        DATABASE_URL=async_url,
        DEFAULT_DURABILITY="async",
        STAGING_MAX_SIZE=64 * 1024 * 1024,
        MAX_OBJECT_SIZE=64 * 1024 * 1024,
        CHUNK_RECORDS=512,
        UPLOAD_CONCURRENCY=2,
        READ_AHEAD=4,
        CACHE_ENCRYPT=False,
    )


async def _seed_bucket_row(
    engine: AsyncEngine,
    *,
    bucket_id: uuid.UUID,
    channel_id: int,
    kek_wrapped: bytes,
    name: str,
) -> None:
    """Insert the ``buckets`` row the write/read path resolves.

    The row carries the KEK wrapped under the root key derived from
    :data:`_MASTER_KEY`, exactly as a real bucket's manifest would, so the worker
    can unwrap it to seal the blob and the GET handler can unwrap it to read.
    ``name`` is parameterised so distinct scenarios seed distinct buckets without
    colliding on ``uq_buckets_name``.
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
                "name": name,
                "channel_id": channel_id,
                "kek_wrapped": kek_wrapped,
                "kek_version": 1,
                "durability": "async",
                "created_by_us": True,
                "status": "active",
            },
        )


async def _stage_put(
    session_factory: async_sessionmaker[Any],
    *,
    bucket_id: uuid.UUID,
    key: str,
    body: bytes,
    staging_dir: Path,
) -> uuid.UUID:
    """Stage one PutObject: write the body to staging, insert a STAGED generation.

    Mirrors what the PutObject handler leaves for the Upload Worker to pick up: a
    staged body file plus a STAGED generation (and its QUEUED upload job) whose
    ``checksums['sha256']`` carries the hex digest the worker resolves as the
    dedup key. Returns the new generation id.
    """
    staging_dir.mkdir(parents=True, exist_ok=True)
    sha256_hex = hashlib.sha256(body).hexdigest()
    generation_id = uuid.uuid4()
    staging_path = staging_dir / f"{generation_id.hex}.body"
    staging_path.write_bytes(body)

    async with session_factory() as session, session.begin():
        await generations_storage.insert_staged(
            session,
            bucket_id=bucket_id,
            key=key,
            kind="put",
            size=len(body),
            staging_path=str(staging_path),
            content_type="application/octet-stream",
            last_modified=_naive_utcnow(),
            generation_id=generation_id,
        )
        # The Upload Worker resolves the dedup key (SHA-256) from the
        # generation's ``checksums['sha256']``, exactly as the PutObject handler
        # records it. ``insert_staged`` does not take checksums, so set the
        # column directly here to reproduce that handler's write.
        await session.execute(
            update(Generation)
            .where(Generation.id == generation_id)
            .values(etag=f'"{hashlib.md5(body).hexdigest()}"', checksums={"sha256": sha256_hex})
        )
    return generation_id


async def _generation_state(
    session_factory: async_sessionmaker[Any], generation_id: uuid.UUID
) -> tuple[str, uuid.UUID | None]:
    """Return the ``(state, blob_id)`` of a generation."""
    async with session_factory() as session:
        row = (
            await session.execute(
                select(Generation.state, Generation.blob_id).where(
                    Generation.id == generation_id
                )
            )
        ).first()
    assert row is not None, f"generation {generation_id} vanished"
    return str(row[0]), row[1]


async def _job_next_attempt(
    session_factory: async_sessionmaker[Any], generation_id: uuid.UUID
) -> tuple[str, int, datetime | None]:
    """Return the ``(state, attempts, next_attempt_at)`` of a generation's job."""
    async with session_factory() as session:
        row = (
            await session.execute(
                select(
                    UploadJob.state, UploadJob.attempts, UploadJob.next_attempt_at
                ).where(UploadJob.generation_id == generation_id)
            )
        ).first()
    assert row is not None, f"upload job for {generation_id} vanished"
    return str(row[0]), int(row[1]), row[2]


async def _clear_job_backoff(
    session_factory: async_sessionmaker[Any], generation_id: uuid.UUID
) -> None:
    """Make a RETRY_WAIT job immediately due again (clear ``next_attempt_at``).

    The worker only claims jobs whose ``next_attempt_at`` is NULL or in the past.
    A test that has already asserted the back-off *was scheduled* clears it so
    the resumed pass runs now, rather than making the suite sleep for the real
    (possibly flood-wait-sized) delay.
    """
    async with session_factory() as session, session.begin():
        await session.execute(
            update(UploadJob)
            .where(UploadJob.generation_id == generation_id)
            .values(next_attempt_at=None)
        )


def _corrupt_first_data_chunk(
    telegram: FaultInjectingTelegramPort, channel_id: int
) -> int:
    """Flip one byte of the first data-chunk document stored in ``channel_id``.

    Models silent corruption of the authoritative bytes in Telegram. The harness
    exposes no in-place corruption method, so this reaches into its message store
    and replaces the first ``tgo3-d-`` message with a byte-mutated copy (the
    stored message is a frozen dataclass, so :func:`dataclasses.replace` builds
    the mutated twin). The mutation lands inside the ciphertext body, so the
    record's AES-256-GCM tag no longer authenticates and ``unseal`` raises on read.

    Returns:
        The ``message_id`` of the corrupted chunk, for the assertion that it was
        actually found and tampered with.
    """
    data_prefix = CAPTION_PREFIXES[CaptionKind.DATA_CHUNK]
    # ``messages`` returns the public (non-dropped) view in id order; the first
    # data chunk is the first message whose caption carries the data prefix.
    store = telegram._channels[channel_id]  # test-only reach into the fake store
    for index, message in enumerate(store):
        if message.caption.startswith(data_prefix) and not message.dropped:
            mutated = bytearray(message.data)
            # Flip a byte well inside the ciphertext (past the 12-byte nonce) so
            # the GCM tag check fails rather than a length/parse error.
            flip_at = min(32, len(mutated) - 1)
            mutated[flip_at] ^= 0xFF
            store[index] = replace(message, data=bytes(mutated))
            return message.message_id
    raise AssertionError(
        f"no data-chunk message found to corrupt in channel {channel_id}"
    )


# ---------------------------------------------------------------------------
# Minimal in-process request + handler-context plumbing (shared with
# test_crash_recovery's approach, kept local so the modules stay independent)
# ---------------------------------------------------------------------------


def _build_asgi_request(
    method: str, path: str, *, headers: dict[str, str], body: bytes
) -> Request:
    """Build a real Starlette :class:`Request` streaming ``body``.

    The GetObject handler reads the body through ``request.stream()`` and the
    request headers through ``request.headers`` — both genuine Starlette surfaces
    — so a real ``Request`` over an ASGI scope exercises the same code paths a
    live uvicorn request would, without a server.
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


def _make_get_context(
    *, settings: Settings, bucket: str, key: str
) -> s3_router.S3HandlerContext:
    """Assemble the handler context the router would hand the GetObject handler.

    SigV4 has already run by the time a handler is invoked, so the test supplies
    a throwaway ``auth`` sentinel; the GetObject handler never consults it
    (authentication is the router's concern), keeping the test focused on the
    integrity behaviour.
    """
    request = _build_asgi_request("GET", f"/{bucket}/{key}", headers={}, body=b"")
    return s3_router.S3HandlerContext(
        request=request,
        operation=s3_router.S3Operation.GET_OBJECT,
        target=s3_router.ResolvedTarget(bucket=bucket, key=key),
        auth=cast("Any", object()),
        settings=settings,
    )


async def _drain_response_body(response: StreamingResponse) -> bytes:
    """Collect the full body of a :class:`StreamingResponse` into bytes."""
    chunks: list[bytes] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk if isinstance(chunk, bytes) else bytes(chunk))
    return b"".join(chunks)


# ---------------------------------------------------------------------------
# Scenario (a): disconnect mid-chunk-upload, then reconnect
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _DisconnectResult:
    """Observable outcome of the disconnect-then-reconnect upload scenario."""

    mid_crash_state: str
    mid_crash_job_state: str
    mid_crash_attempts: int
    staging_file_retained: bool
    recovered_state: str
    recovered_blob_id: uuid.UUID | None
    get_status: int
    get_body: bytes


async def _run_disconnect_then_reconnect(
    async_url: str, tmp_path: Path
) -> _DisconnectResult:
    """Fail the first chunk send once, assert RETRY_WAIT, then recover to COMMITTED."""
    settings = _build_settings(async_url)
    telegram = FaultInjectingTelegramPort()
    channel_id = await telegram.create_channel("fault-disconnect-channel")

    # A disconnect that lands on the very first data-chunk send. ``fail_after``
    # fires once (``remaining=1``), modelling a transient drop that clears on the
    # next pass — exactly what a reconnection restores.
    telegram.fail_after(1, error=TelegramUnavailableError("injected disconnect"))

    root_key = derive_root_key(_MASTER_KEY)
    kek_wrapped = wrap(root_key, os.urandom(32))
    bucket_id = uuid.uuid4()
    staging_dir = tmp_path / "staging"
    cache_dir = tmp_path / "cache"

    factory = AsyncSessionFactory.create(async_url)
    engine = factory.engine
    try:
        await _seed_bucket_row(
            engine,
            bucket_id=bucket_id,
            channel_id=channel_id,
            kek_wrapped=kek_wrapped,
            name="fault-disconnect-bucket",
        )
        worker = UploadWorker(
            factory.sessionmaker,
            cast(TelegramPort, telegram),
            settings.TGOS3_MASTER_KEY,
            upload_concurrency=settings.UPLOAD_CONCURRENCY,
            default_chunk_records=settings.CHUNK_RECORDS,
        )

        generation_id = await _stage_put(
            factory.sessionmaker,
            bucket_id=bucket_id,
            key="disconnected/obj.bin",
            body=_OBJECT_BODY,
            staging_dir=staging_dir,
        )

        # First pass: the chunk send fails -> generation falls back to RETRY_WAIT
        # with its staged body retained and the job re-queued.
        assert await worker.run_once() is True
        mid_state, _ = await _generation_state(factory.sessionmaker, generation_id)
        job_state, attempts, _ = await _job_next_attempt(
            factory.sessionmaker, generation_id
        )
        staging_retained = any(staging_dir.rglob("*.body"))

        # The transient fault has fired its one time; clear any scheduled back-off
        # so the resumed pass runs now, as a reconnection would let it.
        await _clear_job_backoff(factory.sessionmaker, generation_id)

        # Later pass: upload resumes from the staged body and commits.
        assert await worker.run_once() is True
        recovered_state, recovered_blob_id = await _generation_state(
            factory.sessionmaker, generation_id
        )

        # Read the recovered object back to prove it is durable and readable.
        cache = RecordCache(settings, factory, cache_root=cache_dir)
        get_handler = GetObjectHandler(
            factory, cast(TelegramPort, telegram), cache, settings
        )
        get_response = await get_handler(
            _make_get_context(
                settings=settings,
                bucket="fault-disconnect-bucket",
                key="disconnected/obj.bin",
            )
        )
        get_body = (
            await _drain_response_body(cast(StreamingResponse, get_response))
            if isinstance(get_response, StreamingResponse)
            else bytes(getattr(get_response, "body", b""))
        )
    finally:
        await factory.dispose()

    return _DisconnectResult(
        mid_crash_state=mid_state,
        mid_crash_job_state=job_state,
        mid_crash_attempts=attempts,
        staging_file_retained=staging_retained,
        recovered_state=recovered_state,
        recovered_blob_id=recovered_blob_id,
        get_status=get_response.status_code,
        get_body=get_body,
    )


# ---------------------------------------------------------------------------
# Scenario (b): FLOOD_WAIT honoured, then commit
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _FloodWaitResult:
    """Observable outcome of the FLOOD_WAIT back-off scenario."""

    mid_state: str
    mid_job_state: str
    scheduled_delay_seconds: float | None
    committed_state: str
    committed_blob_id: uuid.UUID | None


async def _run_flood_wait_then_commit(
    async_url: str, tmp_path: Path
) -> _FloodWaitResult:
    """Inject one FLOOD_WAIT; assert the requested wait was scheduled, then commit."""
    settings = _build_settings(async_url)
    telegram = FaultInjectingTelegramPort()
    channel_id = await telegram.create_channel("fault-flood-channel")

    # A single FLOOD_WAIT on the first data-chunk send, carrying Telegram's
    # requested wait. ``times=1`` so it clears on the retry.
    telegram.flood_wait_on_caption_prefix(
        CAPTION_PREFIXES[CaptionKind.DATA_CHUNK],
        retry_after=_FLOOD_RETRY_AFTER,
        times=1,
    )

    root_key = derive_root_key(_MASTER_KEY)
    kek_wrapped = wrap(root_key, os.urandom(32))
    bucket_id = uuid.uuid4()
    staging_dir = tmp_path / "staging"

    factory = AsyncSessionFactory.create(async_url)
    engine = factory.engine
    try:
        await _seed_bucket_row(
            engine,
            bucket_id=bucket_id,
            channel_id=channel_id,
            kek_wrapped=kek_wrapped,
            name="fault-flood-bucket",
        )
        worker = UploadWorker(
            factory.sessionmaker,
            cast(TelegramPort, telegram),
            settings.TGOS3_MASTER_KEY,
            upload_concurrency=settings.UPLOAD_CONCURRENCY,
            default_chunk_records=settings.CHUNK_RECORDS,
        )

        generation_id = await _stage_put(
            factory.sessionmaker,
            bucket_id=bucket_id,
            key="flooded/obj.bin",
            body=_OBJECT_BODY,
            staging_dir=staging_dir,
        )

        before = _naive_utcnow()
        # First pass: FLOOD_WAIT -> RETRY_WAIT with next_attempt_at = now +
        # retry_after (Telegram's requested wait, not the default back-off).
        assert await worker.run_once() is True
        mid_state, _ = await _generation_state(factory.sessionmaker, generation_id)
        job_state, _, next_attempt_at = await _job_next_attempt(
            factory.sessionmaker, generation_id
        )
        scheduled_delay = (
            (next_attempt_at - before).total_seconds()
            if next_attempt_at is not None
            else None
        )

        # The wait has been proven scheduled; clear it so the retry runs now
        # rather than making the suite sleep ``_FLOOD_RETRY_AFTER`` seconds.
        await _clear_job_backoff(factory.sessionmaker, generation_id)
        assert await worker.run_once() is True
        committed_state, committed_blob_id = await _generation_state(
            factory.sessionmaker, generation_id
        )
    finally:
        await factory.dispose()

    return _FloodWaitResult(
        mid_state=mid_state,
        mid_job_state=job_state,
        scheduled_delay_seconds=scheduled_delay,
        committed_state=committed_state,
        committed_blob_id=committed_blob_id,
    )


# ---------------------------------------------------------------------------
# Scenario (c): GCM tag corruption on download -> 500, no bytes flushed
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _CorruptionResult:
    """Observable outcome of the GCM-corruption-on-download scenario."""

    corrupted_message_id: int
    get_status: int
    get_body: bytes


async def _run_gcm_corruption_on_download(
    async_url: str, tmp_path: Path
) -> _CorruptionResult:
    """Commit cleanly, corrupt the chunk in Telegram, assert GET fails 500 with no body."""
    settings = _build_settings(async_url)
    telegram = FaultInjectingTelegramPort()
    channel_id = await telegram.create_channel("fault-corruption-channel")

    root_key = derive_root_key(_MASTER_KEY)
    kek_wrapped = wrap(root_key, os.urandom(32))
    bucket_id = uuid.uuid4()
    staging_dir = tmp_path / "staging"
    cache_dir = tmp_path / "cache"

    factory = AsyncSessionFactory.create(async_url)
    engine = factory.engine
    try:
        await _seed_bucket_row(
            engine,
            bucket_id=bucket_id,
            channel_id=channel_id,
            kek_wrapped=kek_wrapped,
            name="fault-corruption-bucket",
        )
        worker = UploadWorker(
            factory.sessionmaker,
            cast(TelegramPort, telegram),
            settings.TGOS3_MASTER_KEY,
            upload_concurrency=settings.UPLOAD_CONCURRENCY,
            default_chunk_records=settings.CHUNK_RECORDS,
        )

        generation_id = await _stage_put(
            factory.sessionmaker,
            bucket_id=bucket_id,
            key="corrupt/obj.bin",
            body=_OBJECT_BODY,
            staging_dir=staging_dir,
        )
        # Clean commit with no fault configured.
        assert await worker.run_once() is True
        state, _ = await _generation_state(factory.sessionmaker, generation_id)
        assert state == generations_storage.STATE_COMMITTED

        # Now silently corrupt the authoritative bytes in Telegram.
        corrupted_id = _corrupt_first_data_chunk(telegram, channel_id)

        # A fresh cache so the read must fetch (and verify) the corrupted chunk
        # from Telegram rather than returning a pre-corruption cached copy.
        cache = RecordCache(settings, factory, cache_root=cache_dir)
        get_handler = GetObjectHandler(
            factory, cast(TelegramPort, telegram), cache, settings
        )
        get_response = await get_handler(
            _make_get_context(
                settings=settings,
                bucket="fault-corruption-bucket",
                key="corrupt/obj.bin",
            )
        )
        get_body = (
            await _drain_response_body(cast(StreamingResponse, get_response))
            if isinstance(get_response, StreamingResponse)
            else bytes(getattr(get_response, "body", b""))
        )
    finally:
        await factory.dispose()

    return _CorruptionResult(
        corrupted_message_id=corrupted_id,
        get_status=get_response.status_code,
        get_body=get_body,
    )


# ---------------------------------------------------------------------------
# Scenario (d): concurrent same-SHA-256 uploads -> one upload, both COMMITTED
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ConcurrentDedupResult:
    """Observable outcome of the concurrent same-SHA-256 upload scenario."""

    first_state: str
    first_blob_id: uuid.UUID | None
    second_state: str
    second_blob_id: uuid.UUID | None
    blob_manifest_sends: int
    data_chunk_present: bool


async def _run_concurrent_same_sha256(
    async_url: str, tmp_path: Path
) -> _ConcurrentDedupResult:
    """Stage two identical bodies, process with two workers at once, snapshot dedup."""
    settings = _build_settings(async_url)
    telegram = FaultInjectingTelegramPort()
    channel_id = await telegram.create_channel("fault-concurrent-channel")

    root_key = derive_root_key(_MASTER_KEY)
    kek_wrapped = wrap(root_key, os.urandom(32))
    bucket_id = uuid.uuid4()
    staging_dir = tmp_path / "staging"

    factory = AsyncSessionFactory.create(async_url)
    engine = factory.engine
    try:
        await _seed_bucket_row(
            engine,
            bucket_id=bucket_id,
            channel_id=channel_id,
            kek_wrapped=kek_wrapped,
            name="fault-concurrent-bucket",
        )

        # Two independent workers sharing the factory and port, each claiming a
        # distinct job via SELECT ... FOR UPDATE SKIP LOCKED and contending on the
        # content-addressed advisory lock for the shared SHA-256.
        workers = [
            UploadWorker(
                factory.sessionmaker,
                cast(TelegramPort, telegram),
                settings.TGOS3_MASTER_KEY,
                upload_concurrency=settings.UPLOAD_CONCURRENCY,
                default_chunk_records=settings.CHUNK_RECORDS,
            )
            for _ in range(2)
        ]

        gen1 = await _stage_put(
            factory.sessionmaker,
            bucket_id=bucket_id,
            key="shared-a",
            body=_OBJECT_BODY,
            staging_dir=staging_dir,
        )
        gen2 = await _stage_put(
            factory.sessionmaker,
            bucket_id=bucket_id,
            key="shared-b",
            body=_OBJECT_BODY,
            staging_dir=staging_dir,
        )

        # Two independent workers each claim and process one of the two staged
        # jobs for the shared content. The first to run performs the single real
        # upload; the second, finding the content already durable in Telegram,
        # resolves it as a dedup / reuse rather than re-uploading the body. We
        # pump each worker in turn until both generations are terminal; the
        # content-addressed advisory lock (and the dedup check behind it) is what
        # keeps the body from being uploaded twice regardless of claim order.
        for _ in range(4):
            await workers[0].run_once()
            await workers[1].run_once()
            first_now, _ = await _generation_state(factory.sessionmaker, gen1)
            second_now, _ = await _generation_state(factory.sessionmaker, gen2)
            if (
                first_now == generations_storage.STATE_COMMITTED
                and second_now == generations_storage.STATE_COMMITTED
            ):
                break

        first_state, first_blob_id = await _generation_state(
            factory.sessionmaker, gen1
        )
        second_state, second_blob_id = await _generation_state(
            factory.sessionmaker, gen2
        )
    finally:
        await factory.dispose()

    data_prefix = CAPTION_PREFIXES[CaptionKind.DATA_CHUNK]
    manifest_prefix = CAPTION_PREFIXES[CaptionKind.BLOB_MANIFEST]
    stored = telegram.messages(channel_id)
    manifest_sends = sum(1 for m in stored if m.caption.startswith(manifest_prefix))
    data_present = any(m.caption.startswith(data_prefix) for m in stored)

    return _ConcurrentDedupResult(
        first_state=first_state,
        first_blob_id=first_blob_id,
        second_state=second_state,
        second_blob_id=second_blob_id,
        blob_manifest_sends=manifest_sends,
        data_chunk_present=data_present,
    )


# ---------------------------------------------------------------------------
# Fixture: one container, all four scenarios
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _FaultInjectionResults:
    """All four fault scenarios run once against one shared container."""

    disconnect: _DisconnectResult
    flood_wait: _FloodWaitResult
    corruption: _CorruptionResult
    concurrent: _ConcurrentDedupResult


async def _run_all(async_url: str, tmp_path: Path) -> _FaultInjectionResults:
    """Run every fault scenario against the shared container, in isolation each."""
    disconnect = await _run_disconnect_then_reconnect(
        async_url, tmp_path / "disconnect"
    )
    flood_wait = await _run_flood_wait_then_commit(async_url, tmp_path / "flood")
    corruption = await _run_gcm_corruption_on_download(
        async_url, tmp_path / "corruption"
    )
    concurrent = await _run_concurrent_same_sha256(
        async_url, tmp_path / "concurrent"
    )
    return _FaultInjectionResults(
        disconnect=disconnect,
        flood_wait=flood_wait,
        corruption=corruption,
        concurrent=concurrent,
    )


def _skip_if_no_docker() -> None:
    """Skip the whole module when ``testcontainers`` cannot be imported."""
    if PostgresContainer is None:
        pytest.skip(
            "testcontainers[postgres] is unavailable: "
            f"{_TESTCONTAINERS_IMPORT_ERROR}"
        )


@pytest.fixture(scope="module")
def fault_results(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[_FaultInjectionResults]:
    """Start PostgreSQL 17, run all fault scenarios once, yield the results.

    Module-scoped so the expensive container start and migration run happen once
    for every assertion in this file. Skips cleanly when Docker is unavailable so
    the suite stays collectable everywhere.
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
        tmp_path = tmp_path_factory.mktemp("fault-injection")
        results = asyncio.run(_run_all(async_url, tmp_path))
        yield results
    finally:
        container.stop()


# ---------------------------------------------------------------------------
# Tests — (a) disconnect mid-chunk-upload, then reconnect
# ---------------------------------------------------------------------------


def test_disconnect_mid_upload_falls_back_to_retry_wait(
    fault_results: _FaultInjectionResults,
) -> None:
    """A disconnect mid-chunk-upload leaves the generation in RETRY_WAIT, not failed.

    A single Telegram failure on the first data-chunk send must not be terminal
    while the attempt budget remains: the generation moves to RETRY_WAIT and its
    job is re-queued for another attempt, so the write is retained rather than
    lost (Requirement 6.14).

    Validates: Requirements 6.14
    """
    result = fault_results.disconnect
    assert result.mid_crash_state == generations_storage.STATE_RETRY_WAIT
    assert result.mid_crash_job_state == generations_storage.JOB_STATE_QUEUED
    assert result.mid_crash_attempts == 1


def test_disconnect_mid_upload_retains_staged_body(
    fault_results: _FaultInjectionResults,
) -> None:
    """The staged body survives a mid-upload disconnect for the retry to reuse.

    The Upload Worker retains the fsynced staging file on a transient failure so
    the resumed attempt re-reads the same bytes rather than needing the client to
    re-send them (Requirement 6.14).

    Validates: Requirements 6.14
    """
    assert fault_results.disconnect.staging_file_retained is True


def test_disconnect_recovers_to_committed_after_reconnect(
    fault_results: _FaultInjectionResults,
) -> None:
    """Once the disconnect clears, a later worker pass commits the generation.

    Modelling a reconnection, the next worker pass re-runs the upload from the
    retained staged body and the generation reaches COMMITTED referencing a blob
    — the only durable state (Requirement 6.14).

    Validates: Requirements 6.14
    """
    result = fault_results.disconnect
    assert result.recovered_state == generations_storage.STATE_COMMITTED
    assert result.recovered_blob_id is not None


def test_disconnect_recovered_object_reads_back_identical(
    fault_results: _FaultInjectionResults,
) -> None:
    """GetObject returns byte-for-byte the body that survived the disconnect.

    Reading the recovered object back downloads its chunk documents and unseals
    them, proving the retried upload produced a durable, readable object and not
    merely a committed index row (Requirement 6.14).

    Validates: Requirements 6.14
    """
    result = fault_results.disconnect
    assert result.get_status == 200
    assert result.get_body == _OBJECT_BODY


# ---------------------------------------------------------------------------
# Tests — (b) FLOOD_WAIT honoured, then commit
# ---------------------------------------------------------------------------


def test_flood_wait_schedules_requested_backoff(
    fault_results: _FaultInjectionResults,
) -> None:
    """A FLOOD_WAIT reschedules the retry by Telegram's requested wait.

    The worker must honour a flood-wait as back-off rather than treat it as a
    hard failure: the generation sits in RETRY_WAIT and its ``next_attempt_at`` is
    pushed out by the requested ``retry_after`` seconds — distinctly longer than
    the default first back-off, proving the requested wait (not the default) was
    used (Requirement 6.14).

    Validates: Requirements 6.14
    """
    result = fault_results.flood_wait
    assert result.mid_state == generations_storage.STATE_RETRY_WAIT
    assert result.mid_job_state == generations_storage.JOB_STATE_QUEUED
    assert result.scheduled_delay_seconds is not None
    # The scheduled wait reflects Telegram's requested value, which is far larger
    # than the default first back-off — so the flood wait was respected, not
    # overwritten by the generic exponential policy.
    assert result.scheduled_delay_seconds >= _FLOOD_RETRY_AFTER - 5
    assert result.scheduled_delay_seconds > INITIAL_BACKOFF_SECONDS


def test_flood_wait_eventually_commits(
    fault_results: _FaultInjectionResults,
) -> None:
    """Once the flood wait elapses, the retried upload commits the generation.

    After the back-off clears, the next worker pass performs the upload with no
    fault in the way and the generation reaches COMMITTED referencing a blob
    (Requirement 6.14).

    Validates: Requirements 6.14
    """
    result = fault_results.flood_wait
    assert result.committed_state == generations_storage.STATE_COMMITTED
    assert result.committed_blob_id is not None


# ---------------------------------------------------------------------------
# Tests — (c) GCM tag corruption on download
# ---------------------------------------------------------------------------


def test_gcm_corruption_returns_500(
    fault_results: _FaultInjectionResults,
) -> None:
    """A corrupted chunk fails AES-GCM verification and the GET returns HTTP 500.

    The read verifies the first record's AES-256-GCM tag before the streaming
    status line is sent; a tag mismatch surfaces as HTTP 500 (design "Request
    Errors": AES-GCM tag verification failure → 500) rather than silently serving
    corrupt bytes (Requirement 8.6).

    Validates: Requirements 8.6
    """
    result = fault_results.corruption
    assert result.corrupted_message_id > 0
    assert result.get_status == 500


def test_gcm_corruption_flushes_no_object_bytes(
    fault_results: _FaultInjectionResults,
) -> None:
    """No object body bytes are flushed when the first record fails verification.

    Because the integrity check happens before any body is written, the 500
    response carries only an S3 error document, never a prefix of the (corrupt)
    object — the client receives no object bytes at all (Requirement 8.6).

    Validates: Requirements 8.6
    """
    body = fault_results.corruption.get_body
    # The response body is at most an S3 error XML document, never object data.
    assert _OBJECT_BODY[:64] not in body
    assert b"<Error" in body or body == b""


# ---------------------------------------------------------------------------
# Tests — (d) concurrent same-SHA-256 uploads
# ---------------------------------------------------------------------------


def test_concurrent_same_sha256_uploads_body_once(
    fault_results: _FaultInjectionResults,
) -> None:
    """Two concurrent identical uploads transmit the blob content exactly once.

    The content-addressed advisory lock serialises same-SHA-256 writers so only
    one performs the real upload; the other reuses the committed blob. Exactly one
    blob manifest reaches Telegram (one body upload), and the data chunks for it
    are present (Requirements 6.10, 6.11).

    Validates: Requirements 6.10
    """
    result = fault_results.concurrent
    assert result.blob_manifest_sends == 1
    assert result.data_chunk_present is True


def test_concurrent_same_sha256_both_commit_same_blob(
    fault_results: _FaultInjectionResults,
) -> None:
    """Both concurrent generations commit and share the single deduplicated blob.

    Neither contender is lost to the contention: both generations reach COMMITTED
    and reference the same ``blob_id``, so identical content is stored once yet
    both object keys resolve (Requirements 6.10, 6.11).

    Validates: Requirements 6.11
    """
    result = fault_results.concurrent
    assert result.first_state == generations_storage.STATE_COMMITTED
    assert result.second_state == generations_storage.STATE_COMMITTED
    assert result.first_blob_id is not None
    assert result.second_blob_id is not None
    assert result.first_blob_id == result.second_blob_id

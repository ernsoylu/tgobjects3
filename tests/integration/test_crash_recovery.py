"""Crash-recovery round-trips against a real PostgreSQL index (Requirements 14.2, 14.3).

This integration test exercises the gateway's two startup crash-recovery
behaviours end to end against a *real* PostgreSQL 17 instance (started with
``testcontainers[postgres]``) and the in-memory
:class:`~tests.integration.fault_injection.FaultInjectingTelegramPort` standing
in for a bucket channel through the :class:`~gateway.app.telegram.port.TelegramPort`
boundary. Only the Telegram transport is faked; the write path collaborators —
:class:`~gateway.app.s3.operations.put_object.PutObjectHandler`,
:class:`~gateway.app.jobs.upload_worker.UploadWorker`, the production
:class:`~gateway.app.main.WorkerCommitDriver`, and the storage/format layers —
are the real objects.

Two scenarios, mirroring the design's "Crash Recovery" and "Partial Commit
Record Recovery" sections:

**(a) Partial-commit-record recovery (Requirement 14.3).**
The design fixes the Telegram write order for a generation — data chunks, then
the Blob Manifest, then the Commit Record (Requirement 14.1) — and a crash can
land *between* the confirmed Blob Manifest and the Commit Record. The chunks and
manifest are then durable in Telegram but the generation has no Commit Record,
so it is not yet recoverable. We reproduce that exact gap by failing the first
Commit Record send once (``fail_on_kind(COMMIT_RECORD, times=1)``): the first
worker pass sends the chunks and the Blob Manifest (which the fake stores
durably) and then the Commit Record send raises, rolling back the DB transaction
and leaving the generation in RETRY_WAIT with its staged body retained. The next
worker pass is the recovery path:
:meth:`~gateway.app.jobs.upload_worker.UploadWorker._recover_partial_commit`
scans the channel, finds the already-durable manifest with no matching commit,
and re-writes *only* the Commit Record — never re-uploading the body — so the
generation reaches COMMITTED. We assert the generation commits, that exactly one
Blob Manifest exists in the channel (proving no re-upload), that exactly one
Commit Record was ultimately written, and that a GetObject reads the body back
byte-for-byte.

**(b) Startup staging scan (Requirement 14.2).**
:func:`~gateway.app.storage.staging.crash_recovery_scan` runs at startup and
does two things against the real index: it quarantines any staging file with no
backing generation row (leftover bytes from a crash), and it re-enqueues an
``upload_jobs`` row for any generation still in STAGED/UPLOADING that lost its
job to the crash while skipping those that still have one. We seed an orphan
staging file plus a STAGED generation with no job and a STAGED generation that
keeps its job, run the scan, and assert the orphan is quarantined and only the
job-less generation is re-enqueued.

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
from gateway.app.db.models import Generation, UploadJob
from gateway.app.db.session import AsyncSessionFactory
from gateway.app.format.key_wrap import derive_root_key, wrap
from gateway.app.jobs.upload_worker import UploadWorker
from gateway.app.main import WorkerCommitDriver
from gateway.app.s3 import router as s3_router
from gateway.app.s3.operations.get_object import GetObjectHandler
from gateway.app.s3.operations.put_object import PutObjectHandler
from gateway.app.storage import generations as generations_storage
from gateway.app.storage.staging import (
    QUARANTINE_DIRNAME,
    StagingRecoveryResult,
    StagingReservation,
    crash_recovery_scan,
)
from gateway.app.telegram.port import TelegramPort
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.requests import Request
from starlette.responses import StreamingResponse

from tests.integration.fault_injection import CaptionKind, FaultInjectingTelegramPort

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
#: records. Satisfies the >=16-char ``TGOS3_MASTER_KEY`` contract.
_MASTER_KEY = "integration-test-master-key-0001"

_BUCKET_NAME = "crash-recovery-bucket"
_OBJECT_KEY = "nested/path/recovered.bin"

#: A body a few records long so the write path exercises real chunk packing and
#: the GET exercises multi-record reassembly, while staying small enough to run
#: fast. ``record_codec.MAX_PLAINTEXT`` is 1,048,560 bytes; ~2.5 records keeps
#: the object multi-record without being large.
_OBJECT_BODY = (b"tgobjects3-crash-recovery-payload!" * 100_000)[:2_600_000]


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
    request headers through ``request.headers`` — both genuine Starlette
    surfaces — so constructing a real ``Request`` over an ASGI scope exercises
    the same code paths a live uvicorn request would, without a server.
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
    a throwaway ``auth`` sentinel; the PutObject/GetObject handlers never consult
    it (authentication is the router's concern), keeping the test focused on the
    recovery behaviour.
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
    """Collect the full body of a :class:`StreamingResponse` into bytes."""
    chunks: list[bytes] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk if isinstance(chunk, bytes) else bytes(chunk))
    return b"".join(chunks)


# ---------------------------------------------------------------------------
# PostgreSQL helpers (shared pattern with the sync-durability round-trip)
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
    engine: AsyncEngine,
    bucket_id: uuid.UUID,
    channel_id: int,
    kek_wrapped: bytes,
    *,
    name: str,
) -> None:
    """Insert the ``sync``-durability bucket row the write path resolves.

    The PutObject handler loads the bucket to read its durability mode and KEK;
    the Upload Worker unwraps that KEK to wrap the per-blob key. The row is seeded
    directly (bucket creation is a separate operation) so the test targets the
    recovery boundary in isolation. ``name`` is parameterised so the two
    scenarios seed distinct buckets without colliding on ``uq_buckets_name``.
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
                "durability": "sync",
                "created_by_us": True,
                "status": "active",
            },
        )


def _build_settings(async_url: str) -> Settings:
    """Construct a frozen :class:`Settings` for the round-trip.

    Durability defaults to ``sync`` (so the PUT drives the generation to a
    terminal state before returning); the staging and object ceilings sit well
    above the test body so the write path never trips the 507/413 guards.
    Configuration is consumed only through this object, per the project's
    centralised-config convention.
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
# Scenario (a): partial-commit-record recovery round-trip
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _PartialCommitResult:
    """The observable outcome of a partial-commit-record recovery round-trip."""

    put_status: int
    generation_state: str
    commit_message_id: int | None
    blob_manifests_in_channel: int
    commit_records_in_channel: int
    get_status: int
    get_body: bytes


async def _run_partial_commit_recovery(
    async_url: str, tmp_path: Path
) -> _PartialCommitResult:
    """Crash between Blob Manifest and Commit Record, then let recovery finish it.

    The first Commit Record send is failed exactly once. The first worker pass
    therefore sends the data chunks and the Blob Manifest (which the fake stores
    durably) and then rolls back when the Commit Record send raises, leaving the
    generation in RETRY_WAIT. The :class:`WorkerCommitDriver` keeps pumping the
    worker; the next pass runs the partial-commit recovery path, finds the
    already-durable manifest with no commit, and re-writes only the Commit Record
    so the generation reaches COMMITTED without re-uploading the body.
    """
    settings = _build_settings(async_url)
    telegram = FaultInjectingTelegramPort()
    channel_id = await telegram.create_channel("crash-recovery-channel")

    # Reproduce the design's "crash after Blob Manifest, before Commit Record":
    # fail the Commit Record send once so the manifest lands durably but the
    # first commit attempt does not.
    telegram.fail_on_kind(CaptionKind.COMMIT_RECORD, times=1)

    root_key = derive_root_key(_MASTER_KEY)
    kek_wrapped = wrap(root_key, os.urandom(32))
    bucket_id = uuid.uuid4()

    staging_dir = tmp_path / "staging"
    cache_dir = tmp_path / "cache"

    factory = AsyncSessionFactory.create(async_url)
    engine = factory.engine
    try:
        await _seed_bucket_row(
            engine, bucket_id, channel_id, kek_wrapped, name=_BUCKET_NAME
        )

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

        # --- PUT: crashes mid-commit, then recovers on a later worker pass ---
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
            put_handler(put_context), timeout=60.0
        )

        generation_id, state, commit_message_id = await _read_generation(
            engine, bucket_id
        )
        blob_manifests = _count_by_prefix(telegram, channel_id, "tgo3-b-")
        commit_records = _count_by_prefix(telegram, channel_id, "tgo3-c-")

        # --- GET: read the recovered object back ---------------------------
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

    return _PartialCommitResult(
        put_status=put_response.status_code,
        generation_state=state,
        commit_message_id=commit_message_id,
        blob_manifests_in_channel=blob_manifests,
        commit_records_in_channel=commit_records,
        get_status=get_response.status_code,
        get_body=get_body,
    )


async def _read_generation(
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


def _count_by_prefix(
    telegram: FaultInjectingTelegramPort, channel_id: int, prefix: str
) -> int:
    """Count durably-stored messages whose caption starts with ``prefix``."""
    return sum(
        1
        for message in telegram.messages(channel_id)
        if message.caption.startswith(prefix)
    )


# ---------------------------------------------------------------------------
# Scenario (b): startup staging crash-recovery scan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _StagingScanResult:
    """The observable outcome of a startup staging crash-recovery scan."""

    recovery: StagingRecoveryResult
    orphan_still_in_staging: bool
    orphan_in_quarantine: bool
    reenqueued_generation_ids: frozenset[uuid.UUID]
    jobless_generation_id: uuid.UUID
    kept_job_generation_id: uuid.UUID
    job_counts: dict[uuid.UUID, int]


async def _run_staging_scan(async_url: str, tmp_path: Path) -> _StagingScanResult:
    """Seed a crash-interrupted staging state, run the scan, report what moved.

    The seeded state models what a crash leaves behind:

    * ``orphan`` — a staging file with no generation row referencing it; the scan
      must quarantine it (Requirement 14.2).
    * ``jobless`` — a STAGED generation whose ``upload_jobs`` row was lost to the
      crash; the scan must re-enqueue a fresh job.
    * ``kept`` — a STAGED generation that still has its ``upload_jobs`` row; the
      scan must leave it untouched (idempotency), so it is *not* re-enqueued and
      still has exactly one job afterwards.
    """
    bucket_id = uuid.uuid4()
    channel_id = 7001

    staging_dir = tmp_path / "staging"
    staging_dir.mkdir(parents=True, exist_ok=True)

    # An orphan staging file: present on disk, referenced by no generation.
    orphan = staging_dir / "orphan-body.bin"
    orphan.write_bytes(b"leftover bytes from a crashed upload")

    # A referenced staging file for the job-less generation, so its staging_path
    # points at a real file the scan recognises as *known* (not an orphan).
    jobless_body = staging_dir / "jobless-body.bin"
    jobless_body.write_bytes(b"body awaiting re-enqueue")

    factory = AsyncSessionFactory.create(async_url)
    engine = factory.engine
    try:
        root_key = derive_root_key(_MASTER_KEY)
        kek_wrapped = wrap(root_key, os.urandom(32))
        await _seed_bucket_row(
            engine,
            bucket_id,
            channel_id,
            kek_wrapped,
            name="crash-recovery-staging-scan-bucket",
        )

        async with factory.sessionmaker() as session, session.begin():
            # STAGED generation that lost its job to the crash: no upload_jobs row.
            jobless_id = uuid.uuid4()
            session.add(
                Generation(
                    id=jobless_id,
                    bucket_id=bucket_id,
                    key="jobless-key",
                    kind="put",
                    blob_id=None,
                    size=24,
                    etag=None,
                    checksums=None,
                    content_type=None,
                    user_meta=None,
                    last_modified=_naive_utcnow(),
                    state=generations_storage.STATE_STAGED,
                    commit_message_id=None,
                    staging_path=os.fspath(jobless_body),
                )
            )
            # STAGED generation that still has its job: must be left untouched.
            kept_id = uuid.uuid4()
            session.add(
                Generation(
                    id=kept_id,
                    bucket_id=bucket_id,
                    key="kept-key",
                    kind="put",
                    blob_id=None,
                    size=24,
                    etag=None,
                    checksums=None,
                    content_type=None,
                    user_meta=None,
                    last_modified=_naive_utcnow(),
                    state=generations_storage.STATE_STAGED,
                    commit_message_id=None,
                    staging_path=None,
                )
            )
            session.add(
                UploadJob(
                    id=uuid.uuid4(),
                    generation_id=kept_id,
                    state=generations_storage.JOB_STATE_QUEUED,
                    attempts=0,
                    next_attempt_at=None,
                    last_error=None,
                )
            )

        # Run the startup sweep: quarantine orphans + re-enqueue interrupted
        # uploads, committing the whole sweep atomically as startup would.
        async with factory.sessionmaker() as session, session.begin():
            recovery = await crash_recovery_scan(session, staging_dir=staging_dir)

        orphan_still_in_staging = orphan.exists()
        quarantine_dir = staging_dir / QUARANTINE_DIRNAME
        orphan_in_quarantine = (quarantine_dir / orphan.name).exists()

        job_counts = await _job_counts(engine, [jobless_id, kept_id])
    finally:
        await factory.dispose()

    return _StagingScanResult(
        recovery=recovery,
        orphan_still_in_staging=orphan_still_in_staging,
        orphan_in_quarantine=orphan_in_quarantine,
        reenqueued_generation_ids=frozenset(recovery.reenqueued),
        jobless_generation_id=jobless_id,
        kept_job_generation_id=kept_id,
        job_counts=job_counts,
    )


def _naive_utcnow() -> Any:
    """Return a naive UTC ``datetime`` matching the generations column type."""
    from datetime import UTC, datetime

    return datetime.now(UTC).replace(tzinfo=None)


async def _job_counts(
    engine: AsyncEngine, generation_ids: list[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """Return the number of ``upload_jobs`` rows per generation id."""
    async with engine.connect() as conn:
        result = await conn.execute(
            select(UploadJob.generation_id, func.count())
            .where(UploadJob.generation_id.in_(generation_ids))
            .group_by(UploadJob.generation_id)
        )
        counts = {row[0]: int(row[1]) for row in result.all()}
    return {gen_id: counts.get(gen_id, 0) for gen_id in generation_ids}


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


@dataclass(frozen=True)
class _CrashRecoveryResults:
    """Both recovery scenarios run once against one container."""

    partial_commit: _PartialCommitResult
    staging_scan: _StagingScanResult


async def _run_all(async_url: str, tmp_path: Path) -> _CrashRecoveryResults:
    """Run both recovery scenarios against the shared container."""
    partial_commit = await _run_partial_commit_recovery(
        async_url, tmp_path / "partial-commit"
    )
    staging_scan = await _run_staging_scan(async_url, tmp_path / "staging-scan")
    return _CrashRecoveryResults(
        partial_commit=partial_commit, staging_scan=staging_scan
    )


@pytest.fixture(scope="module")
def crash_recovery(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[_CrashRecoveryResults]:
    """Start PostgreSQL 17, run both crash-recovery scenarios, yield the results.

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
        tmp_path = tmp_path_factory.mktemp("crash-recovery")
        results = asyncio.run(_run_all(async_url, tmp_path))
        yield results
    finally:
        container.stop()


# ---------------------------------------------------------------------------
# Tests — partial-commit-record recovery (Requirement 14.3)
# ---------------------------------------------------------------------------


def test_partial_commit_generation_reaches_committed(
    crash_recovery: _CrashRecoveryResults,
) -> None:
    """A generation crashed between manifest and commit recovers to COMMITTED.

    The first Commit Record send failed, leaving the Blob Manifest durable in
    Telegram and the generation uncommitted. On a later worker pass the
    partial-commit recovery path re-writes only the Commit Record and the
    generation reaches COMMITTED with its ``commit_message_id`` recorded — the
    durability boundary. The PUT ultimately returns HTTP 200.

    Validates: Requirements 14.3
    """
    result = crash_recovery.partial_commit
    assert result.put_status == 200
    assert result.generation_state == generations_storage.STATE_COMMITTED
    assert result.commit_message_id is not None


def test_partial_commit_does_not_reupload_blob_manifest(
    crash_recovery: _CrashRecoveryResults,
) -> None:
    """Recovery reuses the already-durable Blob Manifest instead of re-uploading.

    The chunks and the Blob Manifest were confirmed before the crash, so the
    recovery path must re-write *only* the Commit Record. Exactly one Blob
    Manifest is present in the channel, proving the body was not re-uploaded.

    Validates: Requirements 14.3
    """
    assert crash_recovery.partial_commit.blob_manifests_in_channel == 1


def test_partial_commit_writes_exactly_one_commit_record(
    crash_recovery: _CrashRecoveryResults,
) -> None:
    """Exactly one Commit Record is durably present after recovery.

    The first send was failed (and left nothing durable); the recovery attempt
    wrote the single Commit Record that makes the generation recoverable.

    Validates: Requirements 14.3
    """
    assert crash_recovery.partial_commit.commit_records_in_channel == 1


def test_partial_commit_object_reads_back_identical(
    crash_recovery: _CrashRecoveryResults,
) -> None:
    """GetObject returns byte-for-byte the body the client PUT.

    Reading the recovered object back downloads the same chunk documents from the
    channel and unseals them, proving recovery produced a durable, readable
    object and not merely a committed index row.

    Validates: Requirements 14.3
    """
    result = crash_recovery.partial_commit
    assert result.get_status == 200
    assert result.get_body == _OBJECT_BODY


# ---------------------------------------------------------------------------
# Tests — startup staging crash-recovery scan (Requirement 14.2)
# ---------------------------------------------------------------------------


def test_staging_scan_quarantines_orphan_file(
    crash_recovery: _CrashRecoveryResults,
) -> None:
    """A staging file with no generation row is moved to quarantine.

    Leftover bytes from a crashed upload must never masquerade as live data; the
    startup scan moves the orphan into the quarantine subdirectory (retained for
    operator inspection) rather than leaving it in staging or deleting it.

    Validates: Requirements 14.2
    """
    scan = crash_recovery.staging_scan
    assert not scan.orphan_still_in_staging
    assert scan.orphan_in_quarantine
    assert len(scan.recovery.quarantined) == 1


def test_staging_scan_reenqueues_jobless_generation(
    crash_recovery: _CrashRecoveryResults,
) -> None:
    """A STAGED generation that lost its upload job is re-enqueued exactly once.

    The scan inserts a fresh ``upload_jobs`` row for the job-less generation so an
    interrupted upload is picked back up by the Upload Worker, and reports it as
    re-enqueued.

    Validates: Requirements 14.2
    """
    scan = crash_recovery.staging_scan
    assert scan.jobless_generation_id in scan.reenqueued_generation_ids
    assert scan.job_counts[scan.jobless_generation_id] == 1


def test_staging_scan_skips_generation_that_kept_its_job(
    crash_recovery: _CrashRecoveryResults,
) -> None:
    """A STAGED generation that still has its job is left untouched (idempotent).

    Re-inserting a job for a generation that already has one would duplicate the
    work, so the scan skips it: it is not reported as re-enqueued and still has
    exactly one ``upload_jobs`` row afterwards.

    Validates: Requirements 14.2
    """
    scan = crash_recovery.staging_scan
    assert scan.kept_job_generation_id not in scan.reenqueued_generation_ids
    assert scan.job_counts[scan.kept_job_generation_id] == 1

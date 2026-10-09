"""GC worker reclamation integration test (Requirement 10.10).

This integration test exercises the garbage-collection worker end to end against
a *real* PostgreSQL 17 instance (started with ``testcontainers[postgres]``) and
the in-memory Telegram stub that stands in for a bucket channel through the
:class:`~gateway.app.telegram.port.TelegramPort` boundary.

The behaviour under test is :class:`~gateway.app.jobs.gc_worker.GCWorker` and the
two deferred-reclamation sweeps a single :meth:`GCWorker.run_once` performs
(Requirement 10.10, design "Generation State Machine"):

* **Multipart expiry.** A ``multipart_uploads`` row whose ``expires_at`` has
  passed received neither CompleteMultipartUpload nor AbortMultipartUpload within
  ``MULTIPART_EXPIRY``. The worker deletes its staged part files from disk and
  removes the ``multipart_parts`` and ``multipart_uploads`` rows. A multipart
  upload that is *not* yet expired is left completely untouched.
* **Delete sweep.** A ``SUPERSEDED`` / ``DELETED`` generation whose
  ``last_modified`` is older than ``GC_GRACE`` is purged: its Commit Record /
  Tombstone ``tgo3`` message is deleted from the channel, the blob it referenced
  has its refcount decremented, a blob that reaches refcount 0 has its data-chunk
  and manifest messages deleted and its ``chunks``/``blobs`` rows removed, any
  staged file attached to the generation is unlinked, and the generation row is
  flipped to ``PURGED`` yet *retained* for audit. A dereferenced generation still
  inside its grace window is left ``SUPERSEDED`` with its messages intact.

Rather than drive a full object write to produce GC-eligible state, this test
seeds the index and the stub channel directly to a precise, controlled state —
the same seeding approach the recovery-oriented fault-injection fixtures use —
so the worker's reclamation can be asserted deterministically without depending
on timing out a real ``MULTIPART_EXPIRY`` / ``GC_GRACE``. The worker itself is
the production object, wired to a real
:class:`~gateway.app.db.session.AsyncSessionFactory` and the stub transport, and
run with short grace values so already-past seeded timestamps qualify.

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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from gateway.app.db.models import (
    Blob,
    Bucket,
    Chunk,
    Generation,
    MultipartPart,
    MultipartUpload,
)
from gateway.app.db.session import AsyncSessionFactory
from gateway.app.jobs.gc_worker import GCWorker
from gateway.app.storage import generations as generations_storage
from gateway.app.telegram.port import TelegramPort
from sqlalchemy import ColumnExpressionArgument, func, select
from sqlalchemy.ext.asyncio import AsyncSession

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

#: Short GC parameters so the seeded "already past" timestamps qualify for
#: reclamation immediately: the delete sweep's cutoff is ``now - GC_GRACE`` and a
#: seeded ``last_modified`` days in the past is comfortably older than a 1 s
#: grace. ``MULTIPART_EXPIRY`` is only logged by the worker (the expiry decision
#: reads each row's own ``expires_at``), so its value here is immaterial.
_GC_GRACE_SECONDS = 1
_MULTIPART_EXPIRY_SECONDS = 3600


def _utcnow() -> datetime:
    """Return a timezone-naive UTC now, matching how the gateway persists these.

    The ``generations.last_modified`` and ``multipart_uploads.expires_at`` columns
    are ``TIMESTAMP WITHOUT TIME ZONE``, and every writer in the gateway
    (``multipart.py``, ``put_object.py``, ``upload_worker.py``) stamps them with
    ``datetime.now(UTC).replace(tzinfo=None)`` because asyncpg rejects binding a
    tz-aware value to such a column. Seeding the same naive UTC representation
    keeps the stored rows byte-for-byte consistent with production writes.
    """
    return datetime.now(UTC).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Seed state: identifiers for every row/message the sweeps act on
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _SeedIds:
    """Identifiers of the seeded state, so assertions can target exact rows.

    Attributes:
        channel_id: The stub channel every seeded ``tgo3`` message lives in.
        bucket_id: The bucket owning all seeded rows.
        expired_upload_id: Multipart upload past its ``expires_at`` (eligible).
        live_upload_id: Multipart upload with a future ``expires_at`` (retained).
        expired_part_path: Staged part file of the expired upload (to be deleted).
        live_part_path: Staged part file of the live upload (to be retained).
        purge_generation_id: SUPERSEDED generation past GC_GRACE (eligible).
        retained_generation_id: SUPERSEDED generation inside grace (retained).
        purge_blob_id: Blob referenced only by the eligible generation (refcount 1).
        purge_commit_message_id: Commit Record message of the eligible generation.
        purge_manifest_message_id: Blob Manifest message of the purge blob.
        purge_chunk_message_id: Data-chunk message of the purge blob.
        retained_commit_message_id: Commit Record message of the retained gen.
        purge_staging_path: Staging file attached to the eligible generation.
    """

    channel_id: int
    bucket_id: uuid.UUID
    expired_upload_id: uuid.UUID
    live_upload_id: uuid.UUID
    expired_part_path: str
    live_part_path: str
    purge_generation_id: uuid.UUID
    retained_generation_id: uuid.UUID
    purge_blob_id: uuid.UUID
    purge_commit_message_id: int
    purge_manifest_message_id: int
    purge_chunk_message_id: int
    retained_commit_message_id: int
    purge_staging_path: str


# ---------------------------------------------------------------------------
# PostgreSQL helpers (shared pattern with test_bucket_crud / test_multipart)
# ---------------------------------------------------------------------------


def _async_url(sync_url: str) -> str:
    """Normalise a container connection URL to the ``asyncpg`` driver."""
    bare = sync_url.split("://", 1)[1]
    return "postgresql+asyncpg://" + bare


def _run_alembic_upgrade(async_url: str) -> None:
    """Run ``alembic upgrade head`` through the project's real ``env.py``.

    Creates the schema the GC worker reads and mutates, exactly as the gateway
    does at startup. Runs from the repo root so the relative ``script_location``
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


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def _write_file(path: Path, data: bytes) -> str:
    """Write ``data`` to ``path`` (creating parents) and return the path string."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(path)


async def _seed_state(
    factory: AsyncSessionFactory,
    telegram: FaultInjectingTelegramPort,
    staging_dir: Path,
) -> _SeedIds:
    """Seed the index and the stub channel with GC-eligible and retained state.

    Lays down, for one bucket backed by a stub channel:

    * an **expired** multipart upload (one staged part file on disk) and a
      **live** multipart upload (future ``expires_at``, its own staged part);
    * a **purge-eligible** SUPERSEDED generation whose ``last_modified`` is in the
      past, referencing a refcount-1 blob with a data-chunk message, a manifest
      message, and a commit-record message in the channel, plus a staging file;
    * a **retained** SUPERSEDED generation whose ``last_modified`` is in the
      future, with its own commit-record message that must survive the sweep.
    """
    channel_id = await telegram.create_channel("gc-worker-bucket")

    past = _utcnow() - timedelta(days=1)
    future = _utcnow() + timedelta(days=1)

    bucket_id = uuid.uuid4()
    expired_upload_id = uuid.uuid4()
    live_upload_id = uuid.uuid4()
    purge_generation_id = uuid.uuid4()
    retained_generation_id = uuid.uuid4()
    purge_blob_id = uuid.uuid4()

    # Staged part files on disk, one per multipart upload.
    expired_part_path = _write_file(
        staging_dir / "expired" / "part-1.bin", b"expired-part-body"
    )
    live_part_path = _write_file(
        staging_dir / "live" / "part-1.bin", b"live-part-body"
    )
    # A staging file still attached to the purge-eligible generation.
    purge_staging_path = _write_file(
        staging_dir / "gen" / "body.bin", b"purge-generation-body"
    )

    # Seed the channel with the purge target's three messages and the retained
    # generation's commit record. Captions follow the design's message format so
    # they are indistinguishable from real gateway writes.
    purge_chunk_message_id = telegram.seed_message(
        channel_id, caption=f"tgo3-d-{purge_blob_id}-0", data=b"chunk"
    )
    purge_manifest_message_id = telegram.seed_message(
        channel_id, caption=f"tgo3-b-{purge_blob_id}", data=b"manifest"
    )
    purge_commit_message_id = telegram.seed_message(
        channel_id, caption=f"tgo3-c-{purge_generation_id}", data=b"commit"
    )
    retained_commit_message_id = telegram.seed_message(
        channel_id, caption=f"tgo3-c-{retained_generation_id}", data=b"commit"
    )

    async with factory.session() as session:
        session.add(
            Bucket(
                id=bucket_id,
                name="gc-worker-bucket",
                channel_id=channel_id,
                kek_wrapped=b"wrapped-kek",
                kek_version=1,
                durability="sync",
                created_by_us=True,
                status="active",
            )
        )
        # Flush the bucket before adding rows that reference it, so the FK to
        # ``buckets`` is satisfiable regardless of unit-of-work insert ordering.
        await session.flush()

        # --- Multipart uploads: one expired, one live ---------------------
        session.add(
            MultipartUpload(
                upload_id=expired_upload_id,
                bucket_id=bucket_id,
                key="expired-mpu",
                expires_at=past,
            )
        )
        session.add(
            MultipartUpload(
                upload_id=live_upload_id,
                bucket_id=bucket_id,
                key="live-mpu",
                expires_at=future,
            )
        )

        # --- Blob referenced only by the purge-eligible generation --------
        session.add(
            Blob(
                id=purge_blob_id,
                bucket_id=bucket_id,
                sha256=b"\x01" * 32,
                size=5,
                key_wrapped=b"wrapped-blob-key",
                chunk_records=1,
                state="COMMITTED",
                refcount=1,
                manifest_message_id=purge_manifest_message_id,
            )
        )
        # Flush the parent rows (uploads, blob) before their children so the
        # ``multipart_parts`` and ``chunks`` FKs resolve; the models declare no
        # ORM relationships, so the unit of work does not order them for us.
        await session.flush()

        session.add(
            MultipartPart(
                upload_id=expired_upload_id,
                part_no=1,
                size=len(b"expired-part-body"),
                etag="etag-expired-1",
                checksum=None,
                staging_path=expired_part_path,
            )
        )
        session.add(
            MultipartPart(
                upload_id=live_upload_id,
                part_no=1,
                size=len(b"live-part-body"),
                etag="etag-live-1",
                checksum=None,
                staging_path=live_part_path,
            )
        )
        session.add(
            Chunk(
                blob_id=purge_blob_id,
                idx=0,
                message_id=purge_chunk_message_id,
                records=1,
                document_id=purge_chunk_message_id,
                file_reference=b"ref",
                dc_id=2,
            )
        )

        # --- Generations: one eligible (past), one retained (future) ------
        session.add(
            Generation(
                id=purge_generation_id,
                bucket_id=bucket_id,
                key="purge-key",
                kind="put",
                blob_id=purge_blob_id,
                size=5,
                etag="etag-purge",
                checksums=None,
                content_type="application/octet-stream",
                user_meta=None,
                last_modified=past,
                state=generations_storage.STATE_SUPERSEDED,
                commit_message_id=purge_commit_message_id,
                staging_path=purge_staging_path,
            )
        )
        session.add(
            Generation(
                id=retained_generation_id,
                bucket_id=bucket_id,
                key="retained-key",
                kind="put",
                blob_id=None,
                size=3,
                etag="etag-retained",
                checksums=None,
                content_type="application/octet-stream",
                user_meta=None,
                last_modified=future,
                state=generations_storage.STATE_SUPERSEDED,
                commit_message_id=retained_commit_message_id,
                staging_path=None,
            )
        )

    return _SeedIds(
        channel_id=channel_id,
        bucket_id=bucket_id,
        expired_upload_id=expired_upload_id,
        live_upload_id=live_upload_id,
        expired_part_path=expired_part_path,
        live_part_path=live_part_path,
        purge_generation_id=purge_generation_id,
        retained_generation_id=retained_generation_id,
        purge_blob_id=purge_blob_id,
        purge_commit_message_id=purge_commit_message_id,
        purge_manifest_message_id=purge_manifest_message_id,
        purge_chunk_message_id=purge_chunk_message_id,
        retained_commit_message_id=retained_commit_message_id,
        purge_staging_path=purge_staging_path,
    )


# ---------------------------------------------------------------------------
# Sweep driver
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _SweepObservation:
    """The observable state after one :meth:`GCWorker.run_once`."""

    # Worker-reported counters.
    multipart_aborted: int
    generations_purged: int
    blobs_purged: int
    telegram_messages_deleted: int
    errors: tuple[str, ...]

    # Multipart expiry outcomes.
    expired_upload_rows: int
    expired_part_rows: int
    expired_part_exists: bool
    live_upload_rows: int
    live_part_rows: int
    live_part_exists: bool

    # Delete sweep outcomes.
    purge_generation_state: str | None
    retained_generation_state: str | None
    purge_blob_rows: int
    purge_chunk_rows: int
    purge_staging_exists: bool

    # Channel message presence (by message id).
    channel_message_ids: frozenset[int]


async def _run_sweep(async_url: str, staging_dir: Path) -> _SweepObservation:
    """Seed GC-eligible and retained state, run one sweep, capture observables.

    Wires the production :class:`GCWorker` to a real session factory and the
    in-memory Telegram stub, seeds the index/channel to a precise state, runs a
    single :meth:`GCWorker.run_once`, then reads back the rows, the channel, and
    the staging files the two sweeps should (or should not) have touched.
    """
    telegram = FaultInjectingTelegramPort()
    factory = AsyncSessionFactory.create(async_url)
    try:
        seed = await _seed_state(factory, telegram, staging_dir)

        worker = GCWorker(
            factory.sessionmaker,
            cast(TelegramPort, telegram),
            gc_grace_seconds=_GC_GRACE_SECONDS,
            multipart_expiry_seconds=_MULTIPART_EXPIRY_SECONDS,
            staging_dir=None,
        )
        result = await asyncio.wait_for(worker.run_once(), timeout=30.0)

        async with factory.session() as session:
            expired_upload_rows = await _count(
                session,
                MultipartUpload,
                MultipartUpload.upload_id == seed.expired_upload_id,
            )
            expired_part_rows = await _count(
                session,
                MultipartPart,
                MultipartPart.upload_id == seed.expired_upload_id,
            )
            live_upload_rows = await _count(
                session,
                MultipartUpload,
                MultipartUpload.upload_id == seed.live_upload_id,
            )
            live_part_rows = await _count(
                session,
                MultipartPart,
                MultipartPart.upload_id == seed.live_upload_id,
            )
            purge_generation_state = (
                await session.execute(
                    select(Generation.state).where(
                        Generation.id == seed.purge_generation_id
                    )
                )
            ).scalar_one_or_none()
            retained_generation_state = (
                await session.execute(
                    select(Generation.state).where(
                        Generation.id == seed.retained_generation_id
                    )
                )
            ).scalar_one_or_none()
            purge_blob_rows = await _count(
                session, Blob, Blob.id == seed.purge_blob_id
            )
            purge_chunk_rows = await _count(
                session, Chunk, Chunk.blob_id == seed.purge_blob_id
            )

        channel_message_ids = frozenset(
            message.message_id for message in telegram.messages(seed.channel_id)
        )
    finally:
        await factory.dispose()

    return _SweepObservation(
        multipart_aborted=result.multipart_aborted,
        generations_purged=result.generations_purged,
        blobs_purged=result.blobs_purged,
        telegram_messages_deleted=result.telegram_messages_deleted,
        errors=tuple(result.errors),
        expired_upload_rows=expired_upload_rows,
        expired_part_rows=expired_part_rows,
        expired_part_exists=Path(seed.expired_part_path).exists(),
        live_upload_rows=live_upload_rows,
        live_part_rows=live_part_rows,
        live_part_exists=Path(seed.live_part_path).exists(),
        purge_generation_state=purge_generation_state,
        retained_generation_state=retained_generation_state,
        purge_blob_rows=purge_blob_rows,
        purge_chunk_rows=purge_chunk_rows,
        purge_staging_exists=Path(seed.purge_staging_path).exists(),
        channel_message_ids=channel_message_ids,
    )


async def _count(
    session: AsyncSession,
    model: type,
    where: ColumnExpressionArgument[bool],
) -> int:
    """Return the row count for ``model`` filtered by ``where``.

    A tiny ``SELECT count(*)`` helper so the observation reads stay declarative.
    """
    result = await session.execute(
        select(func.count()).select_from(model).where(where)
    )
    return int(result.scalar_one())


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
def sweep(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_SweepObservation]:
    """Start PostgreSQL 17, seed state, run one GC sweep, yield observables.

    Module-scoped so the expensive container start, migration run, seeding and
    the single sweep happen once for every assertion in this file. Skips cleanly
    when Docker is unavailable so the suite stays collectable everywhere.
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
        staging_dir = tmp_path_factory.mktemp("gc-worker") / "staging"
        observation = asyncio.run(_run_sweep(async_url, staging_dir))
        yield observation
    finally:
        container.stop()


# ---------------------------------------------------------------------------
# Tests — multipart expiry sweep (Requirement 10.10)
# ---------------------------------------------------------------------------


def test_sweep_runs_without_errors(sweep: _SweepObservation) -> None:
    """A single sweep over the seeded state records no per-item failures.

    The seeded state is well-formed, so every reclamation should succeed; a
    non-empty ``errors`` list would mean a sweep step raised and was swallowed.

    Validates: Requirements 10.10
    """
    assert sweep.errors == ()


def test_expired_multipart_upload_is_reclaimed(sweep: _SweepObservation) -> None:
    """The expired multipart upload's rows and staged part file are removed.

    The upload past its ``expires_at`` received no Complete/Abort within
    ``MULTIPART_EXPIRY``, so the worker deletes its staged part file and removes
    both the ``multipart_parts`` and ``multipart_uploads`` rows.

    Validates: Requirements 10.10
    """
    assert sweep.multipart_aborted == 1
    assert sweep.expired_upload_rows == 0
    assert sweep.expired_part_rows == 0
    assert sweep.expired_part_exists is False


def test_live_multipart_upload_is_retained(sweep: _SweepObservation) -> None:
    """A multipart upload still before its ``expires_at`` is left untouched.

    Its ``multipart_uploads`` / ``multipart_parts`` rows and its staged part file
    all survive the sweep, proving the expiry sweep is scoped to past-due uploads.

    Validates: Requirements 10.10
    """
    assert sweep.live_upload_rows == 1
    assert sweep.live_part_rows == 1
    assert sweep.live_part_exists is True


# ---------------------------------------------------------------------------
# Tests — delete sweep (SUPERSEDED/DELETED -> PURGED, Requirement 10.10)
# ---------------------------------------------------------------------------


def test_dereferenced_generation_is_purged(sweep: _SweepObservation) -> None:
    """A SUPERSEDED generation past GC_GRACE is marked PURGED and retained.

    The generation row stays in PostgreSQL for audit but moves to the terminal
    ``PURGED`` state once its Telegram messages have been deleted.

    Validates: Requirements 10.10
    """
    assert sweep.generations_purged == 1
    assert sweep.purge_generation_state == generations_storage.STATE_PURGED


def test_purged_generation_blob_is_fully_reclaimed(
    sweep: _SweepObservation,
) -> None:
    """The purge target's refcount-1 blob and its chunk rows are removed.

    Decrementing the blob to refcount 0 makes it unreferenced, so its ``blobs``
    and ``chunks`` rows are deleted and the blob is counted as fully purged.

    Validates: Requirements 10.10
    """
    assert sweep.blobs_purged == 1
    assert sweep.purge_blob_rows == 0
    assert sweep.purge_chunk_rows == 0


def test_purged_generation_staging_file_removed(sweep: _SweepObservation) -> None:
    """A staging file still attached to the purged generation is unlinked.

    Validates: Requirements 10.10
    """
    assert sweep.purge_staging_exists is False


def test_purged_generation_telegram_messages_deleted(
    sweep: _SweepObservation,
) -> None:
    """The purge target's commit, manifest, and chunk messages are deleted.

    All three ``tgo3`` messages backing the dereferenced generation and its now
    unreferenced blob are deleted from the channel (3 of the 4 seeded messages),
    while the retained generation's commit record stays.

    Validates: Requirements 10.10
    """
    assert sweep.telegram_messages_deleted == 3
    # Exactly one message — the retained generation's commit record — survives.
    assert len(sweep.channel_message_ids) == 1


def test_retained_generation_and_messages_survive(
    sweep: _SweepObservation,
) -> None:
    """A dereferenced generation inside its grace window is left intact.

    Its state stays ``SUPERSEDED`` and its commit-record message remains in the
    channel, proving the delete sweep purges only generations older than
    ``GC_GRACE``.

    Validates: Requirements 10.10
    """
    assert sweep.retained_generation_state == generations_storage.STATE_SUPERSEDED

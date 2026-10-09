# Feature: tgobjects3, Property 12: Dedup does not re-upload identical content
"""Property 12 — dedup does not re-upload identical content (Requirement 6.10).

Design "Property 12: Dedup does not re-upload identical content":

    *For any* bucket and any two PutObject calls supplying byte-for-byte
    identical bodies, the second call SHALL result in a new COMMITTED generation
    that references the same blob row (by ``blob_id``) as the first call, and no
    new Telegram document upload operations SHALL be performed for the second
    call.

This exercises the Upload Worker end to end against a *real* PostgreSQL 17
(``testcontainers[postgres]``), because the dedup path it validates is built on
machinery that only a real database provides: the content-addressed
``pg_advisory_lock`` serialising same-SHA-256 writers and the
``SELECT ... FOR UPDATE SKIP LOCKED`` job claim (Requirements 6.10, 6.11, 14.1).
A fake would have to re-implement both to be meaningful, so the test uses the
genuine schema, the genuine :class:`~gateway.app.jobs.upload_worker.UploadWorker`,
and only fakes the one true boundary — Telegram — behind the
:class:`~gateway.app.telegram.port.TelegramPort` Protocol.

The Telegram fake (:class:`CountingTelegramPort`) is an in-memory channel store
that *counts every* ``send_document`` *call, grouped by the caption prefix* the
worker stamps on each message (``tgo3-d-`` data chunk, ``tgo3-b-`` blob
manifest, ``tgo3-c-`` commit record). That grouping is what lets the test state
the property precisely: "no new document *upload* operations for the second
call" means **no new blob-content uploads** — zero additional data-chunk and
zero additional blob-manifest sends — because the content already lives in
Telegram and must be reused, not re-sent. (A fresh commit record is still
written for the second generation: it is per-generation metadata naming the
reused blob, not a re-upload of the deduplicated body.)

Scenario:

1. Seed one bucket (its row + wrapped KEK) and stage two generations for the
   *same* key with byte-for-byte identical bodies — hence an identical SHA-256,
   the dedup key within the bucket.
2. Run the worker once: it seals, uploads the data chunk(s) and the blob
   manifest, writes the commit record, and commits generation #1 against a fresh
   blob.
3. Run the worker again for generation #2 and assert: it performs **zero** new
   ``tgo3-d-`` and **zero** new ``tgo3-b-`` sends (the body is not re-uploaded),
   the generation reaches COMMITTED, and its ``blob_id`` equals generation #1's.

The suite needs a Docker daemon to launch PostgreSQL. When no container runtime
is available it skips cleanly rather than failing, so ``make lint`` /
``make typecheck`` stay meaningful without a daemon present. The Telegram side
never touches the network.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest
from gateway.app.db.models import Generation
from gateway.app.format.key_wrap import derive_root_key, wrap
from gateway.app.jobs.upload_worker import UploadWorker
from gateway.app.storage import generations as generations_storage
from gateway.app.telegram.port import SessionState, TelegramMessage
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# ``testcontainers`` is an integration-only dependency and starting a container
# requires a Docker daemon. Guard the import so the module can be collected
# everywhere and skip with a clear reason when the package is absent.
try:
    from testcontainers.postgres import PostgresContainer

    _TESTCONTAINERS_IMPORT_ERROR: str | None = None
except Exception as exc:  # pragma: no cover - exercised only without the extra
    PostgresContainer = None  # type: ignore[assignment]
    _TESTCONTAINERS_IMPORT_ERROR = str(exc)


# Repository root: tests/integration/<file>.py -> repo root is two parents up.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_ALEMBIC_INI = _REPO_ROOT / "alembic.ini"

#: Postgres image pinned to the major version the design mandates (17+).
_POSTGRES_IMAGE = "postgres:17-alpine"

#: Master key the worker derives its root key from to unwrap the bucket KEK and
#: wrap per-blob keys. Fixed for determinism; only has to satisfy the >=16-char
#: contract the key hierarchy expects.
_MASTER_KEY = "integration-test-master-key-0001"

#: Channel backing the one bucket under test.
_BUCKET_CHANNEL_ID = 7101

#: Caption prefixes the Upload Worker stamps on each message kind. Mirrors the
#: module-private constants in :mod:`gateway.app.jobs.upload_worker` (``tgo3-d-``
#: data chunk, ``tgo3-b-`` blob manifest, ``tgo3-c-`` commit record); duplicated
#: here so the test can classify sends by kind without importing privates.
_PREFIX_DATA = "tgo3-d-"
_PREFIX_BLOB_MANIFEST = "tgo3-b-"
_PREFIX_COMMIT = "tgo3-c-"

#: The identical body both PutObject calls supply. Spanning more than one record
#: forces multiple data-chunk sends on the first upload, so "zero new data sends"
#: on the second is a non-trivial assertion rather than a one-document accident.
_BODY = b"dedup-me-" * 100_000


# ---------------------------------------------------------------------------
# Telegram fake: counts send_document calls by caption prefix
# ---------------------------------------------------------------------------


@dataclass
class _StoredMessage:
    """One document retained in the fake's in-memory channel store."""

    message_id: int
    caption: str
    body: bytes


@dataclass
class CountingTelegramPort:
    """In-memory :class:`TelegramPort` that counts ``send_document`` by prefix.

    Every :meth:`send_document` call is appended to the target channel (so the
    worker's own recovery scan and manifest download see a faithful channel) and
    tallied in :attr:`send_counts`, keyed on the caption's ``tgo3-?-`` prefix.
    The test reads those tallies before and after the second upload to prove no
    new blob-content documents are sent (Requirement 6.10).

    Only the read/write surface the Upload Worker exercises carries behaviour;
    the remaining :class:`TelegramPort` methods are present so the fake conforms
    structurally to the Protocol but are never reached by this scenario.
    """

    channels: dict[int, list[_StoredMessage]] = field(default_factory=dict)
    send_counts: Counter[str] = field(default_factory=Counter)
    _next_message_id: int = 1

    def _prefix_of(self, caption: str) -> str:
        """Classify a caption to one of the known ``tgo3-?-`` prefixes."""
        for prefix in (_PREFIX_DATA, _PREFIX_BLOB_MANIFEST, _PREFIX_COMMIT):
            if caption.startswith(prefix):
                return prefix
        return caption

    async def send_document(
        self, channel_id: int, data: bytes, file_name: str, caption: str
    ) -> int:
        message_id = self._next_message_id
        self._next_message_id += 1
        self.channels.setdefault(channel_id, []).append(
            _StoredMessage(message_id=message_id, caption=caption, body=data)
        )
        self.send_counts[self._prefix_of(caption)] += 1
        return message_id

    async def get_messages(
        self, channel_id: int, ids: list[int]
    ) -> list[TelegramMessage]:
        wanted = set(ids)
        return [
            TelegramMessage(
                message_id=m.message_id,
                caption=m.caption,
                document_id=m.message_id,
                file_reference=f"ref-{m.message_id}".encode(),
                dc_id=2,
            )
            for m in self.channels.get(channel_id, [])
            if m.message_id in wanted
        ]

    async def download_document(self, channel_id: int, message_id: int) -> bytes:
        for m in self.channels.get(channel_id, []):
            if m.message_id == message_id:
                return m.body
        raise AssertionError(
            f"fake asked for absent message {message_id} in channel {channel_id}"
        )

    async def delete_messages(
        self, channel_id: int, message_ids: list[int]
    ) -> None:  # pragma: no cover - not exercised by this scenario
        drop = set(message_ids)
        stored = self.channels.get(channel_id)
        if stored is not None:
            self.channels[channel_id] = [
                m for m in stored if m.message_id not in drop
            ]

    async def create_channel(self, title: str) -> int:  # pragma: no cover
        raise NotImplementedError

    async def delete_channel(self, channel_id: int) -> None:  # pragma: no cover
        self.channels.pop(channel_id, None)

    async def get_session_state(self) -> SessionState:  # pragma: no cover
        return SessionState.CONNECTED

    async def is_connected(self) -> bool:  # pragma: no cover
        return True


# ---------------------------------------------------------------------------
# PostgreSQL helpers (same normalisation/migration flow the gateway uses)
# ---------------------------------------------------------------------------


def _async_url(sync_url: str) -> str:
    """Normalise a container connection URL to the ``asyncpg`` driver."""
    bare = sync_url.split("://", 1)[1]
    return "postgresql+asyncpg://" + bare


def _run_alembic_upgrade(async_url: str) -> None:
    """Run ``alembic upgrade head`` through the project's real ``env.py``."""
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


async def _seed_bucket(engine: AsyncEngine, bucket_id: uuid.UUID) -> None:
    """Insert the ``buckets`` row the staged generations reference.

    The row carries the KEK wrapped under the root key derived from
    :data:`_MASTER_KEY`, exactly as a real bucket's manifest would, so the worker
    can unwrap it to seal the blob.
    """
    root_key = derive_root_key(_MASTER_KEY)
    kek = os.urandom(32)
    kek_wrapped = wrap(root_key, kek)
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
                "name": "dedup-bucket",
                "channel_id": _BUCKET_CHANNEL_ID,
                "kek_wrapped": kek_wrapped,
                "kek_version": 1,
                "durability": "async",
                "created_by_us": True,
                "status": "active",
            },
        )


async def _stage_put(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    bucket_id: uuid.UUID,
    key: str,
    body: bytes,
    staging_dir: Path,
) -> uuid.UUID:
    """Stage one PutObject: write the body to staging and insert a generation.

    Mirrors what the PutObject handler leaves for the Upload Worker to pick up: a
    fsynced staging file plus a STAGED generation (and its QUEUED upload job)
    whose ``checksums['sha256']`` carries the hex digest the worker resolves as
    the dedup key. Returns the new generation id.
    """
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
            # ``generations.last_modified`` is TIMESTAMP WITHOUT TIME ZONE; store
            # a naive UTC timestamp to match the gateway's own write path.
            last_modified=datetime.now(UTC).replace(tzinfo=None),
            generation_id=generation_id,
        )
        # The Upload Worker resolves the dedup key (SHA-256) from the
        # generation's ``checksums['sha256']``, exactly as the PutObject handler
        # records it. ``insert_staged`` does not take checksums, so set the
        # column directly here to reproduce that handler's write.
        await session.execute(
            update(Generation)
            .where(Generation.id == generation_id)
            .values(checksums={"sha256": sha256_hex})
        )
    return generation_id


async def _generation_row(
    session_factory: async_sessionmaker[AsyncSession], generation_id: uuid.UUID
) -> tuple[str, uuid.UUID | None]:
    """Return the ``(state, blob_id)`` of a generation after processing."""
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


# ---------------------------------------------------------------------------
# Driver: two identical PUTs through the real worker under one container
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _DedupResult:
    """The observable outcome of two identical PutObject calls."""

    first_state: str
    first_blob_id: uuid.UUID | None
    second_state: str
    second_blob_id: uuid.UUID | None
    data_sends_first: int
    manifest_sends_first: int
    data_sends_second: int
    manifest_sends_second: int
    commit_sends_second: int


async def _run_two_identical_puts(async_url: str, staging_dir: Path) -> _DedupResult:
    """Stage and process two byte-identical PUTs; snapshot the dedup outcome."""
    port = CountingTelegramPort()
    bucket_id = uuid.uuid4()

    engine = create_async_engine(async_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _seed_bucket(engine, bucket_id)

        worker = UploadWorker(
            session_factory,
            port,
            _MASTER_KEY,
            default_chunk_records=512,
        )

        # First PUT: full upload of a fresh blob.
        gen1 = await _stage_put(
            session_factory,
            bucket_id=bucket_id,
            key="obj",
            body=_BODY,
            staging_dir=staging_dir,
        )
        assert await worker.run_once() is True
        first_state, first_blob_id = await _generation_row(session_factory, gen1)
        data_first = port.send_counts[_PREFIX_DATA]
        manifest_first = port.send_counts[_PREFIX_BLOB_MANIFEST]

        # Second PUT: byte-for-byte identical body -> identical SHA-256.
        gen2 = await _stage_put(
            session_factory,
            bucket_id=bucket_id,
            key="obj",
            body=_BODY,
            staging_dir=staging_dir,
        )
        assert await worker.run_once() is True
        second_state, second_blob_id = await _generation_row(session_factory, gen2)
    finally:
        await engine.dispose()

    return _DedupResult(
        first_state=first_state,
        first_blob_id=first_blob_id,
        second_state=second_state,
        second_blob_id=second_blob_id,
        data_sends_first=data_first,
        manifest_sends_first=manifest_first,
        data_sends_second=port.send_counts[_PREFIX_DATA] - data_first,
        manifest_sends_second=port.send_counts[_PREFIX_BLOB_MANIFEST] - manifest_first,
        commit_sends_second=port.send_counts[_PREFIX_COMMIT] - 1,
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
def dedup_result(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_DedupResult]:
    """Start PostgreSQL 17, run two identical PUTs, yield the frozen outcome.

    Module-scoped so the expensive container start, migration run and two uploads
    happen once for every assertion in this file. Skips cleanly when Docker is
    unavailable so the suite stays collectable everywhere.
    """
    _skip_if_no_docker()

    try:
        container = PostgresContainer(_POSTGRES_IMAGE)
        container.start()
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"Could not start PostgreSQL test container: {exc}")

    staging_dir = tmp_path_factory.mktemp("staging")
    try:
        async_url = _async_url(container.get_connection_url())
        _run_alembic_upgrade(async_url)
        result = asyncio.run(_run_two_identical_puts(async_url, staging_dir))
        yield result
    finally:
        container.stop()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_first_put_uploads_blob_content(dedup_result: _DedupResult) -> None:
    """The first PUT commits a fresh blob and actually uploads its content.

    Establishes the baseline the dedup assertions stand on: the first write of
    novel content is a full upload — at least one data-chunk document and exactly
    one blob manifest — and the generation reaches COMMITTED referencing a blob.

    Validates: Requirements 6.10
    """
    assert dedup_result.first_state == generations_storage.STATE_COMMITTED
    assert dedup_result.first_blob_id is not None
    assert dedup_result.data_sends_first >= 1
    assert dedup_result.manifest_sends_first == 1


def test_second_identical_put_reuses_same_blob(dedup_result: _DedupResult) -> None:
    """The second identical PUT commits a new generation on the SAME blob.

    Property 12: two PutObject calls with byte-for-byte identical bodies resolve
    to one blob row. The second generation reaches COMMITTED and its ``blob_id``
    equals the first generation's, so the content is shared, not duplicated.

    Validates: Requirements 6.10
    """
    assert dedup_result.second_state == generations_storage.STATE_COMMITTED
    assert dedup_result.second_blob_id is not None
    assert dedup_result.second_blob_id == dedup_result.first_blob_id


def test_second_identical_put_reuploads_no_content(
    dedup_result: _DedupResult,
) -> None:
    """The second identical PUT performs no new blob-content upload.

    Property 12: "no new Telegram document upload operations SHALL be performed
    for the second call". The deduplicated body is already durable in Telegram,
    so the second call sends zero additional data-chunk (``tgo3-d-``) and zero
    additional blob-manifest (``tgo3-b-``) documents — the content is reused in
    place rather than re-uploaded.

    Validates: Requirements 6.10
    """
    assert dedup_result.data_sends_second == 0
    assert dedup_result.manifest_sends_second == 0

"""End-to-end recovery round-trip test (Requirements 15.1, 15.4).

This integration test exercises the Recovery CLI's full "rebuild the index from
Telegram alone" path against a *real* PostgreSQL 17 instance (started with
``testcontainers[postgres]``) and an in-memory Telegram stub that replays a
bucket channel's contents through the :class:`~gateway.app.telegram.port.TelegramPort`
boundary.

The scenario mirrors how an operator recovers from total index loss:

1. A bucket channel is populated with the durable messages that make objects
   recoverable — a BucketManifest carrying the wrapped bucket KEK, followed by
   CommitRecords for several object generations and a Tombstone that deletes
   one of them. The payloads are built with the real format codecs and the real
   key-wrap primitives, so the stub replays byte-for-byte what the gateway would
   have written.
2. The ``buckets`` row the generation / head foreign keys reference is seeded
   directly (recovery rebuilds ``generations`` and ``object_heads`` from the
   channel; the bucket's existence and KEK come from the Recovery Kit).
3. The index is wiped (``object_heads``, ``generations``) to model total index
   loss.
4. :func:`cli.rebuild.run_rebuild` is run against the stub, scanning the channel
   in ascending message-id order and reconstructing the index from the
   CommitRecords and Tombstone alone (Requirement 15.1).
5. The resulting ``object_heads`` is asserted to point at exactly the surviving
   winning generation for each key: the highest ``generation_number`` wins an
   overwrite, and a tombstoned key has no head. The reported counts and gaps are
   asserted against :class:`~cli.rebuild.RebuildResult` (Requirement 15.4).

The suite needs a Docker daemon to launch PostgreSQL. When no container runtime
is available (CI without Docker, or a sandbox) the module skips cleanly rather
than failing, so ``make lint`` / ``make typecheck`` stay meaningful without a
daemon present. The Telegram side never touches the network — it is the same
in-memory stub pattern the fault-injection harness uses.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

import pytest
from cli.rebuild import RebuildArgs, run_rebuild
from gateway.app.format import (
    bucket_manifest as bucket_manifest_codec,
)
from gateway.app.format import (
    commit_record as commit_record_codec,
)
from gateway.app.format import (
    tombstone as tombstone_codec,
)
from gateway.app.format.key_wrap import derive_root_key, wrap
from gateway.app.format.types import BucketManifest, CommitRecord, Tombstone
from gateway.app.telegram.port import SessionState, TelegramMessage, TelegramPort
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

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

#: Master key used to derive the root key that wraps the bucket KEK. A fixed
#: value keeps the test deterministic; it only has to satisfy the >=16 char
#: contract the key hierarchy expects of a real master key.
_MASTER_KEY = "integration-test-master-key-0001"

#: Channel ids used by the stub. The system channel holds no checkpoint in this
#: scenario, so the whole bucket channel is scanned from the beginning.
_BUCKET_CHANNEL_ID = 7001
_SYSTEM_CHANNEL_ID = 9001


# ---------------------------------------------------------------------------
# Telegram stub
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _StoredMessage:
    """One document retained in the stub's in-memory channel store."""

    message_id: int
    caption: str
    body: bytes


class ReplayTelegramPort:
    """Minimal in-memory :class:`TelegramPort` that replays recorded messages.

    Backs each channel with an append-only list of :class:`_StoredMessage`
    objects keyed by a monotonic ``message_id``, exactly as a real channel
    assigns ids, so the Recovery CLI's ascending-order scan and on-demand
    document download see the same ordering a live channel would present
    (Requirement 15.1). Only the read surface the Recovery CLI exercises is
    implemented with behaviour; the remaining :class:`TelegramPort` methods are
    present so the stub conforms structurally to the protocol but are never
    reached by a rebuild.
    """

    def __init__(self) -> None:
        self._channels: dict[int, list[_StoredMessage]] = {}
        self._next_message_id = 1

    def append(self, channel_id: int, caption: str, body: bytes) -> int:
        """Append a document to a channel and return its new ``message_id``."""
        message_id = self._next_message_id
        self._next_message_id += 1
        self._channels.setdefault(channel_id, []).append(
            _StoredMessage(message_id=message_id, caption=caption, body=body)
        )
        return message_id

    async def get_messages(
        self, channel_id: int, ids: list[int]
    ) -> list[TelegramMessage]:
        """Return decoded messages for the requested ids present in the channel."""
        wanted = set(ids)
        return [
            TelegramMessage(
                message_id=m.message_id,
                caption=m.caption,
                document_id=m.message_id,
                file_reference=f"ref-{m.message_id}".encode(),
                dc_id=2,
            )
            for m in self._channels.get(channel_id, [])
            if m.message_id in wanted
        ]

    async def download_document(self, channel_id: int, message_id: int) -> bytes:
        """Return the stored body for a message id in a channel."""
        for m in self._channels.get(channel_id, []):
            if m.message_id == message_id:
                return m.body
        raise AssertionError(
            f"stub asked for absent message {message_id} in channel {channel_id}"
        )

    async def send_document(
        self, channel_id: int, data: bytes, file_name: str, caption: str
    ) -> int:  # pragma: no cover - not exercised by a rebuild
        return self.append(channel_id, caption, data)

    async def delete_messages(
        self, channel_id: int, message_ids: list[int]
    ) -> None:  # pragma: no cover - not exercised by a rebuild
        drop = set(message_ids)
        stored = self._channels.get(channel_id)
        if stored is not None:
            self._channels[channel_id] = [
                m for m in stored if m.message_id not in drop
            ]

    async def create_channel(self, title: str) -> int:  # pragma: no cover
        raise NotImplementedError

    async def delete_channel(self, channel_id: int) -> None:  # pragma: no cover
        self._channels.pop(channel_id, None)

    async def get_session_state(self) -> SessionState:  # pragma: no cover
        return SessionState.CONNECTED

    async def is_connected(self) -> bool:  # pragma: no cover
        return True


# ---------------------------------------------------------------------------
# Scenario construction
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Scenario:
    """The expected state after replaying the seeded channel.

    Attributes:
        bucket_id: The bucket all generations belong to; also the ``buckets``
            row seeded before the rebuild.
        kek_wrapped: The bucket KEK wrapped by the root key, stored on the
            seeded ``buckets`` row.
        kek_version: The KEK rotation generation on the seeded row.
        expected_heads: ``key -> generation_id`` the rebuilt ``object_heads``
            must contain after recovery (surviving winning generation per key).
        tombstoned_keys: Keys whose only / head generation was tombstoned and so
            must have no head after recovery.
        expected_generations: Number of CommitRecords replayed (reconstructed
            ``generations`` rows / ``reconstructed_generations``).
        expected_tombstones: Number of Tombstones that matched a reconstructed
            generation (``applied_tombstones``).
        tombstoned_generation_id: The generation id of the tombstoned key, used
            to assert its reconstructed row is marked DELETED rather than absent.
    """

    bucket_id: uuid.UUID
    kek_wrapped: bytes
    kek_version: int
    expected_heads: dict[str, uuid.UUID]
    tombstoned_keys: set[str]
    expected_generations: int
    expected_tombstones: int
    tombstoned_generation_id: uuid.UUID


def _seed_channel(port: ReplayTelegramPort) -> _Scenario:
    """Populate the stub's bucket channel with a recoverable object history.

    The channel is written in the order the gateway would write it, so the
    ascending-id scan replays it faithfully:

    * a BucketManifest first, carrying the wrapped bucket KEK the Recovery CLI
      needs to admit this channel's CommitRecords (Requirement 15.3);
    * ``alpha`` committed once (generation 0);
    * ``beta`` committed then overwritten (generations 0 then 1 — the higher
      number wins the head);
    * ``gamma`` committed (generation 0) then tombstoned, so it must have no
      head after recovery.

    Returns the :class:`_Scenario` describing the exact post-recovery state the
    assertions check.
    """
    root_key = derive_root_key(_MASTER_KEY)
    bucket_id = uuid.uuid4()
    kek = os.urandom(32)
    kek_wrapped = wrap(root_key, kek)
    kek_version = 1

    manifest = BucketManifest(
        bucket_name="recovery-bucket",
        kek_wrapped=kek_wrapped,
        kek_version=kek_version,
    )
    port.append(
        _BUCKET_CHANNEL_ID,
        f"tgo3-k-{kek_version}",
        bucket_manifest_codec.serialise(manifest),
    )

    def _commit(key: str, generation_number: int) -> uuid.UUID:
        generation_id = uuid.uuid4()
        record = CommitRecord(
            generation_id=generation_id,
            bucket_id=bucket_id,
            key=key,
            generation_number=generation_number,
            blob_manifest_message_id=0,
            etag=f'"{uuid.uuid4().hex}"',
            checksums={"crc32c": "AAAAAA=="},
            content_type="application/octet-stream",
            user_meta={},
            # The ``generations.last_modified`` column is TIMESTAMP WITHOUT TIME
            # ZONE, so the gateway stores naive UTC timestamps here; match that
            # to exercise the same reconstruction insert path the gateway uses.
            last_modified=datetime(2024, 1, 1, 0, 0, 0),
        )
        port.append(
            _BUCKET_CHANNEL_ID,
            f"tgo3-c-{generation_id}",
            commit_record_codec.serialise(record),
        )
        return generation_id

    def _tombstone(key: str, generation_number: int, generation_id: uuid.UUID) -> None:
        tomb = Tombstone(
            generation_id=generation_id,
            bucket_id=bucket_id,
            key=key,
            generation_number=generation_number,
        )
        port.append(
            _BUCKET_CHANNEL_ID,
            f"tgo3-t-{generation_id}",
            tombstone_codec.serialise(tomb),
        )

    # alpha: single committed generation — head points at it.
    alpha_gen = _commit("alpha", 0)

    # beta: committed then overwritten — the higher generation number wins.
    _commit("beta", 0)
    beta_gen1 = _commit("beta", 1)

    # gamma: committed then deleted — no head after recovery.
    gamma_gen = _commit("gamma", 0)
    _tombstone("gamma", 0, gamma_gen)

    return _Scenario(
        bucket_id=bucket_id,
        kek_wrapped=kek_wrapped,
        kek_version=kek_version,
        expected_heads={"alpha": alpha_gen, "beta": beta_gen1},
        tombstoned_keys={"gamma"},
        expected_generations=4,
        expected_tombstones=1,
        tombstoned_generation_id=gamma_gen,
    )


# ---------------------------------------------------------------------------
# PostgreSQL helpers
# ---------------------------------------------------------------------------


def _async_url(sync_url: str) -> str:
    """Normalise a container connection URL to the ``asyncpg`` driver.

    ``PostgresContainer.get_connection_url`` returns a sync URL (often naming
    psycopg2). The gateway only ships ``asyncpg``, so rewrite the scheme to
    match — the same normalisation ``env.py`` performs on ``DATABASE_URL``.
    """
    bare = sync_url.split("://", 1)[1]
    return "postgresql+asyncpg://" + bare


def _run_alembic_upgrade(async_url: str) -> None:
    """Run ``alembic upgrade head`` through the project's real ``env.py``.

    Creates the schema the rebuild writes into, exactly as the gateway does at
    startup. Runs from the repo root so the relative ``script_location``
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


async def _seed_bucket_row(engine: AsyncEngine, scenario: _Scenario) -> None:
    """Insert the ``buckets`` row the reconstructed rows reference.

    ``generations`` and ``object_heads`` carry foreign keys to ``buckets.id``.
    The bucket's identity and wrapped KEK come from the Recovery Kit, not from a
    channel replay, so the row is seeded directly before the rebuild — modelling
    an operator who has the kit but has lost the object index.
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
                "id": scenario.bucket_id,
                "name": "recovery-bucket",
                "channel_id": _BUCKET_CHANNEL_ID,
                "kek_wrapped": scenario.kek_wrapped,
                "kek_version": scenario.kek_version,
                "durability": "async",
                "created_by_us": True,
                "status": "active",
            },
        )


async def _wipe_index(engine: AsyncEngine) -> None:
    """Delete every ``object_heads`` and ``generations`` row.

    Models total loss of the derived object index while the durable channel
    contents (and the seeded ``buckets`` row from the kit) remain.
    """
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM object_heads"))
        await conn.execute(text("DELETE FROM generations"))


async def _read_heads(engine: AsyncEngine) -> dict[str, uuid.UUID]:
    """Return ``key -> generation_id`` for the bucket's ``object_heads`` rows."""
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT key, generation_id FROM object_heads ORDER BY key")
        )
        return {row.key: row.generation_id for row in result}


async def _generation_state(engine: AsyncEngine, generation_id: uuid.UUID) -> str | None:
    """Return the ``state`` of a reconstructed generation, or ``None`` if absent."""
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT state FROM generations WHERE id = :id"),
            {"id": generation_id},
        )
        row = result.first()
        return None if row is None else str(row.state)


# ---------------------------------------------------------------------------
# Driver: full round-trip under one container
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _RoundTripResult:
    """The observable outcome of one full recovery round-trip."""

    scenario: _Scenario
    heads: dict[str, uuid.UUID]
    reconstructed_generations: int
    applied_tombstones: int
    skipped_messages: int
    gap_count: int
    tombstoned_generation_state: str | None


async def _run_round_trip(async_url: str) -> _RoundTripResult:
    """Seed, wipe, rebuild and read back the index over one engine.

    Encapsulates the whole async flow so the (synchronous) test functions can
    assert against a frozen snapshot of the result.
    """
    port = ReplayTelegramPort()
    scenario = _seed_channel(port)

    engine = create_async_engine(async_url)
    try:
        await _seed_bucket_row(engine, scenario)
        await _wipe_index(engine)

        args = RebuildArgs(
            channel_id=_BUCKET_CHANNEL_ID,
            master_key=_MASTER_KEY,
            db_url=async_url,
            system_channel_id=_SYSTEM_CHANNEL_ID,
        )
        result = await run_rebuild(args, cast(TelegramPort, port))

        heads = await _read_heads(engine)
        tomb_state = await _generation_state(
            engine, scenario.tombstoned_generation_id
        )
    finally:
        await engine.dispose()

    return _RoundTripResult(
        scenario=scenario,
        heads=heads,
        reconstructed_generations=result.reconstructed_generations,
        applied_tombstones=result.applied_tombstones,
        skipped_messages=result.skipped_messages,
        gap_count=result.gap_count,
        tombstoned_generation_state=tomb_state,
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
def round_trip() -> Iterator[_RoundTripResult]:
    """Start PostgreSQL 17, run a full recovery round-trip, yield the result.

    Module-scoped so the expensive container start, migration run and rebuild
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
        result = asyncio.run(_run_round_trip(async_url))
        yield result
    finally:
        container.stop()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_heads_point_at_surviving_winning_generations(
    round_trip: _RoundTripResult,
) -> None:
    """object_heads is rebuilt to the surviving winning generation per key.

    After wiping the index and replaying the channel from Telegram alone, the
    reconstructed ``object_heads`` holds exactly the live keys, each pointing at
    the highest-numbered non-deleted generation (Requirement 15.1).

    Validates: Requirements 15.1
    """
    assert round_trip.heads == round_trip.scenario.expected_heads


def test_tombstoned_key_has_no_head(round_trip: _RoundTripResult) -> None:
    """A tombstoned key is absent from the rebuilt object_heads.

    The gamma key was committed and then tombstoned in the channel; recovery
    from Commit Records and Tombstones alone must leave it with no head.

    Validates: Requirements 15.1
    """
    for key in round_trip.scenario.tombstoned_keys:
        assert key not in round_trip.heads


def test_tombstoned_generation_marked_deleted(
    round_trip: _RoundTripResult,
) -> None:
    """The tombstoned generation is reconstructed and marked DELETED.

    The Commit Record for gamma is reconstructed as a generation row, and the
    following Tombstone marks that same row DELETED rather than removing it, so
    the delete is itself durable in the rebuilt index (Requirement 15.1).

    Validates: Requirements 15.1
    """
    assert round_trip.tombstoned_generation_state == "DELETED"


def test_reconstruction_counts_match(round_trip: _RoundTripResult) -> None:
    """The rebuild reports the expected generation and tombstone counts.

    Recovery reports the number of reconstructed generations and applied
    tombstones; the four Commit Records and one matching Tombstone replayed must
    be reflected exactly (Requirement 15.4).

    Validates: Requirements 15.4
    """
    assert (
        round_trip.reconstructed_generations
        == round_trip.scenario.expected_generations
    )
    assert round_trip.applied_tombstones == round_trip.scenario.expected_tombstones


def test_no_skips_or_gaps_for_contiguous_channel(
    round_trip: _RoundTripResult,
) -> None:
    """A clean, contiguous channel reconstructs with no skips and no gaps.

    Every message in the seeded channel is a readable format message laid down
    on consecutive ids, so recovery reports zero skipped/unreadable messages and
    zero gaps (Requirement 15.4).

    Validates: Requirements 15.4
    """
    assert round_trip.skipped_messages == 0
    assert round_trip.gap_count == 0

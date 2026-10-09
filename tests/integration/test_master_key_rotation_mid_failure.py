"""Mid-rotation failure integration test for the master key rotation CLI.

This integration test exercises task 37.2 against task 37.1's
:func:`cli.rotate_master_key.run_rotation`. It drives the rotation over a *real*
PostgreSQL 17 instance (started with ``testcontainers[postgres]``) and the
in-memory :class:`~tests.integration.fault_injection.FaultInjectingTelegramPort`
so a Telegram write can be made to fail at a precise point during the run — with
no network and no Telethon.

Scenario (Requirements 13.8, 13.9; design "Master Key Rotation Atomicity")
---------------------------------------------------------------------------
Rotation re-wraps every bucket KEK under a new master key and publishes a fresh
:class:`~gateway.app.format.types.BucketManifest` to each bucket's channel, then
updates the index row — one bucket at a time, Telegram first. Telegram offers no
cross-channel transaction, so a failure partway through must leave a *clean
split*: buckets already processed keep their new wrapping, buckets not yet
reached keep their old wrapping, and the operator is told exactly which is which
so a re-run can finish the remainder (Requirement 13.9).

The test seeds several buckets whose stored ``kek_wrapped`` unwraps under the
*old* root key, then:

1. Injects a Telegram ``send_document`` failure on the BucketManifest write for
   one bucket in the middle of the (name-ordered) run.
2. Runs the rotation and asserts it stops with ``completed == False`` and an
   actionable error that names the bucket it stopped at and lists the buckets
   left under the old key (Requirement 13.9 reporting).
3. Reads the index back and asserts the split holds against the durable key
   material: every rotated bucket's stored wrapping unwraps under the **new**
   root key (and no longer under the old), while every not-yet-rotated bucket's
   wrapping still unwraps under the **old** root key — the previous wrapping is
   retained for manifests not yet updated.
4. Clears the fault and re-runs the rotation; asserts it now completes with
   every bucket rotated, the already-rotated prefix skipped, and every stored
   wrapping unwrappable under the new root key — proving the run is resumable.

The suite needs a Docker daemon to launch PostgreSQL; when no container runtime
is available it skips cleanly rather than failing, so ``make lint`` /
``make typecheck`` stay meaningful without a daemon present. The Telegram side
never touches the network — it reuses the fault-injection harness from task 38.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
from cli.rotate_master_key import RotationArgs, RotationResult, run_rotation
from gateway.app.format.key_wrap import KeyUnwrapError, derive_root_key, unwrap, wrap
from gateway.app.telegram.port import TelegramPort
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

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

#: Old and new master keys. Both satisfy the >=16 char contract the key
#: hierarchy expects; they only need to differ so a rotation is well-defined.
_OLD_MASTER_KEY = "old-master-key-rotation-00001"
_NEW_MASTER_KEY = "new-master-key-rotation-00002"

#: Bucket names in the deterministic order the rotation processes them
#: (``_load_buckets`` orders by name). The injected failure lands on the third
#: bucket's manifest write, splitting the five buckets into two rotated, one
#: failed-at, and two never-reached.
_BUCKET_NAMES = ["bucket-a", "bucket-b", "bucket-c", "bucket-d", "bucket-e"]

#: 1-indexed position (by name order) of the bucket whose manifest write fails.
_FAIL_AT_BUCKET = 3


# ---------------------------------------------------------------------------
# Scenario construction
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _SeededBucket:
    """A bucket seeded under the old key, with its channel in the fake port."""

    bucket_id: uuid.UUID
    name: str
    channel_id: int
    #: The plaintext KEK; its wrapping changes on rotation but this stays fixed.
    kek: bytes
    #: The KEK version recorded on the seeded (pre-rotation) index row.
    kek_version: int


#: First channel id handed to the seeded buckets. The fake port creates a
#: channel implicitly on the first ``send_document`` to it, so the ids only need
#: to be distinct and stable; they are assigned here rather than via
#: :meth:`create_channel` because the rotation never creates channels.
_FIRST_CHANNEL_ID = 7000


def _seed_buckets() -> list[_SeededBucket]:
    """Build the per-bucket plaintext KEKs and channel ids (not the index rows).

    Each bucket gets a distinct random KEK and a distinct channel id so a
    rotation can publish a fresh BucketManifest per channel. The index rows are
    written separately by :func:`_seed_bucket_rows` once the engine exists. The
    fake port materialises a channel implicitly on the first send, so the ids
    are simply assigned contiguously here.
    """
    seeded: list[_SeededBucket] = []
    for offset, name in enumerate(_BUCKET_NAMES):
        seeded.append(
            _SeededBucket(
                bucket_id=uuid.uuid4(),
                name=name,
                channel_id=_FIRST_CHANNEL_ID + offset,
                kek=os.urandom(32),
                kek_version=1,
            )
        )
    return seeded


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

    Creates the schema the rotation reads and writes, exactly as the gateway
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


async def _seed_bucket_rows(
    engine: AsyncEngine, buckets: list[_SeededBucket], old_root_key: bytes
) -> None:
    """Insert one ``buckets`` row per seeded bucket, wrapped under the old key.

    The stored ``kek_wrapped`` is the bucket's KEK wrapped by the *old* root key
    so the rotation classifies every bucket as "unwraps under the old key →
    rotate it now" on the first run.
    """
    async with engine.begin() as conn:
        for b in buckets:
            await conn.execute(
                text(
                    "INSERT INTO buckets "
                    "(id, name, channel_id, kek_wrapped, kek_version, durability, "
                    " created_by_us, status) "
                    "VALUES (:id, :name, :channel_id, :kek_wrapped, :kek_version, "
                    " :durability, :created_by_us, :status)"
                ),
                {
                    "id": b.bucket_id,
                    "name": b.name,
                    "channel_id": b.channel_id,
                    "kek_wrapped": wrap(old_root_key, b.kek),
                    "kek_version": b.kek_version,
                    "durability": "async",
                    "created_by_us": True,
                    "status": "active",
                },
            )


async def _read_wrapped_keys(engine: AsyncEngine) -> dict[str, tuple[bytes, int]]:
    """Return ``bucket_name -> (kek_wrapped, kek_version)`` from the index."""
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT name, kek_wrapped, kek_version FROM buckets ORDER BY name")
        )
        return {
            row.name: (bytes(row.kek_wrapped), int(row.kek_version))
            for row in result
        }


# ---------------------------------------------------------------------------
# Classification helper
# ---------------------------------------------------------------------------


def _unwraps_under(root_key: bytes, wrapped: bytes) -> bool:
    """Return whether ``wrapped`` unwraps cleanly under ``root_key``.

    AES Key Wrap integrity makes a clean unwrap an unambiguous "this is the key
    that protects the material" signal, which is exactly how the test tells a
    not-yet-rotated bucket (old key) from a rotated one (new key).
    """
    try:
        unwrap(root_key, wrapped)
    except KeyUnwrapError:
        return False
    return True


# ---------------------------------------------------------------------------
# Driver: mid-rotation failure then resume, over one container
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _RotationRunResult:
    """The observable outcome of the mid-failure run and the resume run."""

    buckets: list[_SeededBucket]
    old_root_key: bytes
    new_root_key: bytes

    # First (failing) run.
    first: RotationResult
    first_wrapped: dict[str, tuple[bytes, int]]

    # Second (resumed, clean) run.
    second: RotationResult
    second_wrapped: dict[str, tuple[bytes, int]]


async def _run_scenario(async_url: str) -> _RotationRunResult:
    """Seed, inject a mid-run failure, read back, then resume cleanly.

    Encapsulates the whole async flow so the (synchronous) test functions assert
    against a frozen snapshot. The same fake port is reused across both runs so
    the manifests the first run durably wrote remain visible to the second —
    exactly how a real operator's re-run sees Telegram's retained state.
    """
    old_root_key = derive_root_key(_OLD_MASTER_KEY)
    new_root_key = derive_root_key(_NEW_MASTER_KEY)

    port = FaultInjectingTelegramPort()
    buckets = _seed_buckets()

    engine = create_async_engine(async_url)
    try:
        await _seed_bucket_rows(engine, buckets, old_root_key)

        args = RotationArgs(
            old_master_key=_OLD_MASTER_KEY,
            new_master_key=_NEW_MASTER_KEY,
            db_url=async_url,
        )

        # The rotation sends exactly one BucketManifest (``tgo3-k-``) document
        # per bucket, in name order, and makes no other sends, so the Nth
        # ``send_document`` call is the Nth bucket's manifest write. Failing on
        # call _FAIL_AT_BUCKET lands the failure squarely in the middle of the
        # run, after the earlier buckets have been durably rotated.
        port.fail_after(_FAIL_AT_BUCKET)
        first = await run_rotation(args, cast(TelegramPort, port))
        first_wrapped = await _read_wrapped_keys(engine)

        # Resume: no faults configured now, so the remainder rotates and the
        # already-rotated prefix is skipped.
        second = await run_rotation(args, cast(TelegramPort, port))
        second_wrapped = await _read_wrapped_keys(engine)
    finally:
        await engine.dispose()

    return _RotationRunResult(
        buckets=buckets,
        old_root_key=old_root_key,
        new_root_key=new_root_key,
        first=first,
        first_wrapped=first_wrapped,
        second=second,
        second_wrapped=second_wrapped,
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
def rotation_run() -> Iterator[_RotationRunResult]:
    """Start PostgreSQL 17, run the mid-failure + resume scenario, yield it.

    Module-scoped so the expensive container start, migration run and two
    rotation runs happen once for every assertion in this file. Skips cleanly
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
        result = asyncio.run(_run_scenario(async_url))
        yield result
    finally:
        container.stop()


# ---------------------------------------------------------------------------
# Tests — the failing run
# ---------------------------------------------------------------------------


def test_rotation_stops_incomplete_on_mid_run_failure(
    rotation_run: _RotationRunResult,
) -> None:
    """The run stops with completed == False when a manifest write fails.

    A Telegram failure on the third bucket's manifest write halts the rotation;
    the result must flag the run incomplete so the process exits non-zero and
    the operator knows a re-run is needed (Requirement 13.9).

    Validates: Requirements 13.9
    """
    assert rotation_run.first.completed is False


def test_rotation_reports_which_buckets_updated_and_not(
    rotation_run: _RotationRunResult,
) -> None:
    """The result names exactly the rotated vs not-rotated buckets.

    The run processes buckets in name order and fails on the third manifest
    write, so the first two rotate and the last three (the one it failed on plus
    the two it never reached) remain under the old key. The result's per-bucket
    outcomes must reflect that split precisely so the operator sees which
    manifests were and were not updated (Requirement 13.9).

    Validates: Requirements 13.9
    """
    rotated = {o.bucket_name for o in rotation_run.first.rotated}
    not_rotated = {o.bucket_name for o in rotation_run.first.not_rotated}

    expected_rotated = set(_BUCKET_NAMES[: _FAIL_AT_BUCKET - 1])
    expected_not_rotated = set(_BUCKET_NAMES[_FAIL_AT_BUCKET - 1 :])

    assert rotated == expected_rotated
    assert not_rotated == expected_not_rotated
    # Every bucket is accounted for exactly once across the two categories.
    assert rotated | not_rotated == set(_BUCKET_NAMES)
    assert rotated & not_rotated == set()


def test_rotation_surfaces_actionable_error(
    rotation_run: _RotationRunResult,
) -> None:
    """The incomplete run carries an actionable error naming the remainder.

    The error must name the bucket the run stopped at and list the buckets still
    under the old key, so the operator can re-run with the same keys to finish
    the remainder (Requirement 13.9 reporting).

    Validates: Requirements 13.9
    """
    error = rotation_run.first.error
    assert error is not None
    # Names the bucket it stopped at and every unrotated bucket.
    for name in _BUCKET_NAMES[_FAIL_AT_BUCKET - 1 :]:
        assert name in error
    # Points the operator at the resumable remedy.
    assert "re-run" in error.lower()


def test_not_rotated_buckets_retain_old_wrapping(
    rotation_run: _RotationRunResult,
) -> None:
    """Buckets not yet updated still unwrap under the old master key.

    The previous key wrapping is retained for every manifest the failed run did
    not update, so each not-yet-rotated bucket's stored ``kek_wrapped`` must
    still unwrap under the old root key and must *not* unwrap under the new one
    (Requirement 13.9).

    Validates: Requirements 13.9
    """
    for name in _BUCKET_NAMES[_FAIL_AT_BUCKET - 1 :]:
        wrapped, version = rotation_run.first_wrapped[name]
        assert _unwraps_under(rotation_run.old_root_key, wrapped)
        assert not _unwraps_under(rotation_run.new_root_key, wrapped)
        # The index row is untouched: still at the original KEK version.
        assert version == 1


def test_rotated_buckets_use_new_wrapping(
    rotation_run: _RotationRunResult,
) -> None:
    """Buckets updated before the failure unwrap under the new master key.

    Each bucket rotated before the failure must have its stored ``kek_wrapped``
    unwrap under the new root key (and no longer under the old), with its KEK
    version bumped — the durable proof the re-wrapping took effect
    (Requirement 13.8).

    Validates: Requirements 13.8
    """
    for name in _BUCKET_NAMES[: _FAIL_AT_BUCKET - 1]:
        wrapped, version = rotation_run.first_wrapped[name]
        assert _unwraps_under(rotation_run.new_root_key, wrapped)
        assert not _unwraps_under(rotation_run.old_root_key, wrapped)
        assert version == 2


def test_rotated_wrapping_preserves_kek_plaintext(
    rotation_run: _RotationRunResult,
) -> None:
    """Re-wrapping changes the wrapping only, never the KEK plaintext.

    A rotated bucket's new wrapping must unwrap to the exact same KEK bytes the
    old wrapping protected, so no object data needs re-encryption — the core
    invariant of the rotation design.

    Validates: Requirements 13.8
    """
    by_name = {b.name: b for b in rotation_run.buckets}
    for name in _BUCKET_NAMES[: _FAIL_AT_BUCKET - 1]:
        wrapped, _ = rotation_run.first_wrapped[name]
        recovered = unwrap(rotation_run.new_root_key, wrapped)
        assert recovered == by_name[name].kek


# ---------------------------------------------------------------------------
# Tests — the resumed (clean) run
# ---------------------------------------------------------------------------


def test_resumed_run_completes(rotation_run: _RotationRunResult) -> None:
    """Re-running after the fault clears completes the rotation.

    With no fault configured, the second run finishes every remaining bucket and
    reports the whole rotation complete (Requirement 13.9 resumability).

    Validates: Requirements 13.9
    """
    assert rotation_run.second.completed is True
    assert rotation_run.second.not_rotated == ()


def test_resumed_run_skips_already_rotated_prefix(
    rotation_run: _RotationRunResult,
) -> None:
    """The resume skips buckets the first run already rotated.

    The two buckets rotated by the first run are recognised as already under the
    new key and skipped; only the three left under the old key are rotated now
    (Requirement 13.9 idempotent re-run).

    Validates: Requirements 13.9
    """
    already = {o.bucket_name for o in rotation_run.second.already_rotated}
    rotated_now = {o.bucket_name for o in rotation_run.second.rotated}

    assert already == set(_BUCKET_NAMES[: _FAIL_AT_BUCKET - 1])
    assert rotated_now == set(_BUCKET_NAMES[_FAIL_AT_BUCKET - 1 :])


def test_all_buckets_use_new_wrapping_after_resume(
    rotation_run: _RotationRunResult,
) -> None:
    """After the resume, every bucket unwraps under the new master key.

    The end state of a completed rotation is that no bucket remains under the
    old key: every stored ``kek_wrapped`` unwraps under the new root key, does
    not unwrap under the old, and recovers the original KEK plaintext
    (Requirements 13.8, 13.9).

    Validates: Requirements 13.8
    """
    by_name = {b.name: b for b in rotation_run.buckets}
    for name in _BUCKET_NAMES:
        wrapped, version = rotation_run.second_wrapped[name]
        assert _unwraps_under(rotation_run.new_root_key, wrapped)
        assert not _unwraps_under(rotation_run.old_root_key, wrapped)
        assert version == 2
        assert unwrap(rotation_run.new_root_key, wrapped) == by_name[name].kek

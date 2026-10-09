"""Unit tests for the staging crash-recovery sweep.

Covers the startup crash-recovery path in
:mod:`gateway.app.storage.staging` (Requirement 14.2 / design "Crash Recovery"):

* :func:`quarantine_orphans` moves staging files with no backing generation row
  into the quarantine subdirectory and leaves known files in place.
* :func:`reenqueue_interrupted_uploads` inserts exactly one ``QUEUED``
  ``upload_jobs`` row for each generation the index reports as interrupted
  (``STAGED`` / ``UPLOADING`` with no job), and inserts nothing when there are
  none — the idempotent-skip case, since the "already has a job" filter lives in
  the SQL the fake session serves.
* :func:`crash_recovery_scan` composes the two halves and reports what changed
  via :class:`StagingRecoveryResult`.

The file scan runs against a real temporary directory. The database is replaced
with a small fake session: the re-enqueue issues a single ``select`` (whose rows
are the interrupted generation ids) and then ``add`` + ``flush`` for each new
job, so the fake records added objects and serves a configured id list without
having to emulate PostgreSQL query semantics.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, cast

import pytest
from gateway.app.db.models import Generation, UploadJob
from gateway.app.storage import staging as staging_mod
from gateway.app.storage.generations import JOB_STATE_QUEUED
from gateway.app.storage.staging import (
    StagingRecoveryResult,
    crash_recovery_scan,
    quarantine_orphans,
    reenqueue_interrupted_uploads,
)
from sqlalchemy.ext.asyncio import AsyncSession

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _IdResult:
    """Result stand-in for the interrupted-generation ``select``.

    ``reenqueue_interrupted_uploads`` reads the rows via ``.all()`` and takes
    ``row[0]`` from each, mirroring a ``select(Generation.id)`` row shape.
    """

    def __init__(self, ids: list[uuid.UUID]) -> None:
        self._ids = ids

    def all(self) -> list[tuple[uuid.UUID]]:
        return [(gen_id,) for gen_id in self._ids]


class _FakeSession:
    """Minimal AsyncSession stand-in for the re-enqueue path.

    Serves the single interrupted-generation ``select`` from a fixed id list and
    records every object handed to ``add`` so a test can assert exactly which
    upload jobs were inserted.
    """

    def __init__(self, interrupted_ids: list[uuid.UUID]) -> None:
        self._interrupted_ids = interrupted_ids
        self.added: list[Any] = []
        self.flushes = 0
        self.executes = 0

    async def execute(self, statement: Any) -> _IdResult:
        self.executes += 1
        return _IdResult(self._interrupted_ids)

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flushes += 1


# ---------------------------------------------------------------------------
# quarantine_orphans
# ---------------------------------------------------------------------------


async def test_quarantine_orphans_moves_unknown_and_keeps_known(
    tmp_path: Path,
) -> None:
    """Unknown files are quarantined; known files stay put."""
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    known = staging_dir / "known.bin"
    orphan = staging_dir / "orphan.bin"
    known.write_bytes(b"keep me")
    orphan.write_bytes(b"move me")

    async def is_known(path: Path) -> bool:
        return path.name == "known.bin"

    moved = await quarantine_orphans(staging_dir, is_known)

    quarantine_dir = staging_dir / staging_mod.QUARANTINE_DIRNAME
    assert moved == [quarantine_dir / "orphan.bin"]
    assert known.exists()
    assert not orphan.exists()
    assert (quarantine_dir / "orphan.bin").read_bytes() == b"move me"


async def test_quarantine_orphans_absent_dir_is_noop(tmp_path: Path) -> None:
    """A missing staging directory yields an empty result, not an error."""

    async def is_known(path: Path) -> bool:  # pragma: no cover - never called
        return True

    assert await quarantine_orphans(tmp_path / "missing", is_known) == []


# ---------------------------------------------------------------------------
# reenqueue_interrupted_uploads
# ---------------------------------------------------------------------------


async def test_reenqueue_inserts_one_queued_job_per_interrupted_generation() -> None:
    """Each interrupted generation gets exactly one fresh QUEUED upload job."""
    gen_a = uuid.uuid4()
    gen_b = uuid.uuid4()
    session = _FakeSession([gen_a, gen_b])

    reenqueued = await reenqueue_interrupted_uploads(cast(AsyncSession, session))

    assert reenqueued == [gen_a, gen_b]
    jobs = [obj for obj in session.added if isinstance(obj, UploadJob)]
    assert len(jobs) == 2
    assert {job.generation_id for job in jobs} == {gen_a, gen_b}
    assert all(job.state == JOB_STATE_QUEUED for job in jobs)
    assert all(job.attempts == 0 for job in jobs)
    assert all(job.next_attempt_at is None for job in jobs)
    # New rows are flushed so the transaction surfaces constraint errors eagerly.
    assert session.flushes == 1


async def test_reenqueue_with_nothing_interrupted_inserts_nothing() -> None:
    """When no generation is interrupted, the sweep adds and flushes nothing.

    This is the idempotent-skip case: a clean restart (or one where every
    STAGED/UPLOADING generation still owns its job) re-enqueues no work.
    """
    session = _FakeSession([])

    reenqueued = await reenqueue_interrupted_uploads(cast(AsyncSession, session))

    assert reenqueued == []
    assert session.added == []
    assert session.flushes == 0


# ---------------------------------------------------------------------------
# crash_recovery_scan (composition)
# ---------------------------------------------------------------------------


async def test_crash_recovery_scan_quarantines_and_reenqueues(
    tmp_path: Path,
) -> None:
    """The scan both moves orphaned files and re-enqueues interrupted uploads.

    The ``is_known`` predicate is injected (treating every staging file as
    unknown so it is quarantined) rather than built from the fake session, which
    keeps the file scan free of real-``AsyncSession`` plumbing; the fake session
    still serves the re-enqueue select with one interrupted generation.
    """
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    orphan = staging_dir / "orphan.bin"
    orphan.write_bytes(b"orphaned")

    gen = uuid.uuid4()
    session = _FakeSession([gen])

    async def nothing_known(path: Path) -> bool:
        return False

    result = await crash_recovery_scan(
        cast(AsyncSession, session),
        staging_dir=staging_dir,
        is_known=nothing_known,
    )

    assert isinstance(result, StagingRecoveryResult)
    quarantine_dir = staging_dir / staging_mod.QUARANTINE_DIRNAME
    assert result.quarantined == (quarantine_dir / "orphan.bin",)
    assert not orphan.exists()
    assert result.reenqueued == (gen,)
    jobs = [obj for obj in session.added if isinstance(obj, UploadJob)]
    assert len(jobs) == 1
    assert jobs[0].generation_id == gen
    assert jobs[0].state == JOB_STATE_QUEUED


async def test_crash_recovery_scan_without_staging_dir_only_reenqueues() -> None:
    """With no staging_dir, the file scan is skipped and only re-enqueue runs."""
    gen = uuid.uuid4()
    session = _FakeSession([gen])

    result = await crash_recovery_scan(cast(AsyncSession, session))

    assert result.quarantined == ()
    assert result.reenqueued == (gen,)


def test_staging_recovery_result_defaults_empty() -> None:
    """A fresh result reports nothing changed."""
    result = StagingRecoveryResult()
    assert result.quarantined == ()
    assert result.reenqueued == ()


def test_generation_model_import_is_stable() -> None:
    """The re-enqueue references the real ORM models (guards against drift)."""
    assert Generation.__tablename__ == "generations"
    assert UploadJob.__tablename__ == "upload_jobs"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))

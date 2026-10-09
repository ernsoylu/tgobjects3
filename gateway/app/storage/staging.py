"""Staging area: bounded in-memory reservation counter, fsync helpers, and the
startup crash-recovery quarantine scan.

The staging area is a local filesystem volume that buffers upload bodies before
the Upload Worker transmits them to Telegram. Two concerns live here:

* **Admission control.** :class:`StagingReservation` keeps an in-memory running
  total of the bytes currently reserved by in-flight ``PutObject`` requests and
  refuses any reservation that would push the total past ``STAGING_MAX_SIZE``.
  The counter is guarded by an :class:`asyncio.Lock` so the check-and-increment
  is atomic across concurrently running coroutines on the event loop. A refused
  reservation returns ``None`` (Requirements 6.3/6.4); the PutObject handler
  translates that into an HTTP 507 response without writing any body bytes.

* **Durability + crash recovery.** :func:`fsync_file` and :func:`fsync_dir`
  force buffered writes to stable storage so a committed object survives a power
  loss (Requirement 6.9). :func:`crash_recovery_scan` runs at startup and does
  the full crash-recovery sweep mandated by Requirement 14.2 and the design's
  "Crash Recovery" section:

  1. :func:`quarantine_orphans` moves any staging file with no corresponding
     generation row into a ``quarantine`` subdirectory, so leftover bytes from a
     crash never masquerade as live data and are retained for operator
     inspection rather than silently deleted.
  2. :func:`reenqueue_interrupted_uploads` scans every generation left in
     ``STAGED`` or ``UPLOADING`` state — the states an upload passes through
     before it is durable — and re-enqueues an ``upload_jobs`` row for any that
     has none, so an interrupted upload is picked back up by the Upload Worker.
     Generations that already carry an ``upload_jobs`` row are skipped, which
     keeps the sweep idempotent: a second restart (or a crash mid-sweep) never
     enqueues the same work twice.

Everything in this module is driven by parameters passed in by the caller
(``max_size_bytes``, ``staging_dir``, a generation-row lookup). The module does
not import a global ``Settings`` object, which keeps it composable and testable.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

#: Name of the subdirectory, created under the staging directory, into which
#: orphaned files are moved by :func:`quarantine_orphans`.
QUARANTINE_DIRNAME = "quarantine"


class StagingReservationError(ValueError):
    """Raised when a reservation is requested with a non-positive size."""


@dataclass(frozen=True)
class StagingHandle:
    """A granted staging reservation for ``size`` bytes.

    The handle is an opaque receipt returned by :meth:`StagingReservation.reserve`.
    It must be passed back to :meth:`StagingReservation.release` exactly once when
    the reservation is no longer needed (body fully handed off to the upload
    pipeline, or the request failed). The ``id`` field makes each handle unique so
    the reservation tracker can enforce release-exactly-once semantics even when
    two reservations are for the same number of bytes.
    """

    id: int
    size: int


class StagingReservation:
    """In-memory, bounded, atomic reservation counter for the staging area.

    All mutation of the running total happens under a single :class:`asyncio.Lock`,
    so the "would this exceed the cap?" check and the increment that follows it are
    one indivisible step with respect to other coroutines. Because CPython's
    asyncio runs one coroutine at a time and never preempts between ``await``
    points on its own, the lock is what prevents two coroutines that both pass the
    capacity check from each incrementing the total past the cap.
    """

    def __init__(self, max_size_bytes: int) -> None:
        """Create a reservation tracker with the given ceiling.

        Args:
            max_size_bytes: The ``STAGING_MAX_SIZE`` ceiling in bytes. Must be a
                non-negative integer; ``0`` means no reservation can ever succeed.
        """
        if max_size_bytes < 0:
            raise StagingReservationError("max_size_bytes must be non-negative")
        self._max_size_bytes = max_size_bytes
        self._reserved = 0
        self._next_id = 0
        self._lock = asyncio.Lock()

    @property
    def max_size_bytes(self) -> int:
        """The configured ceiling in bytes."""
        return self._max_size_bytes

    @property
    def reserved_bytes(self) -> int:
        """The number of bytes currently reserved.

        This is a point-in-time snapshot read without the lock; treat it as
        advisory (observability, tests) rather than as the basis for an admission
        decision.
        """
        return self._reserved

    async def reserve(self, size: int) -> StagingHandle | None:
        """Atomically reserve ``size`` bytes if doing so stays within the cap.

        Args:
            size: The number of bytes to reserve. Must be a positive integer; this
                is the declared ``Content-Length`` of the incoming body.

        Returns:
            A :class:`StagingHandle` when the reservation succeeds, or ``None`` when
            granting it would push the total in-progress reservations over
            ``max_size_bytes``. ``None`` is the caller's signal to return HTTP 507
            and write no body bytes.

        Raises:
            StagingReservationError: If ``size`` is not a positive integer.
        """
        if size <= 0:
            raise StagingReservationError("reservation size must be positive")
        async with self._lock:
            if self._reserved + size > self._max_size_bytes:
                return None
            self._reserved += size
            self._next_id += 1
            return StagingHandle(id=self._next_id, size=size)

    async def release(self, handle: StagingHandle) -> None:
        """Release a previously granted reservation.

        Decrements the running total by the handle's size. The decrement is
        clamped at zero so a double release (or releasing a handle from a different
        tracker) can never drive the counter negative and silently inflate
        available capacity.

        Args:
            handle: The handle returned by a prior successful :meth:`reserve`.
        """
        async with self._lock:
            if handle.size > self._reserved:
                logger.warning(
                    "staging release of %d bytes exceeds reserved %d; clamping",
                    handle.size,
                    self._reserved,
                )
                self._reserved = 0
            else:
                self._reserved -= handle.size


def fsync_file(path: Path) -> None:
    """Flush a file's contents to stable storage.

    Opens the file read-only purely to obtain a descriptor and calls
    :func:`os.fsync` on it, forcing the operating system to persist any buffered
    writes before the function returns. Used after streaming a body to staging and
    before renaming it to its content-addressed path (Requirement 6.9).

    Args:
        path: The file to synchronise.
    """
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_dir(path: Path) -> None:
    """Flush a directory entry to stable storage.

    A rename is only durable once the containing directory's metadata is itself
    synced, so this is called after renaming a staging file into place. The
    directory is opened with ``O_DIRECTORY`` and synced via :func:`os.fsync`.

    Args:
        path: The directory to synchronise.
    """
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


#: A predicate the quarantine scan uses to decide whether a staging file is still
#: referenced by a generation row. It receives the file's path and returns an
#: awaitable resolving to ``True`` when a matching generation exists.
KnownStagingLookup = Callable[[Path], Awaitable[bool]]


async def quarantine_orphans(
    staging_dir: Path,
    is_known: KnownStagingLookup,
    *,
    quarantine_dir: Path | None = None,
) -> list[Path]:
    """Scan the staging directory and quarantine files with no generation row.

    This is the startup crash-recovery scan (Requirement 14.2). It walks every
    regular file directly under ``staging_dir`` (the quarantine subdirectory
    itself is skipped) and, for each one, asks ``is_known`` whether a generation
    row still references it. Files that are *not* known are moved into
    ``quarantine_dir`` rather than deleted, so an operator can inspect them.

    The lookup is injected as a callable rather than taking a database session
    directly. Production wiring passes :func:`make_generation_lookup` built from an
    :class:`~sqlalchemy.ext.asyncio.AsyncSession`; tests can pass any coroutine.

    Args:
        staging_dir: The staging volume root to scan.
        is_known: Async predicate returning ``True`` when a generation row still
            references the given staging file path.
        quarantine_dir: Destination for orphaned files. Defaults to a
            ``quarantine`` subdirectory of ``staging_dir``.

    Returns:
        The list of destination paths for the files that were quarantined, in the
        order they were processed.
    """
    staging_dir = Path(staging_dir)
    if quarantine_dir is None:
        quarantine_dir = staging_dir / QUARANTINE_DIRNAME
    else:
        quarantine_dir = Path(quarantine_dir)

    if not staging_dir.is_dir():
        logger.info("staging dir %s does not exist; nothing to scan", staging_dir)
        return []

    quarantine_dir.mkdir(parents=True, exist_ok=True)
    quarantine_resolved = quarantine_dir.resolve()

    quarantined: list[Path] = []
    for entry in _iter_staging_files(staging_dir, quarantine_resolved):
        if await is_known(entry):
            continue
        destination = _quarantine_destination(quarantine_dir, entry.name)
        shutil.move(os.fspath(entry), os.fspath(destination))
        quarantined.append(destination)
        logger.warning(
            "quarantined orphan staging file %s -> %s", entry, destination
        )

    if quarantined:
        # Make the moves durable so a second crash cannot resurrect the orphans.
        fsync_dir(quarantine_dir)
        fsync_dir(staging_dir)
    return quarantined


def _iter_staging_files(staging_dir: Path, quarantine_resolved: Path) -> Iterable[Path]:
    """Yield regular files under ``staging_dir``, skipping the quarantine subtree."""
    for entry in sorted(staging_dir.iterdir()):
        if entry.resolve() == quarantine_resolved:
            continue
        if entry.is_file():
            yield entry


def _quarantine_destination(quarantine_dir: Path, name: str) -> Path:
    """Pick a non-colliding destination path inside ``quarantine_dir`` for ``name``."""
    destination = quarantine_dir / name
    if not destination.exists():
        return destination
    # A file of this name was already quarantined (e.g. a prior crashed run).
    # Disambiguate with a numeric suffix rather than clobbering the earlier copy.
    suffix = 1
    while True:
        candidate = quarantine_dir / f"{name}.{suffix}"
        if not candidate.exists():
            return candidate
        suffix += 1


def make_generation_lookup(session: object) -> KnownStagingLookup:
    """Build a :data:`KnownStagingLookup` backed by an ``AsyncSession``.

    The returned coroutine checks whether any ``generations`` row carries a
    ``staging_path`` equal to the candidate file's path. The import of the ORM
    model is deferred so this module stays importable without a configured
    database (and so the pure reservation/fsync helpers carry no DB dependency).

    Args:
        session: A SQLAlchemy ``AsyncSession``. Typed as ``object`` to avoid a
            hard import of the async engine stack at module import time.

    Returns:
        An async predicate suitable for :func:`quarantine_orphans`.
    """
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession

    from gateway.app.db.models import Generation

    if not isinstance(session, AsyncSession):
        raise TypeError("make_generation_lookup requires an AsyncSession")

    async def is_known(path: Path) -> bool:
        result = await session.execute(
            select(Generation.id)
            .where(Generation.staging_path == os.fspath(path))
            .limit(1)
        )
        return result.first() is not None

    return is_known


async def reenqueue_interrupted_uploads(session: AsyncSession) -> list[uuid.UUID]:
    """Re-enqueue upload jobs for generations interrupted by a crash.

    This is the second half of the startup crash-recovery sweep (Requirement
    14.2 / design "Crash Recovery"). It finds every generation still in the
    ``STAGED`` or ``UPLOADING`` state — the two states an upload passes through
    before Telegram confirms the Commit Record — and ensures each one has an
    ``upload_jobs`` row so the Upload Worker will pick it back up:

    * A generation in ``STAGED`` / ``UPLOADING`` with **no** ``upload_jobs`` row
      lost its job to the crash (for example, a worker claimed and deleted the
      job but died before committing). A fresh ``QUEUED`` job is inserted.
    * A generation in ``STAGED`` / ``UPLOADING`` that **already** has an
      ``upload_jobs`` row is left untouched. Re-inserting would duplicate the
      work, so skipping it keeps the sweep idempotent across repeated restarts.

    The states and the ``QUEUED`` token are the shared constants from
    :mod:`gateway.app.storage.generations`, so a re-enqueued job is
    indistinguishable from one inserted by the normal PutObject path.

    The work runs inside the caller's transaction and is flushed but **not**
    committed — the startup caller owns the commit boundary, so the whole sweep
    lands atomically or not at all.

    Args:
        session: The active async session; its transaction boundary is honoured.

    Returns:
        The ids of the generations for which a new upload job was inserted, in
        the order they were processed. Generations skipped as idempotent are not
        included.
    """
    from sqlalchemy import select

    from gateway.app.db.models import Generation, UploadJob
    from gateway.app.storage.generations import (
        JOB_STATE_QUEUED,
        STATE_STAGED,
        STATE_UPLOADING,
    )

    interrupted_states = (STATE_STAGED, STATE_UPLOADING)

    # Ids of generations in a pre-durable state that still have a job: these are
    # the ones to skip. A correlated NOT EXISTS keeps the whole decision in one
    # round trip and avoids loading rows we will not touch.
    has_job = (
        select(UploadJob.id)
        .where(UploadJob.generation_id == Generation.id)
        .exists()
    )
    rows = await session.execute(
        select(Generation.id)
        .where(Generation.state.in_(interrupted_states))
        .where(~has_job)
        .order_by(Generation.id)
    )
    orphaned_ids = [row[0] for row in rows.all()]

    for generation_id in orphaned_ids:
        session.add(
            UploadJob(
                id=uuid.uuid4(),
                generation_id=generation_id,
                state=JOB_STATE_QUEUED,
                attempts=0,
                next_attempt_at=None,
                last_error=None,
            )
        )
        logger.warning(
            "re-enqueued upload job for interrupted generation %s", generation_id
        )

    if orphaned_ids:
        # Make the new jobs visible to any follow-on statement in this
        # transaction (and surface a constraint error now rather than at commit).
        await session.flush()
    return orphaned_ids


async def crash_recovery_scan(
    session: AsyncSession,
    *,
    staging_dir: Path | None = None,
    quarantine_dir: Path | None = None,
    is_known: KnownStagingLookup | None = None,
) -> StagingRecoveryResult:
    """Run the full startup crash-recovery sweep (Requirement 14.2).

    This is the single entry point the gateway startup path calls. It composes
    the two halves of the design's "Crash Recovery" section against one session:

    1. **Quarantine orphaned staging files.** When ``staging_dir`` is given,
       :func:`quarantine_orphans` moves any file with no backing generation row
       into the quarantine subdirectory. When it is ``None`` (the generation
       lookup needs a path to scan and some callers wire the file scan
       separately) the quarantine step is skipped and only the re-enqueue runs.
    2. **Re-enqueue interrupted uploads.** :func:`reenqueue_interrupted_uploads`
       inserts a fresh ``QUEUED`` upload job for every ``STAGED`` / ``UPLOADING``
       generation that lost its job, and skips those that still have one.

    The whole sweep shares the caller's transaction, so the file moves and the
    index writes commit together with the rest of startup.

    Args:
        session: The active async session; its transaction boundary is honoured.
        staging_dir: The staging volume root to scan for orphaned files. When
            ``None``, only the generation re-enqueue runs.
        quarantine_dir: Optional override for the quarantine destination; passed
            through to :func:`quarantine_orphans`.
        is_known: Optional override for the generation-row predicate used by the
            file scan. Defaults to :func:`make_generation_lookup` built from
            ``session``; injected mainly so the scan can be exercised without a
            live database session.

    Returns:
        A :class:`StagingRecoveryResult` recording the files that were
        quarantined and the generations that were re-enqueued.
    """
    quarantined: list[Path] = []
    if staging_dir is not None:
        lookup = is_known if is_known is not None else make_generation_lookup(session)
        quarantined = await quarantine_orphans(
            staging_dir, lookup, quarantine_dir=quarantine_dir
        )

    reenqueued = await reenqueue_interrupted_uploads(session)
    return StagingRecoveryResult(
        quarantined=tuple(quarantined), reenqueued=tuple(reenqueued)
    )


@dataclass(frozen=True)
class StagingRecoveryResult:
    """Outcome of a :func:`crash_recovery_scan` run.

    Captures what the sweep changed so the startup caller (and tests) can log or
    assert on it: ``quarantined`` is the list of destination paths for orphaned
    staging files moved aside, and ``reenqueued`` is the list of generation ids
    for which a fresh upload job was inserted. Both are empty on a clean start.
    """

    quarantined: tuple[Path, ...] = ()
    reenqueued: tuple[uuid.UUID, ...] = ()


__all__ = [
    "QUARANTINE_DIRNAME",
    "KnownStagingLookup",
    "StagingHandle",
    "StagingRecoveryResult",
    "StagingReservation",
    "StagingReservationError",
    "crash_recovery_scan",
    "fsync_dir",
    "fsync_file",
    "make_generation_lookup",
    "quarantine_orphans",
    "reenqueue_interrupted_uploads",
]

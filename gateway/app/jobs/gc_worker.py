"""The GC Worker — orphan sweep, multipart expiry, and the delete sweep.

Garbage collection is the gateway's deferred reclamation path. The write path
never deletes anything from Telegram synchronously: a DeleteObject writes a
Tombstone and an overwrite advances the head, but the superseded data (chunks,
blob manifest, commit record) and the now-dead tombstone stay in Telegram, and
abandoned multipart parts stay on the staging disk. The GC Worker is the
background task that reclaims all of it, after a grace period, so a mistaken
delete can still be recovered until ``GC_GRACE`` elapses.

Mirroring :class:`~gateway.app.jobs.upload_worker.UploadWorker` and
:class:`~gateway.app.jobs.checkpoint_worker.CheckpointWorker`, this worker
exposes :meth:`run_once` (one full sweep — the unit the loop and tests both
drive), :meth:`run_forever` (``run_once`` on a fixed cadence until
:meth:`stop`), and :meth:`stop` (request a clean exit).

Responsibilities
----------------
A single :meth:`run_once` performs three independent sweeps in order; a failure
in one is logged and does not abort the others, because none of them is on the
object write path:

1. **Multipart expiry** (Requirement 10.10). Every ``multipart_uploads`` row
   whose ``expires_at`` is in the past received neither CompleteMultipartUpload
   nor AbortMultipartUpload within ``MULTIPART_EXPIRY``. The worker deletes each
   staged part file, then removes the ``multipart_parts`` and
   ``multipart_uploads`` rows. No Telegram messages exist for an incomplete
   multipart upload (parts live only on staging until completion), so this sweep
   touches staging and the index only.

2. **Delete sweep** (design "Generation State Machine": ``SUPERSEDED -> PURGED``
   / ``DELETED -> PURGED``). Every SUPERSEDED or DELETED generation whose state
   last changed more than ``GC_GRACE`` seconds ago is purged: its ``tgo3``
   messages (commit record and, for a delete generation, tombstone) are deleted
   from Telegram, the blob it referenced has its refcount decremented, and a
   blob that reaches refcount 0 has its own ``tgo3`` messages (data chunks and
   blob manifest) deleted and its ``chunks`` rows removed. The generation row is
   then marked PURGED and **retained** for audit (design: "PURGED generations
   are retained in PostgreSQL for audit; only the Telegram messages are
   deleted"). Any staging file still attached to the generation is removed too.

3. **Orphan staging sweep.** Any multipart part file left on disk whose
   ``multipart_parts`` row has already gone (e.g. a crash between the row delete
   and the unlink of a prior sweep) is removed. This is a best-effort cleanup of
   dangling bytes; the authoritative crash-recovery quarantine of PutObject
   bodies lives in :mod:`gateway.app.storage.staging`.

Grace reference point
---------------------
The schema (design "PostgreSQL Schema") carries no dedicated ``superseded_at``
column, so the delete sweep uses the generation's ``last_modified`` timestamp as
the "no references since" marker: the write that superseded or deleted a
generation stamps ``last_modified`` at that moment, so ``now - last_modified >=
GC_GRACE`` is exactly "dereferenced for at least ``GC_GRACE``".

Transaction boundaries
----------------------
Like the other workers, this module owns its transactions. Each purge is its own
``session.begin()`` block: the Telegram deletes happen first (they are the
durability-affecting step), and only once they succeed does the matching index
mutation commit, so a crash mid-sweep leaves a generation still SUPERSEDED (it
will be re-swept) rather than a PURGED row whose messages were never deleted.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gateway.app.db.models import (
    Blob,
    Bucket,
    Chunk,
    Generation,
    MultipartPart,
    MultipartUpload,
)
from gateway.app.storage import blobs as blobs_storage
from gateway.app.storage import generations as generations_storage
from gateway.app.telegram.errors import TelegramError
from gateway.app.telegram.port import TelegramPort

logger = logging.getLogger(__name__)

__all__ = [
    "GC_STATES",
    "GCSweepResult",
    "GCWorker",
]

#: Generation states eligible for the delete sweep. A generation is a GC
#: candidate once it has been dereferenced — superseded by a newer write or
#: deleted via a Tombstone — and the grace timer measures from that transition.
GC_STATES: tuple[str, ...] = (
    generations_storage.STATE_SUPERSEDED,
    generations_storage.STATE_DELETED,
)


def _utcnow() -> datetime:
    """Return the current time as a timezone-*naive* UTC datetime.

    The ``multipart_uploads.expires_at`` and ``generations.last_modified`` columns
    this worker filters on are ``TIMESTAMP WITHOUT TIME ZONE``, and asyncpg refuses
    to bind a tz-aware value against such a column ("can't subtract offset-naive
    and offset-aware datetimes"). Every other writer in the gateway
    (``multipart.py``, ``put_object.py``, ``upload_worker.py``,
    ``cache/store.py``) therefore stores naive UTC; the delete-sweep cutoff
    (``now - GC_GRACE``) and the expiry comparison must use the same naive UTC so
    the comparisons line up with what those columns hold.
    """
    return datetime.now(UTC).replace(tzinfo=None)


@dataclass
class GCSweepResult:
    """Counters describing what a single :meth:`GCWorker.run_once` reclaimed.

    Returned by :meth:`GCWorker.run_once` so the background loop can log a sweep
    summary and tests can assert on the work done without inspecting the
    database. Every field defaults to zero so a no-op sweep is reported as such.

    Attributes:
        multipart_aborted: Expired multipart uploads aborted (Requirement 10.10).
        parts_deleted: Staged multipart part files deleted across all sweeps.
        generations_purged: Generations advanced to PURGED by the delete sweep.
        blobs_purged: Blobs that reached refcount 0 and had their chunks and
            blob manifest deleted from Telegram.
        telegram_messages_deleted: ``tgo3`` messages deleted from Telegram.
        orphan_files_deleted: Dangling staging files removed by the orphan sweep.
        errors: Human-readable descriptions of per-item failures that were logged
            and skipped rather than aborting the sweep.
    """

    multipart_aborted: int = 0
    parts_deleted: int = 0
    generations_purged: int = 0
    blobs_purged: int = 0
    telegram_messages_deleted: int = 0
    orphan_files_deleted: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _ExpiredUpload:
    """An expired multipart upload and the staged part files to delete.

    Attributes:
        upload_id: The ``multipart_uploads`` primary key.
        part_paths: Staging paths of every ``multipart_parts`` row for the upload.
    """

    upload_id: uuid.UUID
    part_paths: tuple[str, ...]


@dataclass(frozen=True)
class _PurgeTarget:
    """A dereferenced generation and the Telegram messages backing it.

    Attributes:
        generation_id: The generation to advance to PURGED.
        channel_id: The bucket channel the generation's messages live in.
        commit_message_id: The Commit Record / Tombstone message id, or ``None``
            when the generation never recorded one.
        staging_path: A staging file still attached to the generation, or
            ``None``.
        blob_id: The blob the generation referenced, or ``None`` for a delete
            generation with no blob.
    """

    generation_id: uuid.UUID
    channel_id: int
    commit_message_id: int | None
    staging_path: str | None
    blob_id: uuid.UUID | None


class GCWorker:
    """Reclaims expired multipart uploads, dead generations, and orphan files.

    Owns a session factory and a :class:`TelegramPort`. :meth:`run_once` performs
    one complete sweep (multipart expiry, delete sweep, orphan staging sweep) and
    returns a :class:`GCSweepResult`; :meth:`run_forever` repeats it every
    ``interval_seconds`` until :meth:`stop`. Each reclaimed item is handled in its
    own transaction so a failure is isolated and the item is simply re-swept next
    cycle.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        telegram: TelegramPort,
        *,
        gc_grace_seconds: int,
        multipart_expiry_seconds: int,
        staging_dir: str | None = None,
        interval_seconds: float = 3600.0,
    ) -> None:
        """Create a GC Worker.

        Args:
            session_factory: Factory producing ``AsyncSession`` instances; each
                reclaimed item opens its own short transaction.
            telegram: The Telegram boundary used to delete ``tgo3`` messages.
            gc_grace_seconds: ``GC_GRACE`` — retention (seconds) a dereferenced
                generation must age before its Telegram messages are deleted.
                Must be >= 0.
            multipart_expiry_seconds: ``MULTIPART_EXPIRY`` — recorded on each
                ``multipart_uploads.expires_at``; this worker only reads that
                column, but the value is kept for logging and symmetry. Must be
                >= 0.
            staging_dir: Root of the staging volume, scanned by the orphan sweep
                for dangling multipart part files. When ``None`` the orphan sweep
                is skipped (the two index-driven sweeps still run).
            interval_seconds: Seconds between sweeps in :meth:`run_forever`; must
                be > 0.
        """
        if gc_grace_seconds < 0:
            raise ValueError("gc_grace_seconds must be >= 0")
        if multipart_expiry_seconds < 0:
            raise ValueError("multipart_expiry_seconds must be >= 0")
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be > 0")
        self._session_factory = session_factory
        self._telegram = telegram
        self._gc_grace = timedelta(seconds=gc_grace_seconds)
        self._multipart_expiry_seconds = multipart_expiry_seconds
        self._staging_dir = staging_dir
        self._interval_seconds = interval_seconds
        self._stopping = False

    def stop(self) -> None:
        """Request :meth:`run_forever` to exit after the current sweep."""
        self._stopping = True

    async def run_forever(self) -> None:
        """Run a sweep every ``interval_seconds`` until :meth:`stop`.

        A sweep never raises (per-item failures are caught, logged, and recorded
        in the :class:`GCSweepResult`), so the loop keeps its cadence and the
        object write path is never disturbed.
        """
        while not self._stopping:
            await self.run_once()
            if self._stopping:
                break
            await asyncio.sleep(self._interval_seconds)

    async def run_once(self) -> GCSweepResult:
        """Perform one full GC sweep and return what it reclaimed.

        Runs the three sweeps in order — multipart expiry (Requirement 10.10),
        delete sweep (``SUPERSEDED``/``DELETED`` -> ``PURGED``), orphan staging
        sweep — accumulating results into one :class:`GCSweepResult`. Each sweep
        isolates and logs its own failures, so a problem in one never prevents
        the others from running.

        Returns:
            A :class:`GCSweepResult` with the counts and any per-item errors.
        """
        result = GCSweepResult()
        await self._sweep_expired_multipart(result)
        await self._sweep_dead_generations(result)
        await self._sweep_orphan_staging(result)

        logger.info(
            "gc sweep: %d multipart aborted, %d generations purged, "
            "%d blobs purged, %d telegram messages deleted, "
            "%d staged parts deleted, %d orphan files deleted, %d errors",
            result.multipart_aborted,
            result.generations_purged,
            result.blobs_purged,
            result.telegram_messages_deleted,
            result.parts_deleted,
            result.orphan_files_deleted,
            len(result.errors),
        )
        return result

    # -- sweep 1: multipart expiry (Requirement 10.10) --------------------

    async def _sweep_expired_multipart(self, result: GCSweepResult) -> None:
        """Abort every multipart upload past ``MULTIPART_EXPIRY`` and purge parts.

        Finds all ``multipart_uploads`` whose ``expires_at`` is in the past —
        meaning neither CompleteMultipartUpload nor AbortMultipartUpload arrived
        within ``MULTIPART_EXPIRY`` — and, for each, deletes its staged part files
        and removes the ``multipart_parts`` and ``multipart_uploads`` rows
        (Requirement 10.10). An incomplete multipart upload has no Telegram
        messages (parts live only on staging until completion), so this sweep is
        staging- and index-only.
        """
        try:
            expired = await self._load_expired_uploads()
        except Exception as exc:  # noqa: BLE001 - a GC sweep must never crash the loop
            logger.error("gc: failed to load expired multipart uploads: %s", exc)
            result.errors.append(f"load expired multipart uploads: {exc}")
            return

        for upload in expired:
            try:
                for path in upload.part_paths:
                    if _unlink_if_present(path):
                        result.parts_deleted += 1
                async with self._session_factory() as session, session.begin():
                    await session.execute(
                        delete(MultipartPart).where(
                            MultipartPart.upload_id == upload.upload_id
                        )
                    )
                    await session.execute(
                        delete(MultipartUpload).where(
                            MultipartUpload.upload_id == upload.upload_id
                        )
                    )
                result.multipart_aborted += 1
                logger.info(
                    "gc: aborted expired multipart upload %s (%d parts)",
                    upload.upload_id,
                    len(upload.part_paths),
                )
            except Exception as exc:  # noqa: BLE001 - isolate per-upload failures
                logger.error(
                    "gc: failed to abort multipart upload %s: %s",
                    upload.upload_id,
                    exc,
                )
                result.errors.append(
                    f"abort multipart upload {upload.upload_id}: {exc}"
                )

    async def _load_expired_uploads(self) -> list[_ExpiredUpload]:
        """Load every expired multipart upload with its staged part paths."""
        now = _utcnow()
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(MultipartUpload.upload_id).where(
                        MultipartUpload.expires_at <= now
                    )
                )
            ).scalars().all()

            uploads: list[_ExpiredUpload] = []
            for upload_id in rows:
                part_paths = (
                    await session.execute(
                        select(MultipartPart.staging_path).where(
                            MultipartPart.upload_id == upload_id
                        )
                    )
                ).scalars().all()
                uploads.append(
                    _ExpiredUpload(
                        upload_id=upload_id,
                        part_paths=tuple(part_paths),
                    )
                )
        return uploads

    # -- sweep 2: delete sweep (SUPERSEDED/DELETED -> PURGED) --------------

    async def _sweep_dead_generations(self, result: GCSweepResult) -> None:
        """Purge dereferenced generations whose grace period has elapsed.

        Loads every SUPERSEDED/DELETED generation older than ``GC_GRACE`` and
        purges each in its own transaction: delete its Telegram messages, drop
        the blob reference (purging the blob when it hits refcount 0), remove any
        attached staging file, and mark the generation PURGED while retaining its
        row for audit.
        """
        try:
            targets = await self._load_purge_targets()
        except Exception as exc:  # noqa: BLE001 - a GC sweep must never crash the loop
            logger.error("gc: failed to load purge targets: %s", exc)
            result.errors.append(f"load purge targets: {exc}")
            return

        for target in targets:
            try:
                await self._purge_generation(target, result)
            except Exception as exc:  # noqa: BLE001 - isolate per-generation failures
                logger.error(
                    "gc: failed to purge generation %s: %s",
                    target.generation_id,
                    exc,
                )
                result.errors.append(
                    f"purge generation {target.generation_id}: {exc}"
                )

    async def _load_purge_targets(self) -> list[_PurgeTarget]:
        """Load dereferenced generations past ``GC_GRACE`` with their context.

        A generation qualifies when its state is SUPERSEDED or DELETED and its
        ``last_modified`` (the moment it was dereferenced) is at least
        ``GC_GRACE`` in the past. Each target carries the bucket channel and the
        message/staging/blob identifiers the purge needs.
        """
        cutoff = _utcnow() - self._gc_grace
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(
                        Generation.id,
                        Bucket.channel_id,
                        Generation.commit_message_id,
                        Generation.staging_path,
                        Generation.blob_id,
                    )
                    .join(Bucket, Bucket.id == Generation.bucket_id)
                    .where(
                        Generation.state.in_(GC_STATES),
                        Generation.last_modified <= cutoff,
                    )
                )
            ).all()
        return [
            _PurgeTarget(
                generation_id=row[0],
                channel_id=row[1],
                commit_message_id=row[2],
                staging_path=row[3],
                blob_id=row[4],
            )
            for row in rows
        ]

    async def _purge_generation(
        self, target: _PurgeTarget, result: GCSweepResult
    ) -> None:
        """Purge one dereferenced generation: Telegram, blob, staging, then row.

        Order matters for crash safety. The generation's own ``tgo3`` message
        (Commit Record or Tombstone) is deleted from Telegram first, then — if the
        generation held the last reference to its blob — every generation still
        referencing that blob has its ``blob_id`` cleared (so the retained-for-
        audit PURGED rows no longer pin the blob via the ``generations.blob_id``
        foreign key), the blob's data chunks and manifest are deleted and its
        ``chunks`` rows removed, then any attached staging file is unlinked, and
        only then does the row flip to PURGED and the refcount decrement commit.
        A crash before the final transaction leaves the generation
        SUPERSEDED/DELETED, so the next sweep simply retries (deleting an
        already-absent Telegram message is a no-op).
        """
        # 1. Delete the generation's own message (commit record / tombstone).
        if target.commit_message_id is not None:
            await self._delete_messages(
                target.channel_id, [target.commit_message_id], result
            )

        # 2. Decrement the referenced blob and, if it is now unreferenced, delete
        #    its Telegram data and chunk rows. All index mutations commit together
        #    with the PURGED flip in one transaction.
        async with self._session_factory() as session, session.begin():
            purge_blob_messages: list[int] = []
            blob_fully_purged = False
            if target.blob_id is not None:
                new_refcount = await blobs_storage.decrement_refcount(
                    session, target.blob_id
                )
                if new_refcount <= 0:
                    purge_blob_messages = await self._collect_blob_messages(
                        session, target.blob_id
                    )
                    # Retained PURGED generations keep their row for audit, so
                    # any generation still pointing at this blob (including the
                    # one being purged now) must have its ``blob_id`` cleared
                    # before the blob row is deleted — otherwise the
                    # ``generations.blob_id`` foreign key blocks the DELETE.
                    await session.execute(
                        update(Generation)
                        .where(Generation.blob_id == target.blob_id)
                        .values(blob_id=None)
                    )
                    await session.execute(
                        delete(Chunk).where(Chunk.blob_id == target.blob_id)
                    )
                    await session.execute(
                        delete(Blob).where(Blob.id == target.blob_id)
                    )
                    blob_fully_purged = True

            await generations_storage.mark_purged(session, target.generation_id)

        # 3. Delete the blob's data chunks + manifest from Telegram (outside the
        #    transaction: the rows are already gone, and a message delete is a
        #    no-op if retried, so an error here only costs orphaned Telegram
        #    bytes, never index inconsistency).
        if purge_blob_messages:
            await self._delete_messages(
                target.channel_id, purge_blob_messages, result
            )
        if blob_fully_purged:
            result.blobs_purged += 1

        # 4. Remove any staging file still attached to the generation.
        if target.staging_path is not None and _unlink_if_present(
            target.staging_path
        ):
            result.parts_deleted += 1

        result.generations_purged += 1
        logger.info("gc: purged generation %s", target.generation_id)

    async def _collect_blob_messages(
        self, session: AsyncSession, blob_id: uuid.UUID
    ) -> list[int]:
        """Collect a blob's data-chunk and manifest Telegram message ids.

        Reads every ``chunks.message_id`` for the blob plus the blob's
        ``manifest_message_id``; these are the ``tgo3-d-*`` and ``tgo3-b-*``
        messages deleted when the blob reaches refcount 0.
        """
        chunk_ids = (
            await session.execute(
                select(Chunk.message_id).where(Chunk.blob_id == blob_id)
            )
        ).scalars().all()
        message_ids = [int(mid) for mid in chunk_ids]

        manifest_message_id = (
            await session.execute(
                select(Blob.manifest_message_id).where(Blob.id == blob_id)
            )
        ).scalar_one_or_none()
        if manifest_message_id is not None:
            message_ids.append(int(manifest_message_id))
        return message_ids

    async def _delete_messages(
        self, channel_id: int, message_ids: list[int], result: GCSweepResult
    ) -> None:
        """Delete Telegram messages, tallying the count or recording a failure.

        A :class:`TelegramError` is caught and recorded rather than raised: the
        index mutation that matters has either already committed (blob rows) or
        is yet to commit (generation flip, which will simply be retried), so a
        failed delete only leaves orphaned Telegram bytes to be reclaimed on a
        later sweep.
        """
        if not message_ids:
            return
        try:
            await self._telegram.delete_messages(channel_id, message_ids)
            result.telegram_messages_deleted += len(message_ids)
        except TelegramError as exc:
            logger.warning(
                "gc: failed to delete %d messages from channel %d: %s",
                len(message_ids),
                channel_id,
                exc,
            )
            result.errors.append(
                f"delete {len(message_ids)} messages from channel {channel_id}: {exc}"
            )

    # -- sweep 3: orphan staging files ------------------------------------

    async def _sweep_orphan_staging(self, result: GCSweepResult) -> None:
        """Remove multipart part files on disk with no ``multipart_parts`` row.

        Best-effort cleanup of dangling bytes left by a crash between a row delete
        and the matching unlink. Scans ``staging_dir`` (when configured) and
        unlinks any regular file whose path is not referenced by a surviving
        ``multipart_parts`` row. PutObject staging bodies are handled by the
        startup quarantine scan in :mod:`gateway.app.storage.staging`, so this
        sweep only reclaims files the quarantine would otherwise ignore.
        """
        if self._staging_dir is None:
            return
        try:
            known = await self._known_part_paths()
        except Exception as exc:  # noqa: BLE001 - a GC sweep must never crash the loop
            logger.error("gc: failed to load known part paths: %s", exc)
            result.errors.append(f"load known part paths: {exc}")
            return

        try:
            entries = list(os.scandir(self._staging_dir))
        except FileNotFoundError:
            return
        except OSError as exc:
            logger.error("gc: failed to scan staging dir: %s", exc)
            result.errors.append(f"scan staging dir: {exc}")
            return

        for entry in entries:
            if not entry.is_file(follow_symlinks=False):
                continue
            if entry.path in known:
                continue
            if _unlink_if_present(entry.path):
                result.orphan_files_deleted += 1
                logger.info("gc: removed orphan staging file %s", entry.path)

    async def _known_part_paths(self) -> set[str]:
        """Return the set of staging paths referenced by live multipart parts."""
        async with self._session_factory() as session:
            paths = (
                await session.execute(select(MultipartPart.staging_path))
            ).scalars().all()
        return {str(path) for path in paths}


def _unlink_if_present(path: str) -> bool:
    """Unlink ``path`` if it exists; return whether a file was removed.

    A missing file is treated as already reclaimed (``False`` return, no error),
    which keeps the sweeps idempotent under retries. Any other ``OSError`` is
    logged and swallowed so one unreadable file cannot abort a sweep.
    """
    try:
        os.unlink(path)
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning("gc: failed to unlink %s: %s", path, exc)
        return False

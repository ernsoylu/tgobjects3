"""The Upload Worker — moves a STAGED generation to COMMITTED via Telegram.

A STAGED generation is a durable-on-local-disk write awaiting transmission to
Telegram, the authoritative store. The Upload Worker is the background task that
performs that transmission and flips the generation to COMMITTED (the only
durable state). It implements the write path's second half from the design:

    [Upload Worker claims] -> dedup check -> seal records ->
    send chunks -> send blob manifest -> send commit record ->
    DB: mark COMMITTED + advance head

and the generation state machine's upload transitions:

    STAGED -> UPLOADING -> COMMITTED            (normal upload)
    STAGED -> COMMITTED                         (dedup shortcut)
    UPLOADING -> RETRY_WAIT -> UPLOADING        (transient Telegram error)
    RETRY_WAIT -> FAILED                        (attempts exhausted)
    UPLOADING -> COMMITTED                      (partial-commit recovery)
    UPLOADING -> UPLOAD_FAILED                  (commit-record recovery failed)

Partial commit record recovery (Requirement 14.3)
-------------------------------------------------
Crash recovery re-enqueues STAGED/UPLOADING generations (task 36.1 /
Requirement 14.2), so a resumed job may be one the gateway crashed on *between*
writing the Blob Manifest and writing the Commit Record. The data chunks and the
manifest are already durable in Telegram, but the generation has no Commit
Record, so it is not yet recoverable. :meth:`UploadWorker._recover_partial_commit`
handles this case: before re-uploading anything, the worker inspects the bucket
channel for the generation. If a Commit Record (``tgo3-c-<generation_id>``)
already exists the DB is simply reconciled to COMMITTED; if only the Blob
Manifest exists (its decoded ``sha256`` matching the generation's), the worker
re-attempts *only* the Commit Record write — reusing the already-uploaded
manifest and chunks — up to :data:`MAX_COMMIT_RECORD_RECOVERY_ATTEMPTS` times
with the same exponential backoff the normal path uses. On success the generation
advances to COMMITTED as usual; if all attempts fail the generation is set to
UPLOAD_FAILED and a :class:`CommitRecordRecoveryError` is surfaced to the
operator.

Job claim (Requirement 14.1)
----------------------------
:func:`claim_job` selects one QUEUED upload job joined to its STAGED generation
with ``FOR UPDATE SKIP LOCKED``, so N concurrent workers each grab a distinct
job without blocking on one another and without ever double-claiming. The claim
runs in its own short transaction that also moves the generation to UPLOADING
and the job to the in-flight state, making the claim visible to peers before any
slow Telegram I/O begins.

Dedup (Requirements 6.10, 6.11)
-------------------------------
Before uploading, the worker takes the content-addressed advisory lock for the
generation's SHA-256 (via :func:`gateway.app.storage.blobs.claim_or_wait`). The
lock serialises every worker handling identical content, so the "concurrent
same-SHA-256" case collapses to: the first worker uploads while the rest block
inside ``claim_or_wait``; when it commits the blob and releases the lock, the
waiters wake, observe the now-COMMITTED blob as a dedup hit, and reuse it without
re-uploading. A dedup hit (first or subsequent) skips Telegram entirely and only
writes a generation row + head advance referencing the existing blob.

Message ordering (Requirements 6.12, 6.13)
-------------------------------------------
On the normal path the worker seals every record with AES-256-GCM in a thread
pool (``asyncio.to_thread``; sealing is CPU-bound), sends the data chunk
documents in parallel up to ``UPLOAD_CONCURRENCY``, then sends the BlobManifest,
then — last — the CommitRecord. The CommitRecord's confirmed ``message_id`` is
the durability boundary: only once Telegram acknowledges it does the worker mark
the generation COMMITTED and advance the object head, in one transaction.

Retry and backoff (Requirement 6.14)
-------------------------------------
A Telegram error during upload moves the generation to RETRY_WAIT, increments the
job's attempt counter, and schedules the next attempt after an exponential
backoff that starts at :data:`INITIAL_BACKOFF_SECONDS` and doubles each attempt.
A :class:`~gateway.app.telegram.errors.FloodWaitError` overrides the computed
backoff with Telegram's requested wait. After :data:`MAX_UPLOAD_ATTEMPTS`
attempts the generation is marked FAILED; the staged file is retained for
inspection or operator-initiated retry.

Transaction boundaries
----------------------
This module owns its transactions (unlike the storage-layer helpers, which are
caller-owned). Each distinct step — claim, commit, retry-schedule, fail — is its
own ``session.begin()`` block so a crash between steps leaves a consistent,
re-enqueueable state rather than a half-applied transaction.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gateway.app.db.models import Blob, Bucket, Chunk, Generation, UploadJob
from gateway.app.format import blob_manifest as blob_manifest_codec
from gateway.app.format import commit_record as commit_record_codec
from gateway.app.format import record_codec
from gateway.app.format.key_wrap import derive_root_key, unwrap, wrap
from gateway.app.format.types import BlobManifest, CommitRecord
from gateway.app.storage import blobs as blobs_storage
from gateway.app.storage import generations as generations_storage
from gateway.app.telegram.errors import FloodWaitError, TelegramError
from gateway.app.telegram.port import TelegramMessage, TelegramPort

logger = logging.getLogger(__name__)

__all__ = [
    "BLOB_KEY_SIZE",
    "INITIAL_BACKOFF_SECONDS",
    "JOB_STATE_FAILED",
    "JOB_STATE_UPLOADING",
    "MAX_COMMIT_RECORD_RECOVERY_ATTEMPTS",
    "MAX_UPLOAD_ATTEMPTS",
    "ClaimedJob",
    "CommitRecordRecoveryError",
    "UploadWorker",
    "backoff_seconds",
    "claim_job",
]

# --- Caption scheme -----------------------------------------------------------
# Mirrors the design's "Telegram Message Format" table and the prefixes the
# recovery CLI (cli/rebuild.py) classifies on. The suffix carries the logical
# identity (blob id, chunk index, or generation id).
_PREFIX_DATA = "tgo3-d-"
_PREFIX_BLOB_MANIFEST = "tgo3-b-"
_PREFIX_COMMIT = "tgo3-c-"

# --- Upload-job states --------------------------------------------------------
#: An upload job claimed by a worker and currently being transmitted. Distinct
#: from the generation's UPLOADING state so a job can be filtered by lifecycle
#: independently of the generation it drives.
JOB_STATE_UPLOADING = "UPLOADING"
#: An upload job whose generation exhausted its retry budget.
JOB_STATE_FAILED = "FAILED"

# --- Retry policy -------------------------------------------------------------
#: First retry backoff interval, in seconds. Doubles each subsequent attempt.
INITIAL_BACKOFF_SECONDS = 1.0
#: Total number of upload attempts before a generation is marked FAILED.
MAX_UPLOAD_ATTEMPTS = 5
#: Times the Commit Record write is re-attempted during partial-commit-record
#: recovery before the generation is set to UPLOAD_FAILED (Requirement 14.3).
MAX_COMMIT_RECORD_RECOVERY_ATTEMPTS = 5

# --- Caption-scan tuning ------------------------------------------------------
#: Consecutive message-id window requested per Telegram ``get_messages`` call
#: when scanning a bucket channel for a generation's manifest/commit messages.
_RECOVERY_SCAN_BATCH_SIZE = 200
#: Number of consecutive wholly-empty id windows that ends a recovery scan. A run
#: of empty windows past the last message signals the tail of the channel; the
#: value matches the recovery CLI's ``_EMPTY_BATCH_STOP`` so both scanners agree
#: on when a channel has been fully walked.
_RECOVERY_EMPTY_BATCH_STOP = 3

# --- Crypto -------------------------------------------------------------------
#: Size of the per-blob AES-256 key, in bytes.
BLOB_KEY_SIZE = 32


class CommitRecordRecoveryError(TelegramError):
    """Partial-commit-record recovery exhausted its Commit Record re-attempts.

    Raised by :meth:`UploadWorker._recover_partial_commit` when a Blob Manifest
    for a generation is confirmed present in Telegram but the Commit Record could
    not be written after :data:`MAX_COMMIT_RECORD_RECOVERY_ATTEMPTS` backed-off
    attempts (Requirement 14.3). The generation has already been set to
    UPLOAD_FAILED by the time this is raised; the message names the generation so
    the operator can locate and act on it. It subclasses
    :class:`~gateway.app.telegram.errors.TelegramError` so callers that already
    handle Telegram-boundary failures see it, while remaining distinguishable
    from a transient :class:`~gateway.app.telegram.errors.TelegramUnavailableError`.
    """

    def __init__(self, generation_id: uuid.UUID, attempts: int) -> None:
        self.generation_id = generation_id
        self.attempts = attempts
        super().__init__(
            f"commit record for generation {generation_id} could not be written "
            f"after {attempts} attempts; generation set to UPLOAD_FAILED — the "
            f"blob manifest is durable in Telegram, so a later retry can complete "
            f"the commit without re-uploading data"
        )


def backoff_seconds(attempt: int) -> float:
    """Return the exponential backoff for a given (1-based) attempt number.

    The first retry (``attempt == 1``) waits :data:`INITIAL_BACKOFF_SECONDS`;
    each later attempt doubles the previous interval (1 s, 2 s, 4 s, …). This is
    the computed backoff; a :class:`FloodWaitError` overrides it with Telegram's
    requested wait.

    Args:
        attempt: The 1-based attempt counter the job has reached after the
            failure (i.e. the number of attempts made so far).

    Returns:
        The number of seconds to wait before the next attempt.
    """
    exponent = max(attempt - 1, 0)
    return INITIAL_BACKOFF_SECONDS * (2.0**exponent)


@dataclass(frozen=True)
class ClaimedJob:
    """A single upload job claimed for processing, with its generation context.

    Produced by :func:`claim_job` after the claim transaction has moved the
    generation to UPLOADING. Carries everything the upload path needs without a
    second round-trip: the job and generation ids, the owning bucket's channel
    and wrapped KEK, the key/size/content metadata for the manifest and commit
    record, and the local staging path of the body to transmit.

    Attributes:
        job_id: The ``upload_jobs`` row id.
        generation_id: The generation this job drives to COMMITTED.
        bucket_id: The owning bucket.
        channel_id: The Telegram channel backing the bucket.
        key: The object key being written.
        size: The plaintext object size in bytes.
        sha256: The content digest (dedup key within the bucket).
        kek_wrapped: The bucket KEK wrapped by the root key.
        chunk_records: Records packed into each Telegram data document.
        staging_path: Local path of the fsynced body, or ``None`` for a
            zero-length body (handled as a single empty record).
        content_type: Recorded ``Content-Type`` for later GETs.
        user_meta: User metadata map.
        etag: The object ETag computed by the PutObject handler.
        checksums: Client/computed checksum map.
        last_modified: The generation's last-modified timestamp.
        generation_number: Monotonic per-key generation number.
        attempts: Attempts already made for this job before this claim.
    """

    job_id: uuid.UUID
    generation_id: uuid.UUID
    bucket_id: uuid.UUID
    channel_id: int
    key: str
    size: int
    sha256: bytes
    kek_wrapped: bytes
    chunk_records: int
    staging_path: str | None
    content_type: str | None
    user_meta: dict[str, str]
    etag: str | None
    checksums: dict[str, str]
    last_modified: datetime
    generation_number: int
    attempts: int


@dataclass(frozen=True)
class _RecoveryScanState:
    """What a channel scan found for a generation being crash-recovered.

    Produced by :meth:`UploadWorker._scan_channel_for_generation`. Exactly one of
    two situations is represented:

    * ``manifest is None`` — a Commit Record already exists in the channel for
      the generation; ``commit_message_id`` is its Telegram id and
      ``manifest_message_id`` is the manifest id it was found alongside (``0`` if
      the manifest was not separately located). Recovery only reconciles the DB.
    * ``manifest`` is set — only the Blob Manifest exists; ``manifest_message_id``
      is its Telegram id and ``commit_message_id`` is ``0``. Recovery re-attempts
      the Commit Record write.

    Attributes:
        manifest_message_id: Telegram message id of the Blob Manifest, or ``0``.
        manifest: The decoded Blob Manifest when only it exists, else ``None``.
        commit_message_id: Telegram message id of an already-present Commit
            Record, or ``0`` when none exists.
    """

    manifest_message_id: int
    manifest: BlobManifest | None
    commit_message_id: int


def _utcnow() -> datetime:
    """Return the current UTC time as a naive datetime.

    The ``upload_jobs.next_attempt_at`` column this value is compared against and
    written into is ``TIMESTAMP WITHOUT TIME ZONE`` (see the data model), so the
    worker works in naive UTC throughout: a timezone-aware value would make
    asyncpg reject the ``next_attempt_at <= :now`` claim comparison and the
    retry reschedule with "can't subtract offset-naive and offset-aware
    datetimes". Dropping the tzinfo keeps the value byte-for-byte comparable with
    what the column stores while still being UTC.
    """
    return datetime.now(UTC).replace(tzinfo=None)


async def claim_job(
    session: AsyncSession,
    *,
    default_chunk_records: int = 512,
    sha256_for: dict[uuid.UUID, bytes] | None = None,
) -> ClaimedJob | None:
    """Claim one QUEUED upload job and move its generation to UPLOADING.

    Runs the whole claim inside the caller's transaction. The core statement
    selects a single ``upload_jobs`` row in the QUEUED state whose
    ``next_attempt_at`` is due (``NULL`` or in the past), locking it with
    ``FOR UPDATE SKIP LOCKED`` so concurrent workers never contend for or
    double-claim the same job (Requirement 14.1). On a successful claim the job
    moves to :data:`JOB_STATE_UPLOADING` and the generation to UPLOADING, so the
    claim is visible to peers before any Telegram I/O starts.

    Args:
        session: The active async session; the caller owns the transaction
            boundary (:class:`UploadWorker` wraps this call in ``begin()``).
        default_chunk_records: Records packed per Telegram document, applied to
            the claimed job (``CHUNK_RECORDS``).
        sha256_for: Optional precomputed ``generation_id -> sha256`` map, used
            only by tests that stage generations without a blob row. In
            production the digest is read from the generation's blob metadata.

    Returns:
        A :class:`ClaimedJob` when a job was claimed, or ``None`` when no due
        QUEUED job exists.
    """
    now = _utcnow()
    row = (
        await session.execute(
            select(UploadJob)
            .join(Generation, Generation.id == UploadJob.generation_id)
            .where(
                UploadJob.state == generations_storage.JOB_STATE_QUEUED,
                Generation.state.in_(
                    (
                        generations_storage.STATE_STAGED,
                        generations_storage.STATE_RETRY_WAIT,
                    )
                ),
                (UploadJob.next_attempt_at.is_(None))
                | (UploadJob.next_attempt_at <= now),
            )
            .order_by(UploadJob.next_attempt_at.nulls_first())
            .limit(1)
            .with_for_update(skip_locked=True)
        )
    ).scalar_one_or_none()

    if row is None:
        return None

    generation = (
        await session.execute(
            select(Generation).where(Generation.id == row.generation_id)
        )
    ).scalar_one()

    bucket = (
        await session.execute(
            select(Bucket).where(Bucket.id == generation.bucket_id)
        )
    ).scalar_one()

    sha256 = _resolve_sha256(generation, sha256_for)
    generation_number = await _generation_number(
        session, generation.bucket_id, generation.key
    )

    # Make the claim visible to peers: flip both the job and the generation
    # before the slow upload begins.
    row.state = JOB_STATE_UPLOADING
    await session.execute(
        update(Generation)
        .where(Generation.id == generation.id)
        .values(state=generations_storage.STATE_UPLOADING)
    )

    return ClaimedJob(
        job_id=row.id,
        generation_id=generation.id,
        bucket_id=generation.bucket_id,
        channel_id=bucket.channel_id,
        key=generation.key,
        size=generation.size,
        sha256=sha256,
        kek_wrapped=bucket.kek_wrapped,
        chunk_records=max(default_chunk_records, 1),
        staging_path=generation.staging_path,
        content_type=generation.content_type,
        user_meta=_as_str_map(generation.user_meta),
        etag=generation.etag,
        checksums=_as_str_map(generation.checksums),
        last_modified=generation.last_modified,
        generation_number=generation_number,
        attempts=row.attempts,
    )


async def _generation_number(
    session: AsyncSession, bucket_id: uuid.UUID, key: str
) -> int:
    """Compute the monotonic generation number for the next write to a key.

    Counts the COMMITTED generations already recorded for ``(bucket_id, key)``;
    the new generation is numbered after them. This is the ordinal recorded in
    the Commit Record so recovery can order a key's history.
    """
    from sqlalchemy import func

    count = (
        await session.execute(
            select(func.count())
            .select_from(Generation)
            .where(
                Generation.bucket_id == bucket_id,
                Generation.key == key,
                Generation.state == generations_storage.STATE_COMMITTED,
            )
        )
    ).scalar_one()
    return int(count)


def _resolve_sha256(
    generation: Generation, sha256_for: dict[uuid.UUID, bytes] | None
) -> bytes:
    """Resolve the content digest for a generation.

    The digest is the dedup key. It is sourced from the checksums map recorded
    by the PutObject handler (the ``sha256`` entry, hex-encoded), falling back to
    an explicit ``sha256_for`` map supplied by tests that stage generations with
    no committed blob yet.

    Raises:
        ValueError: If no SHA-256 can be resolved for the generation.
    """
    if sha256_for is not None and generation.id in sha256_for:
        return sha256_for[generation.id]
    checksums = generation.checksums or {}
    hex_digest = checksums.get("sha256")
    if isinstance(hex_digest, str):
        try:
            return bytes.fromhex(hex_digest)
        except ValueError as exc:
            raise ValueError(
                f"generation {generation.id} has a malformed sha256 checksum"
            ) from exc
    raise ValueError(f"generation {generation.id} has no resolvable sha256")


def _as_str_map(value: object) -> dict[str, str]:
    """Coerce a JSONB map column to ``dict[str, str]`` for the format layer.

    ``user_meta`` and ``checksums`` are stored as JSONB and may be ``None``; the
    commit record requires string-to-string maps, so non-string values are
    stringified and ``None`` becomes an empty map.
    """
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items()}


class UploadWorker:
    """Drives STAGED generations to COMMITTED, one claimed job at a time.

    A worker owns a session factory and a :class:`TelegramPort`. :meth:`run_once`
    claims and processes a single job (the unit the background loop and tests
    both drive); :meth:`run_forever` loops over :meth:`run_once`, idling briefly
    when the queue is empty. ``UPLOAD_CONCURRENCY`` workers can share one factory
    and port, each claiming distinct jobs via ``SKIP LOCKED``.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        telegram: TelegramPort,
        master_key: str,
        *,
        upload_concurrency: int = 2,
        default_chunk_records: int = 512,
        max_attempts: int = MAX_UPLOAD_ATTEMPTS,
        idle_sleep_seconds: float = 1.0,
    ) -> None:
        """Create an Upload Worker.

        Args:
            session_factory: Factory producing ``AsyncSession`` instances; each
                step opens its own short transaction.
            telegram: The Telegram boundary for sending documents.
            master_key: ``TGOS3_MASTER_KEY``; the root key is derived from it to
                unwrap bucket KEKs and wrap per-blob keys.
            upload_concurrency: Maximum parallel chunk-document sends per blob
                (``UPLOAD_CONCURRENCY``); must be >= 1.
            default_chunk_records: Records packed per Telegram document when a
                bucket does not override it (``CHUNK_RECORDS``).
            max_attempts: Attempts before a generation is marked FAILED.
            idle_sleep_seconds: Sleep between empty-queue polls in
                :meth:`run_forever`.
        """
        if upload_concurrency < 1:
            raise ValueError("upload_concurrency must be >= 1")
        if default_chunk_records < 1:
            raise ValueError("default_chunk_records must be >= 1")
        self._session_factory = session_factory
        self._telegram = telegram
        self._root_key = derive_root_key(master_key)
        self._upload_concurrency = upload_concurrency
        self._default_chunk_records = default_chunk_records
        self._max_attempts = max_attempts
        self._idle_sleep_seconds = idle_sleep_seconds
        self._stopping = False

    def stop(self) -> None:
        """Request :meth:`run_forever` to exit after the current iteration."""
        self._stopping = True

    async def run_forever(self) -> None:
        """Process jobs until :meth:`stop` is called, idling on an empty queue."""
        while not self._stopping:
            processed = await self.run_once()
            if not processed:
                await asyncio.sleep(self._idle_sleep_seconds)

    async def run_once(self) -> bool:
        """Claim and process at most one upload job.

        A :class:`CommitRecordRecoveryError` from partial-commit-record recovery
        is the one terminal outcome surfaced as an exception from :meth:`_process`
        (Requirement 14.3). It is caught here so the worker loop keeps running:
        the generation has already been moved to UPLOAD_FAILED and the failure
        logged, so the error has been surfaced and the job is done being
        processed — this iteration counts as processed.

        Returns:
            ``True`` if a job was claimed and processed (committed, dedup-reused,
            retried, failed, or recovered/UPLOAD_FAILED), ``False`` if the queue
            had no due job.
        """
        async with self._session_factory() as session:
            async with session.begin():
                job = await claim_job(
                    session, default_chunk_records=self._default_chunk_records
                )
            if job is None:
                return False

        try:
            await self._process(job)
        except CommitRecordRecoveryError as exc:
            logger.error("upload worker surfaced recovery failure: %s", exc)
        return True

    async def _process(self, job: ClaimedJob) -> None:
        """Run the dedup/upload/commit pipeline for one claimed job.

        Before the normal pipeline, a resumed job is checked for the
        partial-commit-record case (Requirement 14.3): a crash between the Blob
        Manifest and the Commit Record leaves the manifest (and chunks) durable
        in Telegram but the generation uncommitted. When that is detected the
        worker recovers by re-writing only the Commit Record and never re-uploads
        the body; see :meth:`_recover_partial_commit`.
        """
        try:
            if await self._recover_partial_commit(job):
                return
        except CommitRecordRecoveryError:
            # Recovery already set the generation UPLOAD_FAILED and the job
            # FAILED; surface the actionable error rather than rescheduling.
            raise
        except TelegramError as exc:
            # A transient failure while *scanning* for the recovery state (not
            # the commit re-attempt, which handles its own retries): reschedule
            # the job like any other transient upload error.
            await self._handle_telegram_error(job, exc)
            return

        async with self._session_factory() as session:
            try:
                async with session.begin():
                    claim = await blobs_storage.claim_or_wait(
                        session, job.bucket_id, job.sha256
                    )
                    try:
                        if claim.is_dedup_hit:
                            assert claim.blob_id is not None
                            await self._commit_dedup(session, job, claim.blob_id)
                            return
                        blob_id = await self._upload_and_commit(session, job)
                        logger.info(
                            "committed generation %s as blob %s",
                            job.generation_id,
                            blob_id,
                        )
                    finally:
                        await blobs_storage.release_claim(session, claim)
            except TelegramError as exc:
                await self._handle_telegram_error(job, exc)

    async def _commit_dedup(
        self, session: AsyncSession, job: ClaimedJob, blob_id: uuid.UUID
    ) -> None:
        """Commit a generation by reusing an existing COMMITTED blob.

        No Telegram upload happens (Requirement 6.10): the generation is pointed
        at ``blob_id``, the blob's refcount is bumped, and the head advances —
        all in the caller's transaction. The commit still needs a Commit Record
        in Telegram to be recoverable, so one is written referencing the existing
        blob's manifest message before the state advance.
        """
        blob = await _load_blob(session, blob_id)
        commit_message_id = await self._send_commit_record(
            job, blob_manifest_message_id=blob.manifest_message_id or 0
        )
        await blobs_storage.increment_refcount(session, blob_id)
        await generations_storage.advance_committed(
            session,
            job.generation_id,
            commit_message_id=commit_message_id,
            blob_id=blob_id,
            etag=job.etag,
            checksums=job.checksums or None,
            last_modified=job.last_modified,
        )
        await _mark_job_done(session, job.job_id)

    async def _upload_and_commit(
        self, session: AsyncSession, job: ClaimedJob
    ) -> uuid.UUID:
        """Seal, transmit, and commit a brand-new blob (Requirements 6.12–6.14).

        Runs on the dedup-miss path with the content advisory lock held by
        :meth:`_process`, so this worker is the single writer for the content.

        Steps, in order: generate and wrap a per-blob key, read the staged body,
        seal every record in a thread pool, send the data chunk documents in
        parallel (bounded by ``UPLOAD_CONCURRENCY``), send the BlobManifest, then
        the CommitRecord. Only after the Commit Record message id is confirmed do
        the blob/chunks/generation rows land and the head advance — one atomic
        transaction (the caller's).

        Returns:
            The id of the newly inserted blob.
        """
        blob_id = uuid.uuid4()
        blob_key = secrets.token_bytes(BLOB_KEY_SIZE)
        bucket_kek = unwrap(self._root_key, job.kek_wrapped)
        key_wrapped = wrap(bucket_kek, blob_key)

        plaintext = _read_staged_body(job.staging_path)
        records = _split_records(plaintext)
        sealed = await asyncio.to_thread(
            _seal_records, records, blob_key, blob_id.bytes, len(plaintext)
        )

        chunk_message_ids = await self._send_chunks(job, blob_id, sealed)

        manifest = BlobManifest(
            format_version=1,
            blob_id=blob_id.bytes,
            bucket_id=job.bucket_id,
            sha256=job.sha256,
            size=job.size,
            chunk_records=job.chunk_records,
            chunk_message_ids=chunk_message_ids,
            key_wrapped=key_wrapped,
        )
        manifest_message_id = await self._telegram.send_document(
            job.channel_id,
            blob_manifest_codec.serialise(manifest),
            file_name=f"{blob_id.hex}.manifest",
            caption=f"{_PREFIX_BLOB_MANIFEST}{blob_id.hex}",
        )

        commit_message_id = await self._send_commit_record(
            job, blob_manifest_message_id=manifest_message_id
        )

        # Durable: persist the blob, its chunks, and advance the generation.
        await blobs_storage.insert_blob(
            session,
            bucket_id=job.bucket_id,
            sha256=job.sha256,
            size=job.size,
            key_wrapped=key_wrapped,
            chunk_records=job.chunk_records,
            refcount=1,
            manifest_message_id=manifest_message_id,
            blob_id=blob_id,
        )
        _insert_chunks(session, blob_id, job.chunk_records, len(records), chunk_message_ids)
        # Flush the blob (and chunks) before advancing the generation: the
        # session factory runs with ``autoflush=False``, so the Core UPDATE in
        # ``advance_committed`` that sets ``generations.blob_id`` would otherwise
        # execute while the blob INSERT is still pending and trip the
        # ``fk_generations_blob_id_blobs`` foreign key. Flushing makes the blob
        # row visible for the FK check within this same transaction.
        await session.flush()
        await generations_storage.advance_committed(
            session,
            job.generation_id,
            commit_message_id=commit_message_id,
            blob_id=blob_id,
            etag=job.etag,
            checksums=job.checksums or None,
            last_modified=job.last_modified,
        )
        await _mark_job_done(session, job.job_id)
        return blob_id

    async def _send_chunks(
        self, job: ClaimedJob, blob_id: uuid.UUID, sealed: list[bytes]
    ) -> list[int]:
        """Send sealed records as chunk documents, bounded by UPLOAD_CONCURRENCY.

        Records are grouped ``chunk_records`` at a time into one document each.
        Documents are sent concurrently, but no more than ``UPLOAD_CONCURRENCY``
        at once (an :class:`asyncio.Semaphore` gate). The returned message ids are
        ordered by chunk index so the manifest records them in blob order.
        """
        groups = _group_records(sealed, job.chunk_records)
        semaphore = asyncio.Semaphore(self._upload_concurrency)

        async def send(idx: int, body: bytes) -> tuple[int, int]:
            async with semaphore:
                message_id = await self._telegram.send_document(
                    job.channel_id,
                    body,
                    file_name=f"{blob_id.hex}.{idx}",
                    caption=f"{_PREFIX_DATA}{blob_id.hex}-{idx}",
                )
                return idx, message_id

        results = await asyncio.gather(
            *(send(idx, body) for idx, body in enumerate(groups))
        )
        return [message_id for _, message_id in sorted(results)]

    async def _send_commit_record(
        self, job: ClaimedJob, *, blob_manifest_message_id: int
    ) -> int:
        """Serialise and send the Commit Record; return its Telegram message id.

        The Commit Record is written last (Requirement 6.12): its confirmed
        message id is the durability boundary the caller records when advancing
        the generation to COMMITTED.
        """
        record = CommitRecord(
            format_version=1,
            generation_id=job.generation_id,
            bucket_id=job.bucket_id,
            key=job.key,
            generation_number=job.generation_number,
            blob_manifest_message_id=blob_manifest_message_id,
            etag=job.etag or "",
            checksums=job.checksums,
            content_type=job.content_type or "application/octet-stream",
            user_meta=job.user_meta,
            last_modified=job.last_modified,
        )
        return await self._telegram.send_document(
            job.channel_id,
            commit_record_codec.serialise(record),
            file_name=f"{job.generation_id.hex}.commit",
            caption=f"{_PREFIX_COMMIT}{job.generation_id.hex}",
        )

    async def _handle_telegram_error(
        self, job: ClaimedJob, exc: TelegramError
    ) -> None:
        """Schedule a retry or fail the generation after a Telegram error.

        Increments the job's attempt counter. If the budget is not yet exhausted,
        the generation moves to RETRY_WAIT and ``next_attempt_at`` is set to now
        plus the backoff (:func:`backoff_seconds`, or Telegram's requested wait
        on a :class:`FloodWaitError`). Otherwise the generation moves to FAILED
        and the staged file is retained (Requirement 6.14).
        """
        attempts = job.attempts + 1
        async with self._session_factory() as session, session.begin():
            if attempts >= self._max_attempts:
                await generations_storage.mark_failed(session, job.generation_id)
                await session.execute(
                    update(UploadJob)
                    .where(UploadJob.id == job.job_id)
                    .values(
                        state=JOB_STATE_FAILED,
                        attempts=attempts,
                        last_error=str(exc),
                    )
                )
                logger.error(
                    "generation %s FAILED after %d attempts: %s",
                    job.generation_id,
                    attempts,
                    exc,
                )
                return

            if isinstance(exc, FloodWaitError):
                delay = float(exc.retry_after)
            else:
                delay = backoff_seconds(attempts)
            next_attempt_at = _utcnow() + timedelta(seconds=delay)

            await generations_storage.mark_retry_wait(session, job.generation_id)
            await session.execute(
                update(UploadJob)
                .where(UploadJob.id == job.job_id)
                .values(
                    state=generations_storage.JOB_STATE_QUEUED,
                    attempts=attempts,
                    next_attempt_at=next_attempt_at,
                    last_error=str(exc),
                )
            )
            logger.warning(
                "generation %s upload attempt %d failed: %s; retry in %.1fs",
                job.generation_id,
                attempts,
                exc,
                delay,
            )

    # --- Partial commit record recovery (Requirement 14.3) ------------------

    async def _recover_partial_commit(self, job: ClaimedJob) -> bool:
        """Recover a generation stuck between its Blob Manifest and Commit Record.

        Crash recovery re-enqueues STAGED/UPLOADING generations (Requirement
        14.2), so a resumed job may be one the gateway crashed on after writing
        the Blob Manifest but before writing the Commit Record. The chunks and
        the manifest are already durable in Telegram; only the Commit Record —
        the message that makes the generation recoverable — is missing.

        This method inspects the bucket channel for the generation (Requirement
        14.3):

        * If a Commit Record (``tgo3-c-<generation_id>``) already exists, the
          write actually completed before the crash and only the DB is behind.
          The generation is reconciled to COMMITTED referencing the existing
          manifest/commit messages; no Telegram write happens.
        * If only the Blob Manifest exists (a decoded manifest whose ``sha256``
          and ``bucket_id`` match the generation), the Commit Record is
          re-attempted up to :data:`MAX_COMMIT_RECORD_RECOVERY_ATTEMPTS` times
          with the normal exponential backoff. On success the generation advances
          to COMMITTED; if every attempt fails the generation is set to
          UPLOAD_FAILED and :class:`CommitRecordRecoveryError` is surfaced.
        * If neither exists, this is not a partial-commit case; the normal
          upload path runs.

        Returns:
            ``True`` when the partial-commit case was detected and handled (the
            caller must not run the normal pipeline), ``False`` when the normal
            pipeline should proceed.
        """
        state = await self._scan_channel_for_generation(job)
        if state is None:
            return False

        if state.commit_message_id != 0:
            # The Commit Record itself is already present: the pre-crash write
            # reached Telegram (the commit is written last, so the manifest it
            # points at is present too); just reconcile the index to COMMITTED.
            await self._commit_recovered(
                job,
                blob_manifest_message_id=state.manifest_message_id,
                commit_message_id=state.commit_message_id,
                manifest=state.manifest,
            )
            logger.info(
                "generation %s already had a commit record; reconciled to "
                "COMMITTED without re-uploading",
                job.generation_id,
            )
            return True

        assert state.manifest is not None  # scan returns None when neither found
        await self._reattempt_commit_record(
            job, state.manifest_message_id, state.manifest
        )
        return True

    async def _reattempt_commit_record(
        self,
        job: ClaimedJob,
        manifest_message_id: int,
        manifest: BlobManifest,
    ) -> None:
        """Re-write only the Commit Record for a recovered generation.

        Retries the Commit Record send up to
        :data:`MAX_COMMIT_RECORD_RECOVERY_ATTEMPTS` times with the shared
        exponential backoff (:func:`backoff_seconds`), honouring a
        :class:`FloodWaitError`'s requested wait. The already-uploaded manifest
        and its chunks are reused: on success the blob/chunk rows are
        materialised from the manifest and the generation advances to COMMITTED
        in one transaction, exactly as the normal path leaves the index. If every
        attempt fails the generation is set to UPLOAD_FAILED and a
        :class:`CommitRecordRecoveryError` is raised (Requirement 14.3).
        """
        last_error: TelegramError | None = None
        for attempt in range(1, MAX_COMMIT_RECORD_RECOVERY_ATTEMPTS + 1):
            try:
                commit_message_id = await self._send_commit_record(
                    job, blob_manifest_message_id=manifest_message_id
                )
            except TelegramError as exc:
                last_error = exc
                if attempt == MAX_COMMIT_RECORD_RECOVERY_ATTEMPTS:
                    break
                delay = (
                    float(exc.retry_after)
                    if isinstance(exc, FloodWaitError)
                    else backoff_seconds(attempt)
                )
                logger.warning(
                    "generation %s commit-record recovery attempt %d/%d failed: "
                    "%s; retry in %.1fs",
                    job.generation_id,
                    attempt,
                    MAX_COMMIT_RECORD_RECOVERY_ATTEMPTS,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
                continue

            await self._commit_recovered(
                job,
                blob_manifest_message_id=manifest_message_id,
                commit_message_id=commit_message_id,
                manifest=manifest,
            )
            logger.info(
                "generation %s recovered: commit record re-written on attempt "
                "%d/%d",
                job.generation_id,
                attempt,
                MAX_COMMIT_RECORD_RECOVERY_ATTEMPTS,
            )
            return

        # All attempts failed: mark UPLOAD_FAILED and surface an actionable error.
        async with self._session_factory() as session, session.begin():
            await generations_storage.mark_upload_failed(
                session, job.generation_id
            )
            await session.execute(
                update(UploadJob)
                .where(UploadJob.id == job.job_id)
                .values(
                    state=JOB_STATE_FAILED,
                    last_error=(
                        str(last_error)
                        if last_error is not None
                        else "commit record recovery failed"
                    ),
                )
            )
        logger.error(
            "generation %s UPLOAD_FAILED: commit record could not be written "
            "after %d recovery attempts",
            job.generation_id,
            MAX_COMMIT_RECORD_RECOVERY_ATTEMPTS,
        )
        raise CommitRecordRecoveryError(
            job.generation_id, MAX_COMMIT_RECORD_RECOVERY_ATTEMPTS
        )

    async def _commit_recovered(
        self,
        job: ClaimedJob,
        *,
        blob_manifest_message_id: int,
        commit_message_id: int,
        manifest: BlobManifest | None = None,
    ) -> None:
        """Persist the recovered blob (when known) and advance to COMMITTED.

        Runs the durable half of the write path for a recovered generation in one
        transaction. When ``manifest`` is supplied (the re-written-commit-record
        case), the ``blobs`` and ``chunks`` rows are materialised from it — the
        content is already in Telegram, so this only records where it lives — and
        the generation is pointed at that blob. When ``manifest`` is ``None`` (the
        commit-record-already-present case) no blob row is materialised here; the
        generation is advanced referencing the existing commit, leaving the
        blob→generation link to the recovery CLI, consistent with how
        :func:`cli.rebuild.reconstruct_channel` reconstructs commit-only state.
        """
        async with self._session_factory() as session, session.begin():
            blob_id: uuid.UUID | None = None
            if manifest is not None:
                blob_id = uuid.UUID(bytes=manifest.blob_id)
                await self._materialise_recovered_blob(
                    session, manifest, blob_manifest_message_id
                )
                # Flush the materialised blob (and chunks) before advancing the
                # generation: the session factory runs with ``autoflush=False``,
                # so the Core UPDATE in ``advance_committed`` that sets
                # ``generations.blob_id`` would otherwise execute while the blob
                # INSERT is still pending and trip the
                # ``fk_generations_blob_id_blobs`` foreign key (the normal
                # ``_upload_and_commit`` path flushes for the same reason).
                await session.flush()
            await generations_storage.advance_committed(
                session,
                job.generation_id,
                commit_message_id=commit_message_id,
                blob_id=blob_id,
                etag=job.etag,
                checksums=job.checksums or None,
                last_modified=job.last_modified,
            )
            await _mark_job_done(session, job.job_id)

    async def _materialise_recovered_blob(
        self,
        session: AsyncSession,
        manifest: BlobManifest,
        manifest_message_id: int,
    ) -> None:
        """Insert the ``blobs``/``chunks`` rows for an already-uploaded manifest.

        The blob content and manifest are durable in Telegram; this only records
        them in the index so reads resolve. The insert is idempotent on the
        blob's ``(bucket_id, sha256)``: if a row already exists (a prior partial
        recovery, or the dedup unique constraint) the existing blob is reused
        rather than duplicated.
        """
        blob_id = uuid.UUID(bytes=manifest.blob_id)
        existing = (
            await session.execute(
                select(Blob.id).where(
                    Blob.bucket_id == manifest.bucket_id,
                    Blob.sha256 == manifest.sha256,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return

        await blobs_storage.insert_blob(
            session,
            bucket_id=manifest.bucket_id,
            sha256=manifest.sha256,
            size=manifest.size,
            key_wrapped=manifest.key_wrapped,
            chunk_records=manifest.chunk_records,
            refcount=1,
            manifest_message_id=manifest_message_id,
            blob_id=blob_id,
        )
        total_records = manifest.chunk_records * len(manifest.chunk_message_ids)
        _insert_chunks(
            session,
            blob_id,
            manifest.chunk_records,
            total_records,
            manifest.chunk_message_ids,
        )

    async def _scan_channel_for_generation(
        self, job: ClaimedJob
    ) -> _RecoveryScanState | None:
        """Scan the bucket channel for the generation's commit/manifest messages.

        Walks the channel in ascending ``message_id`` windows (mirroring the
        recovery CLI's scanner) looking for two things for this generation:

        * a Commit Record whose caption is ``tgo3-c-<generation_id>``; and
        * a Blob Manifest whose decoded ``sha256`` and ``bucket_id`` match the
          generation (the manifest caption carries the blob id, not the
          generation id, so the content digest is the link).

        Returns:
            ``None`` when neither message is found (not a partial-commit case). A
            :class:`_RecoveryScanState` when at least one is found:
            ``commit_message_id`` is non-zero when a Commit Record exists, and
            ``manifest``/``manifest_message_id`` carry the matching Blob Manifest
            when one was located and decoded (populated whether or not the Commit
            Record is also present, since the commit is written last).
        """
        commit_caption = f"{_PREFIX_COMMIT}{job.generation_id.hex}"
        manifest_message_id = 0
        manifest: BlobManifest | None = None
        commit_message_id = 0

        next_id = 1
        empty_batches = 0
        while empty_batches < _RECOVERY_EMPTY_BATCH_STOP:
            ids = list(range(next_id, next_id + _RECOVERY_SCAN_BATCH_SIZE))
            messages = await self._telegram.get_messages(job.channel_id, ids)
            next_id += _RECOVERY_SCAN_BATCH_SIZE
            if not messages:
                empty_batches += 1
                continue
            empty_batches = 0
            for message in sorted(messages, key=lambda m: m.message_id):
                if message.caption == commit_caption:
                    commit_message_id = message.message_id
                elif manifest is None and message.caption.startswith(
                    _PREFIX_BLOB_MANIFEST
                ):
                    candidate = await self._decode_manifest_if_match(job, message)
                    if candidate is not None:
                        manifest = candidate
                        manifest_message_id = message.message_id

        if commit_message_id == 0 and manifest is None:
            # Neither message exists: not a partial-commit case.
            return None
        return _RecoveryScanState(
            manifest_message_id=manifest_message_id,
            manifest=manifest,
            commit_message_id=commit_message_id,
        )

    async def _decode_manifest_if_match(
        self, job: ClaimedJob, message: TelegramMessage
    ) -> BlobManifest | None:
        """Download and decode a Blob Manifest, returning it only if it matches.

        A match is a decoded manifest in the generation's bucket whose ``sha256``
        equals the generation's content digest. A manifest that fails to download
        or decode, or that belongs to different content, is ignored (``None``) so
        a single bad or unrelated manifest never derails recovery.
        """
        if message.document_id is None:
            return None
        try:
            body = await self._telegram.download_document(
                job.channel_id, message.message_id
            )
            manifest = blob_manifest_codec.deserialise(body)
        except TelegramError:
            raise
        except (ValueError, KeyError) as exc:
            logger.warning(
                "ignoring undecodable blob manifest at message %d while "
                "recovering generation %s: %s",
                message.message_id,
                job.generation_id,
                exc,
            )
            return None
        if manifest.bucket_id == job.bucket_id and manifest.sha256 == job.sha256:
            return manifest
        return None


# ---------------------------------------------------------------------------
# Pure helpers (no I/O other than the staged-body read)
# ---------------------------------------------------------------------------


def _read_staged_body(staging_path: str | None) -> bytes:
    """Read the fsynced body from staging, or return empty bytes when absent.

    A ``None`` or missing path means a zero-length object; the caller still
    produces a single empty record so the blob has a well-formed manifest.
    """
    if staging_path is None:
        return b""
    if not os.path.exists(staging_path):
        return b""
    with open(staging_path, "rb") as handle:
        return handle.read()


def _split_records(plaintext: bytes) -> list[bytes]:
    """Split a plaintext body into record-sized slices.

    Each slice is at most :data:`record_codec.MAX_PLAINTEXT` bytes. An empty body
    yields exactly one empty record so a zero-length object still has a sealed,
    recoverable record.
    """
    if not plaintext:
        return [b""]
    size = record_codec.MAX_PLAINTEXT
    return [plaintext[i : i + size] for i in range(0, len(plaintext), size)]


def _seal_records(
    records: list[bytes], blob_key: bytes, blob_id: bytes, total_plaintext: int
) -> list[bytes]:
    """Seal every record with AES-256-GCM, binding its position into the AAD.

    Runs in a worker thread (sealing is CPU-bound). ``record_index`` doubles as
    the GCM nonce; ``is_last`` is set on the final record so a truncated blob
    fails authentication.
    """
    last = len(records) - 1
    return [
        record_codec.seal(
            record,
            blob_key,
            blob_id,
            record_index=index,
            total_plaintext=total_plaintext,
            is_last=index == last,
        )
        for index, record in enumerate(records)
    ]


def _group_records(sealed: list[bytes], chunk_records: int) -> list[bytes]:
    """Concatenate sealed records into ``chunk_records``-sized document bodies."""
    return [
        b"".join(sealed[i : i + chunk_records])
        for i in range(0, len(sealed), chunk_records)
    ]


def _insert_chunks(
    session: AsyncSession,
    blob_id: uuid.UUID,
    chunk_records: int,
    total_records: int,
    chunk_message_ids: list[int],
) -> None:
    """Add one ``chunks`` row per uploaded data document to the session.

    Each row records how many records the document holds (the last document may
    hold fewer than ``chunk_records``) and the Telegram message id, so a read can
    locate the document for a given record index. ``document_id``/``file_reference``
    /``dc_id`` are backfilled on first read; they are seeded here with placeholder
    values the read path refreshes.
    """
    for idx, message_id in enumerate(chunk_message_ids):
        start = idx * chunk_records
        records = min(chunk_records, total_records - start)
        session.add(
            Chunk(
                blob_id=blob_id,
                idx=idx,
                message_id=message_id,
                records=records,
                document_id=0,
                file_reference=b"",
                dc_id=0,
            )
        )


async def _load_blob(session: AsyncSession, blob_id: uuid.UUID) -> Blob:
    """Load a blob row by id for the dedup-reuse commit path."""
    return (
        await session.execute(select(Blob).where(Blob.id == blob_id))
    ).scalar_one()


async def _mark_job_done(session: AsyncSession, job_id: uuid.UUID) -> None:
    """Mark a committed job's row done so it is not re-claimed."""
    await session.execute(
        update(UploadJob)
        .where(UploadJob.id == job_id)
        .values(state=generations_storage.STATE_COMMITTED)
    )

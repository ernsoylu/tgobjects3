"""The Checkpoint Worker — periodic encrypted index snapshots to Telegram.

A Checkpoint is the index-recovery accelerator: an encrypted ``pg_dump`` of the
PostgreSQL cache plus the highest confirmed Telegram ``message_id`` per channel,
written to the system channel under caption ``tgo3-p-<timestamp>``. The Recovery
CLI selects the highest-id Checkpoint, restores the dump, and then only scans
messages strictly newer than the recorded per-channel high-water marks instead
of replaying every channel from the beginning (requirements 15.5, 15.7, 16.1).

This worker is the background task that produces those Checkpoints. Once per
``CHECKPOINT_INTERVAL`` seconds it:

    dump index (pg_dump) -> encrypt envelope (AES-256-GCM, HKDF-derived key) ->
    compute per-channel high-water -> build Checkpoint msgpack ->
    send_document to system channel (caption tgo3-p-<ts>) ->
    insert a `checkpoints` row with the returned message_id

mirroring :class:`~gateway.app.jobs.upload_worker.UploadWorker`'s shape:
:meth:`run_once` performs exactly one cycle (the unit tests and the loop both
drive), :meth:`run_forever` sleeps ``CHECKPOINT_INTERVAL`` between cycles, and
:meth:`stop` requests a clean exit.

Durability vs. the Upload Worker
--------------------------------
A Checkpoint is a *cache* accelerator, never a source of truth — the durable
object history lives in the bucket channels' Commit Records and Tombstones. So a
failed Checkpoint must never interfere with the write path. Per requirement 15.6
the Telegram write is retried up to :data:`CHECKPOINT_WRITE_MAX_ATTEMPTS` times
at :data:`CHECKPOINT_RETRY_INTERVAL_SECONDS` intervals; if every attempt fails
the worker emits an error log entry and returns normally, leaving object storage
operations untouched. :class:`CheckpointWriteError` is raised only by the inner
write helper and is caught at the cycle boundary.

Encryption
----------
The ``pg_dump`` bytes are sealed with AES-256-GCM under a key derived from
``TGOS3_MASTER_KEY`` via HKDF-SHA256 (info string
:data:`CHECKPOINT_KEY_INFO`), domain-separated from the root key-wrap key and
the cache key. The envelope is ``nonce (12 bytes) || ciphertext || tag`` — the
same prepended-nonce layout the record and cache codecs use — and is carried in
``Checkpoint.pg_dump_enc`` (requirement 15.5, "encrypted pg_dump").

High-water marks
----------------
For each bucket channel the high-water mark is the maximum confirmed
``message_id`` across every message type the gateway has written there: Commit
Records (``generations.commit_message_id``), Blob Manifests
(``blobs.manifest_message_id``), and data chunks (``chunks.message_id``). The map
is keyed by the channel id rendered as a string so it round-trips through the
msgpack ``dict[str, int]`` field and the JSONB ``high_water`` column unchanged
(requirement 15.5, "highest confirmed message ID per channel").
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gateway.app.db.models import Blob, Bucket, Chunk, Generation
from gateway.app.db.models import Checkpoint as CheckpointRow
from gateway.app.format import checkpoint as checkpoint_codec
from gateway.app.format.types import FORMAT_VERSION, Checkpoint
from gateway.app.telegram.errors import TelegramError
from gateway.app.telegram.port import TelegramPort

logger = logging.getLogger(__name__)

__all__ = [
    "CHECKPOINT_CAPTION_PREFIX",
    "CHECKPOINT_KEY_INFO",
    "CHECKPOINT_KEY_LENGTH",
    "CHECKPOINT_NONCE_LENGTH",
    "CHECKPOINT_RETRY_INTERVAL_SECONDS",
    "CHECKPOINT_WRITE_MAX_ATTEMPTS",
    "CheckpointWriteError",
    "CheckpointWorker",
    "compute_high_water",
    "derive_checkpoint_key",
]

# --- Caption scheme -----------------------------------------------------------
#: Caption prefix for Checkpoint messages in the system channel. The suffix is a
#: Unix timestamp (seconds), matching the design's ``tgo3-p-<timestamp>`` row.
CHECKPOINT_CAPTION_PREFIX = "tgo3-p-"

# --- Crypto -------------------------------------------------------------------
#: HKDF domain-separation info string for the checkpoint-envelope key. Distinct
#: from the root-key info (``tgobjects3-v1-root``) so the key that encrypts the
#: pg_dump is independent of the bucket-KEK-wrapping key.
CHECKPOINT_KEY_INFO = b"tgobjects3-v1-checkpoint"
#: Length in bytes of the derived AES-256 checkpoint key.
CHECKPOINT_KEY_LENGTH = 32
#: Length in bytes of the AES-GCM nonce prepended to the sealed envelope.
CHECKPOINT_NONCE_LENGTH = 12

# --- Retry policy (requirement 15.6) -----------------------------------------
#: Total Telegram write attempts before a checkpoint cycle is abandoned.
CHECKPOINT_WRITE_MAX_ATTEMPTS = 3
#: Fixed interval, in seconds, between checkpoint write attempts.
CHECKPOINT_RETRY_INTERVAL_SECONDS = 5.0


class CheckpointWriteError(RuntimeError):
    """Every attempt to write a Checkpoint to the system channel failed.

    Raised by :meth:`CheckpointWorker._write_with_retry` after
    :data:`CHECKPOINT_WRITE_MAX_ATTEMPTS` failed Telegram writes (requirement
    15.6). It is caught at the cycle boundary, logged as an error, and swallowed
    so object storage operations are never interrupted.
    """


def derive_checkpoint_key(master_key: str) -> bytes:
    """Derive the AES-256 checkpoint-envelope key from the master key.

    Uses HKDF-SHA256 with the fixed info string :data:`CHECKPOINT_KEY_INFO` so
    the resulting key is domain-separated from the root key-wrap key and the
    cache key. The Recovery CLI derives the same key from the same master key to
    decrypt the pg_dump (requirement 15.5).

    Args:
        master_key: The master key supplied via ``TGOS3_MASTER_KEY``.

    Returns:
        A 32-byte key suitable for AES-256-GCM.
    """
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=CHECKPOINT_KEY_LENGTH,
        salt=None,
        info=CHECKPOINT_KEY_INFO,
    )
    return hkdf.derive(master_key.encode("utf-8"))


def _seal_pg_dump(pg_dump: bytes, key: bytes) -> bytes:
    """Seal the pg_dump bytes with AES-256-GCM, prepending a random nonce.

    The envelope layout is ``nonce (12 bytes) || ciphertext || tag`` — the same
    prepended-nonce shape used by the record and cache codecs, so the Recovery
    CLI unseals it by splitting off the first :data:`CHECKPOINT_NONCE_LENGTH`
    bytes as the nonce (requirement 15.5).
    """
    nonce = os.urandom(CHECKPOINT_NONCE_LENGTH)
    sealed = AESGCM(key).encrypt(nonce, pg_dump, None)
    return nonce + sealed


def _pg_dump_url(database_url: str) -> str:
    """Return ``database_url`` with any async driver suffix stripped.

    ``pg_dump`` is a libpq client and understands only plain ``postgresql://``
    connection URIs, whereas the gateway stores a SQLAlchemy URL that may carry
    the ``+asyncpg`` driver tag. Strip the driver so the subprocess connects.
    """
    for prefix in ("postgresql+asyncpg://", "postgresql+psycopg://"):
        if database_url.startswith(prefix):
            return "postgresql://" + database_url[len(prefix) :]
    return database_url


async def _run_pg_dump(database_url: str) -> bytes:
    """Run ``pg_dump`` against ``database_url`` and return its stdout bytes.

    Produces a plain-text SQL dump on stdout. The subprocess is awaited off the
    event loop via :func:`asyncio.create_subprocess_exec`.

    Raises:
        CheckpointWriteError: If ``pg_dump`` exits non-zero; the stderr tail is
            included so the failure is diagnosable from the error log.
    """
    process = await asyncio.create_subprocess_exec(
        "pg_dump",
        "--no-password",
        _pg_dump_url(database_url),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        detail = stderr.decode("utf-8", "replace").strip()
        raise CheckpointWriteError(
            f"pg_dump exited with code {process.returncode}: {detail}"
        )
    return stdout


async def compute_high_water(session: AsyncSession) -> dict[str, int]:
    """Compute the highest confirmed ``message_id`` per bucket channel.

    For every bucket, takes the maximum ``message_id`` the gateway has confirmed
    in that bucket's channel across all message types it writes there — Commit
    Records (``generations.commit_message_id``), Blob Manifests
    (``blobs.manifest_message_id``), and data chunks (``chunks.message_id``). A
    bucket with no confirmed messages yet contributes ``0`` so its channel still
    appears in the map and the Recovery CLI scans it from the start.

    The result is keyed by the channel id rendered as a string so it round-trips
    through the msgpack ``dict[str, int]`` field and the JSONB column unchanged
    (requirement 15.5).

    Args:
        session: An open async session; this is a read-only query.

    Returns:
        A map of ``str(channel_id)`` to the highest confirmed message id.
    """
    channels = (
        await session.execute(select(Bucket.id, Bucket.channel_id))
    ).all()

    high_water: dict[str, int] = {}
    for bucket_id, channel_id in channels:
        commit_max = (
            await session.execute(
                select(func.max(Generation.commit_message_id)).where(
                    Generation.bucket_id == bucket_id
                )
            )
        ).scalar_one()
        manifest_max = (
            await session.execute(
                select(func.max(Blob.manifest_message_id)).where(
                    Blob.bucket_id == bucket_id
                )
            )
        ).scalar_one()
        chunk_max = (
            await session.execute(
                select(func.max(Chunk.message_id))
                .select_from(Chunk)
                .join(Blob, Blob.id == Chunk.blob_id)
                .where(Blob.bucket_id == bucket_id)
            )
        ).scalar_one()

        candidates = [v for v in (commit_max, manifest_max, chunk_max) if v is not None]
        high_water[str(channel_id)] = max(candidates) if candidates else 0
    return high_water


def _utcnow() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


class CheckpointWorker:
    """Writes an encrypted index snapshot to the system channel periodically.

    Owns a session factory, a :class:`TelegramPort`, and the system channel id.
    :meth:`run_once` performs a single checkpoint cycle (dump, encrypt, write,
    record); :meth:`run_forever` repeats it every ``CHECKPOINT_INTERVAL``
    seconds until :meth:`stop` is called. A failed Telegram write is retried per
    requirement 15.6 and, if exhausted, logged without disturbing the write
    path.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        telegram: TelegramPort,
        master_key: str,
        system_channel_id: int,
        database_url: str,
        *,
        interval_seconds: float,
        max_write_attempts: int = CHECKPOINT_WRITE_MAX_ATTEMPTS,
        retry_interval_seconds: float = CHECKPOINT_RETRY_INTERVAL_SECONDS,
    ) -> None:
        """Create a Checkpoint Worker.

        Args:
            session_factory: Factory producing ``AsyncSession`` instances; the
                high-water query and the ``checkpoints`` insert each open their
                own short transaction.
            telegram: The Telegram boundary for sending the Checkpoint document.
            master_key: ``TGOS3_MASTER_KEY``; the checkpoint-envelope key is
                derived from it to encrypt the pg_dump.
            system_channel_id: The channel the Checkpoint is written to.
            database_url: ``DATABASE_URL``; ``pg_dump`` is run against it.
            interval_seconds: ``CHECKPOINT_INTERVAL`` — seconds between cycles in
                :meth:`run_forever` (range 60–86400, enforced by config).
            max_write_attempts: Telegram write attempts before a cycle is
                abandoned (requirement 15.6); must be >= 1.
            retry_interval_seconds: Delay between write attempts.
        """
        if max_write_attempts < 1:
            raise ValueError("max_write_attempts must be >= 1")
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be > 0")
        self._session_factory = session_factory
        self._telegram = telegram
        self._checkpoint_key = derive_checkpoint_key(master_key)
        self._system_channel_id = system_channel_id
        self._database_url = database_url
        self._interval_seconds = interval_seconds
        self._max_write_attempts = max_write_attempts
        self._retry_interval_seconds = retry_interval_seconds
        self._stopping = False

    def stop(self) -> None:
        """Request :meth:`run_forever` to exit after the current cycle."""
        self._stopping = True

    async def run_forever(self) -> None:
        """Write a Checkpoint every ``interval_seconds`` until :meth:`stop`.

        A cycle failure (write retries exhausted or an unexpected error) is
        logged inside :meth:`run_once` and never propagates, so the loop keeps
        its cadence and object storage operations are never interrupted
        (requirement 15.6).
        """
        while not self._stopping:
            await self.run_once()
            if self._stopping:
                break
            await asyncio.sleep(self._interval_seconds)

    async def run_once(self) -> bool:
        """Perform one checkpoint cycle: dump, encrypt, write, record.

        Returns:
            ``True`` if a Checkpoint was written to the system channel and a
            ``checkpoints`` row recorded; ``False`` if the cycle failed (write
            retries exhausted or any other error). A ``False`` return has
            already been logged and leaves object storage operations untouched
            (requirement 15.6).
        """
        try:
            pg_dump = await _run_pg_dump(self._database_url)
            pg_dump_enc = _seal_pg_dump(pg_dump, self._checkpoint_key)
            async with self._session_factory() as session:
                high_water = await compute_high_water(session)

            payload = checkpoint_codec.serialise(
                Checkpoint(
                    format_version=FORMAT_VERSION,
                    pg_dump_enc=pg_dump_enc,
                    high_water=high_water,
                )
            )
            message_id = await self._write_with_retry(payload)
            await self._record_checkpoint(message_id, high_water)
        except CheckpointWriteError as exc:
            logger.error("checkpoint write failed: %s", exc)
            return False
        except Exception as exc:  # noqa: BLE001 - a checkpoint must never break writes
            logger.error("checkpoint cycle failed: %s", exc)
            return False

        logger.info(
            "checkpoint written to system channel %s as message %d (%d channels)",
            self._system_channel_id,
            message_id,
            len(high_water),
        )
        return True

    async def _write_with_retry(self, payload: bytes) -> int:
        """Send the Checkpoint document, retrying transient Telegram failures.

        Attempts the write up to :attr:`_max_write_attempts` times, sleeping
        :attr:`_retry_interval_seconds` between attempts (requirement 15.6). The
        caption carries the current Unix timestamp (``tgo3-p-<timestamp>``).

        Returns:
            The ``message_id`` of the written Checkpoint message.

        Raises:
            CheckpointWriteError: If every attempt failed with a Telegram error.
        """
        timestamp = int(_utcnow().timestamp())
        caption = f"{CHECKPOINT_CAPTION_PREFIX}{timestamp}"
        file_name = f"checkpoint-{timestamp}.msgpack"

        last_error: TelegramError | None = None
        for attempt in range(1, self._max_write_attempts + 1):
            try:
                return await self._telegram.send_document(
                    self._system_channel_id,
                    payload,
                    file_name=file_name,
                    caption=caption,
                )
            except TelegramError as exc:
                last_error = exc
                logger.warning(
                    "checkpoint write attempt %d/%d failed: %s",
                    attempt,
                    self._max_write_attempts,
                    exc,
                )
                if attempt < self._max_write_attempts:
                    await asyncio.sleep(self._retry_interval_seconds)

        raise CheckpointWriteError(
            f"checkpoint write failed after {self._max_write_attempts} attempts"
        ) from last_error

    async def _record_checkpoint(
        self, message_id: int, high_water: dict[str, int]
    ) -> None:
        """Insert a ``checkpoints`` row for a successfully written Checkpoint.

        Records the Telegram ``message_id`` and the per-channel high-water map so
        the admin API can report the last checkpoint and the Recovery CLI can
        cross-check against the system channel (requirement 15.5). ``created_at``
        is filled by the column's server default.
        """
        async with self._session_factory() as session, session.begin():
            session.add(
                CheckpointRow(
                    message_id=message_id,
                    high_water=dict(high_water),
                )
            )

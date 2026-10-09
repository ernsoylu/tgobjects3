"""Blob-layer helpers: dedup claim, blob insertion, and refcount management.

A *blob* is the encrypted, deduplicated binary payload shared by one or more
generations within a bucket. Deduplication is content-addressed: two generations
that carry byte-for-byte identical bodies resolve to the same ``blobs`` row,
keyed on the content ``sha256``. The hard guarantee against duplicate rows is the
``UNIQUE(bucket_id, sha256)`` constraint on the table; this module layers the
cooperative dedup *protocol* (Requirements 6.10/6.11) on top of it.

Blob ``state`` casing
---------------------
A fully-uploaded blob is marked ``'COMMITTED'`` (uppercase). The design's dedup
SQL sketch shows a lowercase ``state = 'committed'``, but the recovery CLI and
the generation state machine both emit uppercase ``'COMMITTED'``. We reconcile
on **uppercase** everywhere in the storage layer so the dedup check here matches
the rows the upload worker and recovery CLI actually write. :data:`BLOB_STATE_COMMITTED`
is the single source of truth for that literal.

Lock lifecycle
--------------
:func:`claim_or_wait` implements the dedup entry path. It takes a *session-level*
PostgreSQL advisory lock keyed on the ``sha256`` (via the ``pg_advisory_lock``
helper in :mod:`gateway.app.db.models`), then looks for an existing COMMITTED
blob with the same ``(bucket_id, sha256)``:

* **Dedup hit** — a COMMITTED blob already exists. There is nothing to upload, so
  the lock is released *before returning* and the result carries the existing
  blob id. The caller reuses that blob and performs no Telegram upload.
* **Dedup miss** — no COMMITTED blob exists yet. The lock is *retained* and
  handed to the caller inside the result. The caller is now the single writer for
  this content: concurrent callers for the same ``sha256`` block inside their own
  :func:`claim_or_wait` until this caller releases the lock. The caller MUST call
  :func:`release_claim` (or :func:`gateway.app.db.models.pg_advisory_unlock`)
  exactly once when it finishes uploading — on success *and* on failure — so a
  crashed or failed upload does not wedge every other writer of that content.
  Closing the database session also releases the lock.

Transaction boundary
---------------------
Every helper here takes an :class:`~sqlalchemy.ext.asyncio.AsyncSession` and does
not ``commit``. Flushing versus committing is the caller's decision, because a
blob insert, a generation insert, and an ``object_heads`` upsert must land in one
atomic transaction owned by the upload worker.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.app.db.models import Blob, pg_advisory_lock, pg_advisory_unlock

#: The ``blobs.state`` literal for a fully-uploaded, reusable blob. Uppercase to
#: match the generation state machine and the recovery CLI (see module docstring).
BLOB_STATE_COMMITTED = "COMMITTED"


@dataclass(frozen=True)
class BlobClaimResult:
    """The outcome of a :func:`claim_or_wait` dedup claim.

    Exactly one of two situations is reported:

    * ``is_dedup_hit`` is ``True`` — a COMMITTED blob already exists for this
      content. ``blob_id`` is its id; the caller reuses it and uploads nothing.
      The advisory lock has already been released, so ``holds_lock`` is ``False``.
    * ``is_dedup_hit`` is ``False`` — no COMMITTED blob exists. ``blob_id`` is
      ``None`` and ``holds_lock`` is ``True``: the caller owns the advisory lock
      for ``sha256`` and must release it with :func:`release_claim` once the
      upload finishes (success or failure).

    Attributes:
        is_dedup_hit: Whether an existing COMMITTED blob was found.
        blob_id: The reusable blob's id on a hit, otherwise ``None``.
        sha256: The content digest the claim was taken for; needed to release the
            lock on the miss path.
        holds_lock: Whether this result still holds the advisory lock (miss path).
    """

    is_dedup_hit: bool
    blob_id: uuid.UUID | None
    sha256: bytes
    holds_lock: bool


async def claim_or_wait(
    session: AsyncSession, bucket_id: uuid.UUID, sha256: bytes
) -> BlobClaimResult:
    """Take the dedup lock for ``sha256`` and report whether a reuse is possible.

    Implements the dedup entry path of Requirements 6.10 and 6.11. The call first
    acquires the session-level advisory lock for the content digest; because the
    lock serialises every caller sharing a ``sha256``, concurrent uploads of
    identical content line up here and only one proceeds to upload at a time. With
    the lock held it checks for a COMMITTED blob in the same bucket:

    * On a hit it releases the lock immediately (no upload is needed) and returns
      a result whose ``blob_id`` points at the blob to reuse.
    * On a miss it keeps the lock and returns a result with ``holds_lock=True``;
      the caller is now the designated uploader and owns the lock.

    The lookup is scoped to ``(bucket_id, sha256)`` to match the
    ``UNIQUE(bucket_id, sha256)`` constraint: dedup is *within* a bucket, never
    across buckets, so one bucket's blob is never reused by another.

    Args:
        session: The database session; the advisory lock is tied to its
            connection and released by :func:`release_claim` or on session close.
        bucket_id: The bucket whose blobs are candidates for reuse.
        sha256: The 32-byte content digest to claim.

    Returns:
        A :class:`BlobClaimResult`. See the class docstring and the module-level
        "Lock lifecycle" note for the caller's obligations on each path.
    """
    await pg_advisory_lock(session, sha256)

    existing = await session.execute(
        select(Blob.id)
        .where(
            Blob.bucket_id == bucket_id,
            Blob.sha256 == sha256,
            Blob.state == BLOB_STATE_COMMITTED,
        )
        .limit(1)
    )
    blob_id = existing.scalar_one_or_none()

    if blob_id is not None:
        # A reusable blob already exists; nothing to upload, so drop the lock now
        # rather than make the caller remember to release it on the hit path.
        await pg_advisory_unlock(session, sha256)
        return BlobClaimResult(
            is_dedup_hit=True,
            blob_id=blob_id,
            sha256=sha256,
            holds_lock=False,
        )

    # No committed blob yet: hand the lock to the caller, who is now the single
    # writer for this content and is responsible for releasing it.
    return BlobClaimResult(
        is_dedup_hit=False,
        blob_id=None,
        sha256=sha256,
        holds_lock=True,
    )


async def release_claim(session: AsyncSession, claim: BlobClaimResult) -> None:
    """Release the advisory lock held by a dedup-miss :class:`BlobClaimResult`.

    Call this exactly once when the upload the claim authorised has finished,
    whether it succeeded or failed, so waiting writers of the same content can
    proceed. It is a no-op when ``claim`` does not hold the lock (a dedup hit, or
    a claim already released), which makes it safe to call unconditionally in a
    ``finally`` block.

    Args:
        session: The same session that acquired the lock in :func:`claim_or_wait`.
        claim: The claim returned by :func:`claim_or_wait`.
    """
    if claim.holds_lock:
        await pg_advisory_unlock(session, claim.sha256)


async def insert_blob(
    session: AsyncSession,
    *,
    bucket_id: uuid.UUID,
    sha256: bytes,
    size: int,
    key_wrapped: bytes,
    chunk_records: int,
    state: str = BLOB_STATE_COMMITTED,
    refcount: int = 0,
    manifest_message_id: int | None = None,
    blob_id: uuid.UUID | None = None,
) -> Blob:
    """Insert a new ``blobs`` row and return the pending ORM instance.

    Used on the dedup-miss path once the content has been (or is about to be)
    sealed and uploaded: the caller holds the advisory lock from
    :func:`claim_or_wait`, so no other writer can race in a duplicate row for the
    same ``(bucket_id, sha256)``. The row is added to the session but not
    committed; the caller flushes/commits it together with the generation insert
    and head advance in one transaction.

    Args:
        session: The database session to add the row to.
        bucket_id: Owning bucket.
        sha256: 32-byte content digest (the dedup key within the bucket).
        size: Plaintext blob size in bytes.
        key_wrapped: The blob's AES key, wrapped under the bucket KEK.
        chunk_records: Number of records packed into each Telegram chunk document.
        state: Initial blob state; defaults to ``'COMMITTED'``.
        refcount: Initial reference count; defaults to ``0``.
        manifest_message_id: Telegram message id of the Blob Manifest, if known.
        blob_id: Explicit primary key; defaults to a fresh UUID.

    Returns:
        The newly added :class:`~gateway.app.db.models.Blob` instance. Its
        server-side defaults are populated after the caller flushes.
    """
    blob = Blob(
        bucket_id=bucket_id,
        sha256=sha256,
        size=size,
        key_wrapped=key_wrapped,
        chunk_records=chunk_records,
        state=state,
        refcount=refcount,
        manifest_message_id=manifest_message_id,
    )
    if blob_id is not None:
        blob.id = blob_id
    session.add(blob)
    return blob


async def increment_refcount(session: AsyncSession, blob_id: uuid.UUID) -> int:
    """Atomically add one to a blob's reference count.

    Called when a new generation starts pointing at an existing blob (a dedup hit
    or a CopyObject). The update is expressed as ``refcount = refcount + 1`` so the
    increment happens in the database, not read-modify-write in Python, keeping it
    correct under concurrent writers. Not committed here.

    Args:
        session: The database session.
        blob_id: The blob whose refcount to increment.

    Returns:
        The refcount value after the increment.

    Raises:
        KeyError: If no blob with ``blob_id`` exists.
    """
    return await _adjust_refcount(session, blob_id, +1)


async def decrement_refcount(session: AsyncSession, blob_id: uuid.UUID) -> int:
    """Atomically subtract one from a blob's reference count.

    Called when a generation that referenced the blob is superseded or deleted. A
    blob whose refcount reaches zero becomes eligible for garbage collection; this
    helper only adjusts the count and does not itself purge anything. Expressed as
    ``refcount = refcount - 1`` in SQL for the same concurrency reason as
    :func:`increment_refcount`. Not committed here.

    Args:
        session: The database session.
        blob_id: The blob whose refcount to decrement.

    Returns:
        The refcount value after the decrement.

    Raises:
        KeyError: If no blob with ``blob_id`` exists.
    """
    return await _adjust_refcount(session, blob_id, -1)


async def _adjust_refcount(
    session: AsyncSession, blob_id: uuid.UUID, delta: int
) -> int:
    """Apply ``delta`` to a blob's refcount in one UPDATE and return the new value.

    The ``refcount = refcount + :delta`` form makes the read and write a single
    atomic statement so concurrent increments/decrements do not clobber each
    other. ``RETURNING`` gives back the post-update value without a second query.
    """
    result = await session.execute(
        update(Blob)
        .where(Blob.id == blob_id)
        .values(refcount=Blob.refcount + delta)
        .returning(Blob.refcount)
    )
    new_value = result.scalar_one_or_none()
    if new_value is None:
        raise KeyError(f"no blob with id {blob_id}")
    return new_value


__all__ = [
    "BLOB_STATE_COMMITTED",
    "BlobClaimResult",
    "claim_or_wait",
    "decrement_refcount",
    "increment_refcount",
    "insert_blob",
    "release_claim",
]

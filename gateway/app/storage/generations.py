"""Generation lifecycle helpers against the PostgreSQL index.

A *generation* is one logical write to a key — a ``PutObject``, a completed
multipart upload, or a delete. Its row in ``generations`` moves through the
state machine defined in the design document:

    STAGED -> UPLOADING -> COMMITTED
      |          |
      |          +--> RETRY_WAIT --> UPLOADING
      |                   |
      |                   +--> FAILED
      |
    COMMITTED -> SUPERSEDED   (overwrite or delete)

This module owns the state-transition surface for that machine. Each function
operates on the *caller's* :class:`~sqlalchemy.ext.asyncio.AsyncSession` and
deliberately does **not** commit: the transaction boundary belongs to the
caller (the PutObject handler or the Upload Worker), so a single request can
compose several of these calls into one atomic unit. Where a function must make
its writes visible to a subsequent query within the same transaction (for
example, the ``object_heads`` upsert needs the generation row to exist), it
flushes.

The state values are plain strings matching the recovery CLI convention
(``cli/rebuild.py`` writes uppercase ``COMMITTED`` / ``DELETED`` / ``SUPERSEDED``):
they are exposed as module constants so the Upload Worker and tests reference the
same tokens rather than scattering string literals.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import CursorResult, update
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.app.db.models import Generation, UploadJob

# --- Generation states --------------------------------------------------------
#: Body is fsynced to staging and reserved; durable only once COMMITTED. Async
#: PUTs return HTTP 200 here.
STATE_STAGED = "STAGED"
#: The Upload Worker has claimed the job and is transmitting to Telegram.
STATE_UPLOADING = "UPLOADING"
#: The Commit Record message id is confirmed by Telegram; the write is durable.
STATE_COMMITTED = "COMMITTED"
#: A Telegram error occurred; the worker is backing off before another attempt.
STATE_RETRY_WAIT = "RETRY_WAIT"
#: Retry attempts are exhausted; the staged file is retained for inspection.
STATE_FAILED = "FAILED"
#: Crash recovery found a Blob Manifest in Telegram for this generation but no
#: Commit Record, and the five re-attempts to write the Commit Record all failed
#: (Requirement 14.3). Distinct from :data:`STATE_FAILED` (the normal
#: retry-exhaustion terminal) so an operator can tell a partial-commit-record
#: failure apart from an upload that never reached the manifest.
STATE_UPLOAD_FAILED = "UPLOAD_FAILED"
#: A newer generation (overwrite) or a tombstone (delete) replaced this one.
STATE_SUPERSEDED = "SUPERSEDED"
#: A Tombstone marked this generation deleted; it is no longer resolvable by a
#: read. The recovery CLI emits the same literal when it replays a Tombstone, so
#: a rebuilt index and a live delete agree on the state token (Requirement 9.3).
STATE_DELETED = "DELETED"
#: The GC Worker has deleted this generation's ``tgo3`` messages from Telegram
#: after it was dereferenced for ``GC_GRACE`` (design "Generation State
#: Machine": ``SUPERSEDED -> PURGED``). The row is **retained** in PostgreSQL for
#: audit — only the Telegram messages and any staging file are removed.
STATE_PURGED = "PURGED"

# --- Upload job states --------------------------------------------------------
#: A freshly enqueued upload job awaiting a worker claim.
JOB_STATE_QUEUED = "QUEUED"

__all__ = [
    "JOB_STATE_QUEUED",
    "STATE_COMMITTED",
    "STATE_DELETED",
    "STATE_FAILED",
    "STATE_PURGED",
    "STATE_RETRY_WAIT",
    "STATE_STAGED",
    "STATE_SUPERSEDED",
    "STATE_UPLOADING",
    "STATE_UPLOAD_FAILED",
    "GenerationStateError",
    "advance_committed",
    "insert_staged",
    "mark_deleted",
    "mark_failed",
    "mark_purged",
    "mark_retry_wait",
    "mark_superseded",
    "mark_upload_failed",
    "mark_uploading",
]


class GenerationStateError(RuntimeError):
    """Raised when a state transition targets a generation that does not exist."""


def _utcnow() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


async def insert_staged(
    session: AsyncSession,
    *,
    bucket_id: uuid.UUID,
    key: str,
    kind: str,
    size: int,
    staging_path: str | None,
    content_type: str | None = None,
    user_meta: dict[str, Any] | None = None,
    last_modified: datetime | None = None,
    generation_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Insert a STAGED generation and its upload job in one transaction.

    Both rows are added to the caller's session and flushed so they are visible
    to later statements in the same transaction, but the transaction is **not**
    committed — the caller decides when to commit (and, on failure, rolls both
    rows back together). This is the ``DB: insert generation(STAGED) +
    upload_job`` step of the write path in the design.

    Args:
        session: The active async session; its transaction boundary is honoured.
        bucket_id: The bucket the key lives in.
        key: The object key being written.
        kind: The write kind (e.g. ``"put"``, ``"multipart"``, ``"delete"``).
        size: The object size in bytes.
        staging_path: Filesystem path of the fsynced staged body, or ``None`` for
            writes with no body (such as a delete tombstone generation).
        content_type: Optional ``Content-Type`` recorded for later GETs.
        user_meta: Optional user metadata map stored as JSONB.
        last_modified: The last-modified timestamp; defaults to now (UTC).
        generation_id: Optional explicit id; generated when omitted. Supplying it
            lets the caller correlate the row before the flush returns.

    Returns:
        The ``id`` of the inserted generation row.
    """
    gen_id = generation_id or uuid.uuid4()
    generation = Generation(
        id=gen_id,
        bucket_id=bucket_id,
        key=key,
        kind=kind,
        blob_id=None,
        size=size,
        etag=None,
        checksums=None,
        content_type=content_type,
        user_meta=user_meta,
        last_modified=last_modified or _utcnow(),
        state=STATE_STAGED,
        commit_message_id=None,
        staging_path=staging_path,
    )
    session.add(generation)

    job = UploadJob(
        id=uuid.uuid4(),
        generation_id=gen_id,
        state=JOB_STATE_QUEUED,
        attempts=0,
        next_attempt_at=None,
        last_error=None,
    )
    session.add(job)

    # Flush so the generation and job rows exist for any follow-on statement in
    # this transaction (e.g. the Upload Worker claiming the job), without
    # committing — the caller still owns the boundary.
    await session.flush()
    return gen_id


async def advance_committed(
    session: AsyncSession,
    generation_id: uuid.UUID,
    *,
    commit_message_id: int,
    blob_id: uuid.UUID | None,
    etag: str | None = None,
    checksums: dict[str, Any] | None = None,
    last_modified: datetime | None = None,
) -> None:
    """Advance a generation to COMMITTED and point its object head at it.

    This is the durable-commit transition: it records the Telegram
    ``commit_message_id`` and the ``blob_id`` the generation now references, sets
    the state to COMMITTED, and advances ``object_heads`` so reads resolve to this
    generation. Everything happens inside the caller's transaction, so the state
    advance and the head move are atomic — a reader never sees a COMMITTED
    generation whose head has not yet moved, nor the reverse.

    The ``object_heads`` upsert is owned by the heads helper (task 16.3). We
    delegate to :func:`gateway.app.storage.heads.advance_head` when it is
    available, importing it lazily so this module stays importable before that
    file lands. If it is absent we fall back to an equivalent
    ``INSERT ... ON CONFLICT DO UPDATE`` upsert so the commit path is never
    blocked on the sibling task.

    Args:
        session: The active async session; the transaction boundary is the
            caller's.
        generation_id: The generation being committed.
        commit_message_id: The Telegram message id of the confirmed Commit Record.
        blob_id: The blob this generation's bytes live in (freshly uploaded or
            reused via the dedup shortcut), or ``None`` when the blob row is not
            being materialised in this transaction (partial-commit-record
            recovery whose manifest could not be re-decoded — the blob link is
            then deferred to the recovery CLI, as in commit-only reconstruction).
        etag: Optional ETag to record for later HEAD/GET responses.
        checksums: Optional checksum map stored as JSONB.
        last_modified: Optional override for the commit timestamp; left unchanged
            on the row when omitted.

    Raises:
        GenerationStateError: If no generation row matches ``generation_id``.
    """
    values: dict[str, Any] = {
        "state": STATE_COMMITTED,
        "commit_message_id": commit_message_id,
    }
    if blob_id is not None:
        values["blob_id"] = blob_id
    if etag is not None:
        values["etag"] = etag
    if checksums is not None:
        values["checksums"] = checksums
    if last_modified is not None:
        values["last_modified"] = last_modified

    result = cast(
        "CursorResult[Any]",
        await session.execute(
            update(Generation).where(Generation.id == generation_id).values(**values)
        ),
    )
    if result.rowcount == 0:
        raise GenerationStateError(
            f"cannot commit unknown generation {generation_id}"
        )

    # The head upsert must see the committed generation row, so flush first.
    await session.flush()

    bucket_id, key = await _bucket_key_for(session, generation_id)
    await _advance_head(session, bucket_id, key, generation_id)
    await session.flush()


async def _advance_head(
    session: AsyncSession,
    bucket_id: uuid.UUID,
    key: str,
    generation_id: uuid.UUID,
) -> None:
    """Advance the object head, delegating to heads.advance_head when present.

    Prefers the sibling helper (task 16.3) so there is a single owner of the
    ``object_heads`` upsert. Falls back to an equivalent one-statement upsert if
    that module is not yet importable or does not expose ``advance_head``.
    """
    try:
        from gateway.app.storage import heads  # local import: sibling task 16.3
    except ImportError:
        heads = None  # type: ignore[assignment]

    advance_head = getattr(heads, "advance_head", None)
    if advance_head is not None:
        await advance_head(session, bucket_id, key, generation_id)
        return

    # Fallback upsert, kept semantically identical to the heads helper: one
    # statement that inserts the head or repoints an existing one.
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from gateway.app.db.models import ObjectHead

    stmt = pg_insert(ObjectHead).values(
        bucket_id=bucket_id, key=key, generation_id=generation_id
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[ObjectHead.bucket_id, ObjectHead.key],
        set_={"generation_id": stmt.excluded.generation_id},
    )
    await session.execute(stmt)


async def _bucket_key_for(
    session: AsyncSession, generation_id: uuid.UUID
) -> tuple[uuid.UUID, str]:
    """Load the (bucket_id, key) of a generation for the head upsert.

    Raises:
        GenerationStateError: If the generation row is missing.
    """
    from sqlalchemy import select

    row = (
        await session.execute(
            select(Generation.bucket_id, Generation.key).where(
                Generation.id == generation_id
            )
        )
    ).first()
    if row is None:
        raise GenerationStateError(
            f"generation {generation_id} vanished before head advance"
        )
    return row[0], row[1]


async def _set_state(
    session: AsyncSession, generation_id: uuid.UUID, **values: Any
) -> None:
    """Apply a state transition to one generation row within the caller's txn.

    Raises:
        GenerationStateError: If no generation row matches ``generation_id``.
    """
    result = cast(
        "CursorResult[Any]",
        await session.execute(
            update(Generation).where(Generation.id == generation_id).values(**values)
        ),
    )
    if result.rowcount == 0:
        raise GenerationStateError(
            f"cannot transition unknown generation {generation_id}"
        )
    await session.flush()


async def mark_uploading(
    session: AsyncSession, generation_id: uuid.UUID
) -> None:
    """Transition a generation to UPLOADING when a worker claims its job.

    Args:
        session: The active async session.
        generation_id: The generation whose upload has begun.

    Raises:
        GenerationStateError: If the generation row is missing.
    """
    await _set_state(session, generation_id, state=STATE_UPLOADING)


async def mark_retry_wait(
    session: AsyncSession, generation_id: uuid.UUID
) -> None:
    """Transition a generation to RETRY_WAIT after a Telegram error.

    Args:
        session: The active async session.
        generation_id: The generation whose upload failed transiently.

    Raises:
        GenerationStateError: If the generation row is missing.
    """
    await _set_state(session, generation_id, state=STATE_RETRY_WAIT)


async def mark_failed(session: AsyncSession, generation_id: uuid.UUID) -> None:
    """Transition a generation to FAILED after retries are exhausted.

    The staged file is retained for manual inspection or operator-initiated
    retry; this helper only moves the row's state.

    Args:
        session: The active async session.
        generation_id: The generation that could not be uploaded.

    Raises:
        GenerationStateError: If the generation row is missing.
    """
    await _set_state(session, generation_id, state=STATE_FAILED)


async def mark_upload_failed(
    session: AsyncSession, generation_id: uuid.UUID
) -> None:
    """Transition a generation to UPLOAD_FAILED after Commit Record recovery fails.

    Called by the Upload Worker's partial-commit-record recovery path when a Blob
    Manifest exists in Telegram for the generation but the Commit Record could not
    be written after five backed-off attempts (Requirement 14.3). The staged file
    and the already-uploaded manifest are retained; only the row's state moves, so
    an operator can locate and act on the stuck generation.

    Args:
        session: The active async session.
        generation_id: The generation whose Commit Record could not be written.

    Raises:
        GenerationStateError: If the generation row is missing.
    """
    await _set_state(session, generation_id, state=STATE_UPLOAD_FAILED)


async def mark_superseded(
    session: AsyncSession, generation_id: uuid.UUID
) -> None:
    """Transition a COMMITTED generation to SUPERSEDED.

    Called when a newer generation overwrites the key or a delete tombstone is
    written, so the old generation becomes eligible for garbage collection once
    its references drop.

    Args:
        session: The active async session.
        generation_id: The superseded generation.

    Raises:
        GenerationStateError: If the generation row is missing.
    """
    await _set_state(session, generation_id, state=STATE_SUPERSEDED)


async def mark_deleted(
    session: AsyncSession, generation_id: uuid.UUID
) -> None:
    """Transition a COMMITTED generation to DELETED on DeleteObject.

    Called once the Tombstone message is confirmed in Telegram (Requirement
    9.3): the durable record of the delete already exists in the channel, so the
    index catches up by flipping the generation's state. The row is retained
    (not removed) so the generation remains eligible for garbage collection and
    so a subsequent recovery sees a state consistent with the Tombstone it will
    replay. Clearing the key's object head is the caller's separate step.

    Args:
        session: The active async session; the transaction boundary is the
            caller's, so the state flip and the head clear commit together.
        generation_id: The generation the Tombstone references.

    Raises:
        GenerationStateError: If the generation row is missing.
    """
    await _set_state(session, generation_id, state=STATE_DELETED)


async def mark_purged(session: AsyncSession, generation_id: uuid.UUID) -> None:
    """Transition a SUPERSEDED/DELETED generation to PURGED after GC_GRACE.

    The terminal transition of the state machine (design "Generation State
    Machine": ``SUPERSEDED -> PURGED``). The GC Worker calls this once it has
    deleted the generation's ``tgo3`` messages from Telegram — the durable data
    is gone, so the index catches up by flipping the state. The row is **kept**
    in PostgreSQL for audit (design: "PURGED generations are retained in
    PostgreSQL for audit; only the Telegram messages are deleted"); nothing here
    deletes it.

    Args:
        session: The active async session; the transaction boundary is the
            caller's (the GC Worker purges one generation per transaction).
        generation_id: The generation whose Telegram messages have been deleted.

    Raises:
        GenerationStateError: If the generation row is missing.
    """
    await _set_state(session, generation_id, state=STATE_PURGED)

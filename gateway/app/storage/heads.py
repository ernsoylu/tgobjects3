"""Object-head pointer helpers: read and atomically advance the committed
generation for a ``(bucket_id, key)`` pair.

The ``object_heads`` table holds exactly one row per live key — the pointer to
the generation that a ``GetObject`` should resolve. Every committed write moves
this pointer forward (Requirement 6.13): when the Commit Record write to
Telegram is confirmed, the Gateway marks the generation ``COMMITTED`` and
advances the key's head "as a single atomic database transaction". That atomicity
is the caller's responsibility — :func:`advance_head` issues one statement and
deliberately does **not** commit, so it can be composed into the same transaction
that flips the generation state (the sibling ``generations`` helper calls it
rather than duplicating the upsert).

The advance is expressed as a PostgreSQL ``INSERT ... ON CONFLICT DO UPDATE`` so
first-write (no row yet) and overwrite (row exists) collapse into a single
round-trip with no read-modify-write race: two concurrent commits for the same
key both reach the database, and the one that lands second simply overwrites the
``generation_id``. The last writer wins at the storage layer; ordering between
genuinely concurrent commits to one key is resolved by the caller's transaction
isolation, not here.
"""

from __future__ import annotations

import uuid

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.app.db.models import ObjectHead


async def get_head(
    session: AsyncSession, bucket_id: uuid.UUID, key: str
) -> uuid.UUID | None:
    """Return the committed generation id a key currently points at.

    Args:
        session: The active ``AsyncSession``. No transaction control is performed
            here; the read participates in whatever transaction the caller holds.
        bucket_id: The owning bucket's id.
        key: The object key within the bucket.

    Returns:
        The ``generation_id`` the key's head points at, or ``None`` when the key
        has no head row (never written, or its head was removed).
    """
    result = await session.execute(
        select(ObjectHead.generation_id).where(
            ObjectHead.bucket_id == bucket_id,
            ObjectHead.key == key,
        )
    )
    return result.scalar_one_or_none()


async def advance_head(
    session: AsyncSession,
    bucket_id: uuid.UUID,
    key: str,
    generation_id: uuid.UUID,
) -> None:
    """Atomically point a key's head at ``generation_id`` in one statement.

    Issues a single PostgreSQL ``INSERT ... ON CONFLICT DO UPDATE`` keyed on the
    ``(bucket_id, key)`` primary key: it inserts the head row on first write and
    overwrites the ``generation_id`` of an existing row otherwise. Collapsing both
    cases into one statement avoids a read-modify-write window between concurrent
    commits to the same key.

    This coroutine deliberately does **not** commit. Advancing the head is one
    step of the atomic commit transaction described in Requirement 6.13 (mark the
    generation ``COMMITTED`` *and* advance the head together), so transaction
    control is left to the caller. The sibling ``generations`` helper calls this
    function inside that transaction rather than duplicating the upsert.

    Args:
        session: The active ``AsyncSession`` whose transaction this statement
            joins. The caller is responsible for committing (or rolling back).
        bucket_id: The owning bucket's id.
        key: The object key within the bucket.
        generation_id: The committed generation the head should point at.
    """
    statement = insert(ObjectHead).values(
        bucket_id=bucket_id,
        key=key,
        generation_id=generation_id,
    )
    statement = statement.on_conflict_do_update(
        index_elements=[ObjectHead.bucket_id, ObjectHead.key],
        set_={"generation_id": statement.excluded.generation_id},
    )
    await session.execute(statement)


async def clear_head(
    session: AsyncSession, bucket_id: uuid.UUID, key: str
) -> None:
    """Remove a key's head row so the key no longer resolves to a generation.

    Called on DeleteObject once the Tombstone is durable (Requirement 9.3): with
    the head gone, a subsequent ``GetObject``/``HeadObject`` finds no live
    generation and returns 404, which matches what a recovery would reconstruct
    (the rebuild deletes the head row for a tombstoned key). Deleting a key that
    has no head row is a harmless no-op, so this is safe to call for a key whose
    head was never written.

    This coroutine deliberately does **not** commit. The head clear belongs to
    the same transaction that marks the generation DELETED, so transaction
    control is left to the caller.

    Args:
        session: The active ``AsyncSession`` whose transaction this statement
            joins. The caller is responsible for committing (or rolling back).
        bucket_id: The owning bucket's id.
        key: The object key within the bucket.
    """
    await session.execute(
        delete(ObjectHead).where(
            ObjectHead.bucket_id == bucket_id,
            ObjectHead.key == key,
        )
    )


__all__ = [
    "advance_head",
    "clear_head",
    "get_head",
]

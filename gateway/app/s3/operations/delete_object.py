"""DeleteObject handler — write a Tombstone, then mark the generation DELETED.

This module implements the ``DELETE /{bucket}/{key}`` operation (Requirements
9.3, 9.4, 9.5). DeleteObject is a *generation* like any other write (design
"Generation State Machine"): the durable record of the delete is a Tombstone
message (caption ``tgo3-t-<generation_id>``) in the bucket's Telegram channel,
and the PostgreSQL index only catches up *after* Telegram confirms it.

Ordering is the whole contract
------------------------------
Durability lives in Telegram, not the database (product guarantee), so the two
side effects of a delete must happen in a strict order:

1. **Write the Tombstone to Telegram first.** Only a confirmed ``message_id``
   proves the delete is durable.
2. **Then, in one transaction, mark the current generation ``DELETED`` and clear
   the key's object head.** A reader never sees a half-deleted state: either the
   head still points at the live generation (Tombstone not yet durable) or the
   head is gone (Tombstone durable and index caught up).

If the Telegram write fails, the index is left exactly as it was and the handler
returns ``500`` with no body (Requirement 9.4). The DELETED flip happens
*after* the confirmed ``message_id`` and never before, so a Telegram failure can
never strand the index in a deleted state.

Idempotent delete
-----------------
A delete of a key with no live head is a success, not an error: the handler
returns ``204`` with no body and writes nothing to Telegram (Requirement 9.5).
This matches S3, where DeleteObject is idempotent.

Dependency wiring
-----------------
The router's :class:`~gateway.app.s3.router.S3HandlerContext` carries the
request, target and settings but not the storage/Telegram dependencies, so this
handler is a *class* that captures its session factory and
:class:`~gateway.app.telegram.port.TelegramPort` at construction time and
exposes the :class:`~gateway.app.s3.router.S3Handler` call signature through
:meth:`__call__`. The application wiring registers one instance on the
``DELETE_OBJECT`` slot.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.app.db.models import Bucket, Generation
from gateway.app.db.session import AsyncSessionFactory
from gateway.app.format import tombstone as tombstone_codec
from gateway.app.format.types import Tombstone
from gateway.app.s3.router import S3HandlerContext, error_response
from gateway.app.storage import generations, heads
from gateway.app.telegram.errors import TelegramError
from gateway.app.telegram.port import TelegramPort

__all__ = ["DeleteObjectHandler"]

#: Caption prefix for a Tombstone message, mirroring the design's "Telegram
#: Message Format" table (``tgo3-t-<generation_id>``) and the prefix the
#: recovery CLI classifies on. Kept local to the handler that writes it.
_PREFIX_TOMBSTONE = "tgo3-t-"


@dataclass(frozen=True)
class _ResolvedHead:
    """The live generation a DeleteObject targets, plus its channel.

    Produced by :meth:`DeleteObjectHandler._resolve_target`; ``None`` where a
    piece is missing so the caller can take the idempotent no-op path.

    Attributes:
        bucket_id: The owning bucket's id.
        channel_id: The bucket's Telegram channel, where the Tombstone is sent.
        generation_id: The committed generation the key's head points at.
        generation_number: The generation's monotonic per-key ordinal, recorded
            in the Tombstone so recovery can order the key's history.
    """

    bucket_id: uuid.UUID
    channel_id: int
    generation_id: uuid.UUID
    generation_number: int


class DeleteObjectHandler:
    """Handle ``DELETE /{bucket}/{key}`` (Requirements 9.3, 9.4, 9.5).

    Holds the dependencies the router context does not carry: the database
    session factory and the Telegram port. One instance is registered on the
    ``DELETE_OBJECT`` slot of the router's handler registry.
    """

    def __init__(
        self,
        session_factory: AsyncSessionFactory,
        telegram: TelegramPort,
    ) -> None:
        """Capture the storage and Telegram dependencies for every call.

        Args:
            session_factory: Hands out request-scoped async sessions.
            telegram: The boundary used to write the Tombstone message.
        """
        self._session_factory = session_factory
        self._telegram = telegram

    async def __call__(self, context: S3HandlerContext) -> Response:
        """Execute DeleteObject and return the S3 HTTP response.

        See the module docstring for the ordering contract. The target bucket
        and key come from the already-resolved context; the request body is
        irrelevant to a delete and is not read.

        Returns:
            ``204`` with no body on success and on a non-existent key
            (Requirements 9.3, 9.5); ``500`` with no body if the Telegram write
            fails (Requirement 9.4); the standard error document for a malformed
            target (missing bucket or key).
        """
        bucket_name = context.target.bucket
        key = context.target.key
        if bucket_name is None or key is None:
            # DeleteObject addresses an object; a missing key is a routing error
            # rather than a delete of "no key". The router only dispatches here
            # for object-level requests, so this is defensive.
            return error_response(
                code="InvalidRequest",
                message="DeleteObject requires a bucket and key",
                status_code=400,
                resource=f"/{bucket_name}" if bucket_name else None,
            )

        # Resolve the live head in its own session; nothing is mutated yet.
        async with self._session_factory.session() as session:
            resolved = await self._resolve_target(session, bucket_name, key)

        if resolved is None:
            # Non-existent key (unknown bucket, or no live head): idempotent
            # success with no Telegram write (Requirement 9.5).
            return Response(status_code=204)

        # 1. Write the Tombstone to Telegram FIRST. Only a confirmed message_id
        #    makes the delete durable; until then the index is untouched.
        tombstone = Tombstone(
            format_version=1,
            generation_id=resolved.generation_id,
            bucket_id=resolved.bucket_id,
            key=key,
            generation_number=resolved.generation_number,
        )
        try:
            await self._telegram.send_document(
                resolved.channel_id,
                tombstone_codec.serialise(tombstone),
                file_name=f"{resolved.generation_id.hex}.tombstone",
                caption=f"{_PREFIX_TOMBSTONE}{resolved.generation_id.hex}",
            )
        except TelegramError:
            # The channel write failed: leave the index entry unchanged and
            # return 500 with no body (Requirement 9.4). The generation is still
            # COMMITTED and the head still points at it, so a retry re-resolves
            # the same live head.
            return Response(status_code=500)

        # 2. The Tombstone is durable. Catch the index up in one transaction so a
        #    reader never sees the generation DELETED while its head still points
        #    at it: mark the generation DELETED and clear the key's head
        #    together (Requirement 9.3).
        async with self._session_factory.session() as session:
            await generations.mark_deleted(
                session, resolved.generation_id
            )
            await heads.clear_head(session, resolved.bucket_id, key)

        return Response(status_code=204)

    async def _resolve_target(
        self, session: AsyncSession, bucket_name: str, key: str
    ) -> _ResolvedHead | None:
        """Resolve the bucket, its channel, and the key's live head generation.

        Returns ``None`` — the idempotent no-op signal — when the bucket is
        unknown or the key has no object head (Requirement 9.5). Otherwise
        returns the generation id to tombstone together with the channel to
        write it to and the generation number recorded in the Tombstone.

        Args:
            session: A read-only session; no state is mutated here.
            bucket_name: The target bucket name from the request.
            key: The target object key from the request.
        """
        bucket_row = (
            await session.execute(
                select(Bucket.id, Bucket.channel_id).where(
                    Bucket.name == bucket_name
                )
            )
        ).first()
        if bucket_row is None:
            return None
        bucket_id, channel_id = bucket_row

        generation_id = await heads.get_head(session, bucket_id, key)
        if generation_id is None:
            return None

        generation_number = await self._generation_number(
            session, bucket_id, key
        )
        return _ResolvedHead(
            bucket_id=bucket_id,
            channel_id=channel_id,
            generation_id=generation_id,
            generation_number=generation_number,
        )

    @staticmethod
    async def _generation_number(
        session: AsyncSession, bucket_id: uuid.UUID, key: str
    ) -> int:
        """Return the monotonic generation number of the key's current head.

        A key's history is numbered by the count of its COMMITTED generations
        (the same ordinal the Upload Worker records in each Commit Record). The
        current head is the latest such write, so its number is that count minus
        one. The Tombstone carries this number so recovery marks exactly the head
        generation deleted and leaves any superseded generations untouched
        (mirrors the recovery CLI's head resolution).
        """
        count = (
            await session.execute(
                select(func.count())
                .select_from(Generation)
                .where(
                    Generation.bucket_id == bucket_id,
                    Generation.key == key,
                    Generation.state == generations.STATE_COMMITTED,
                )
            )
        ).scalar_one()
        return max(int(count) - 1, 0)

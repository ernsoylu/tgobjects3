"""DeleteObjects (batch delete) handler — ``POST /{bucket}?delete``.

This module implements the S3 *DeleteObjects* operation, which removes up to a
thousand keys in a single request and reports the outcome of every key
independently (requirements 9.6, 9.7):

* **Requirement 9.7** — a request naming *more than* 1000 keys is rejected
  outright with ``400 MalformedXML`` and no per-key processing happens. The
  count is checked before any key is touched, so an over-large batch never
  partially deletes.
* **Requirement 9.6** — a request naming 1 to 1000 keys processes each key
  *independently*: one key's failure never aborts the batch, and the response
  body carries a success (``<Deleted>``) or failure (``<Error>``) entry for
  every requested key. In ``Quiet`` mode only the failures are reported.

Per-key deletion
----------------
Each key is deleted by the same two-phase sequence a single ``DeleteObject``
uses, factored here into :func:`delete_one_key` so the batch can run it once per
key with each key's errors contained:

1. Resolve the key's current committed generation via its ``object_heads``
   pointer. A key with no head (never written, or already deleted) is a no-op
   *success* — S3 batch delete, like single-key DeleteObject (requirement 9.5),
   reports success for a key that does not exist.
2. Otherwise write a Tombstone message to the bucket's Telegram channel
   (caption ``tgo3-t-<generation_id>``), which is the durability boundary of the
   delete, then — only once Telegram confirms the write — mark the delete
   generation ``DELETED``, supersede the previously committed generation, and
   drop the key's head so subsequent reads resolve to 404. Telegram is written
   *before* the index is mutated so that a Telegram failure leaves the index
   untouched (requirement 9.4), surfacing as a per-key error rather than a lost
   object.

The per-key helper owns its own database transaction so that a failure on one
key rolls back only that key's index writes, leaving the keys processed before
it durably deleted and letting the keys after it proceed. This isolation is what
makes "process each key independently" (requirement 9.6) hold even when a
Telegram write fails midway through a batch.

The handler is defined as an :class:`~gateway.app.s3.router.S3Handler`-compatible
class whose dependencies — the database session factory and the
:class:`~gateway.app.telegram.port.TelegramPort` — are injected at construction,
matching the project's ports-and-adapters convention. The Telegram transport is
only ever reached through the port, so tests supply a fake.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, cast

from fastapi import Response
from sqlalchemy import CursorResult, func, select, update
from sqlalchemy import delete as sql_delete
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.app.db.models import Bucket, Generation, ObjectHead
from gateway.app.db.session import AsyncSessionFactory
from gateway.app.format import tombstone as tombstone_codec
from gateway.app.format.types import FORMAT_VERSION, Tombstone
from gateway.app.s3 import xml_codecs
from gateway.app.s3.router import S3HandlerContext, error_response
from gateway.app.storage import generations as gen_storage
from gateway.app.storage import heads as heads_storage
from gateway.app.telegram.errors import TelegramError
from gateway.app.telegram.port import TelegramPort

__all__ = [
    "MAX_DELETE_KEYS",
    "STATE_DELETED",
    "DeleteObjectsHandler",
    "KeyDeleteOutcome",
    "delete_one_key",
]

#: The maximum number of keys S3 permits in a single DeleteObjects request.
#: A request naming more than this is rejected with ``400 MalformedXML`` before
#: any key is processed (requirement 9.7).
MAX_DELETE_KEYS = 1000

#: The ``generations.state`` value for a delete generation whose Tombstone has
#: been durably written. The recovery CLI uses this same uppercase token when it
#: replays a Tombstone (see :mod:`gateway.app.storage.generations`), so a
#: rebuilt index agrees with one mutated live.
STATE_DELETED = "DELETED"

#: Caption prefix for a Tombstone message, mirroring the format layer
#: (``tgo3-t-<generation_id>`` — see :class:`gateway.app.format.types.Tombstone`).
_TOMBSTONE_CAPTION_PREFIX = "tgo3-t-"

#: File name attached to the Tombstone document. Telegram requires a name; the
#: logical identity lives in the caption, so this is purely cosmetic.
_TOMBSTONE_FILE_NAME = "tombstone.bin"


@dataclass(frozen=True)
class KeyDeleteOutcome:
    """The result of deleting one key, framed for the per-key response body.

    Exactly one of :attr:`error_code` / :attr:`error_message` is populated: when
    both are ``None`` the delete succeeded (which includes the no-op success for
    a key that did not exist), otherwise they carry the S3 error code and message
    reported for that key. ``key`` echoes the requested key so the batch can
    build the response without re-pairing results to inputs.
    """

    key: str
    error_code: str | None = None
    error_message: str | None = None

    @property
    def ok(self) -> bool:
        """Whether this key was deleted (or did not exist), i.e. not an error."""
        return self.error_code is None


class DeleteObjectsHandler:
    """Handle ``POST /{bucket}?delete`` — delete up to 1000 keys in one request.

    An :class:`~gateway.app.s3.router.S3Handler`: the router hands it a
    :class:`~gateway.app.s3.router.S3HandlerContext` whose request has already
    been authenticated and whose addressing is resolved, and it returns the S3
    ``DeleteResult`` XML response (or a ``400 MalformedXML`` error document).

    Dependencies are injected so the handler stays testable: ``sessions`` opens
    the database transactions each key's delete runs in, and ``telegram`` is the
    only path to the bucket channel.
    """

    def __init__(
        self, sessions: AsyncSessionFactory, telegram: TelegramPort
    ) -> None:
        """Store the session factory and Telegram port used for every request."""
        self._sessions = sessions
        self._telegram = telegram

    async def __call__(self, context: S3HandlerContext) -> Response:
        """Execute the batch delete and return its HTTP response.

        Rejects a malformed body or an over-large batch with ``400`` before any
        key is touched (requirement 9.7), then processes each requested key
        independently and frames a per-key result for every one (requirement
        9.6), honouring the request's ``Quiet`` flag.
        """
        bucket_name = context.target.bucket
        if bucket_name is None:
            # The router only classifies DeleteObjects for a bucket-level path,
            # so a missing bucket here is a routing invariant violation.
            return error_response(
                code="MethodNotAllowed",
                message="DeleteObjects requires a bucket",
                status_code=405,
            )

        body = await context.request.body()
        resource = f"/{bucket_name}"
        try:
            request = xml_codecs.deserialise_delete_objects_request(body)
        except xml_codecs.MalformedXMLError as exc:
            return error_response(
                code="MalformedXML",
                message=str(exc),
                status_code=400,
                resource=resource,
            )

        # Requirement 9.7: reject > 1000 keys with 400 before processing any key.
        if len(request.objects) > MAX_DELETE_KEYS:
            return error_response(
                code="MalformedXML",
                message=(
                    f"the batch delete request contains {len(request.objects)} "
                    f"keys, which exceeds the maximum of {MAX_DELETE_KEYS}"
                ),
                status_code=400,
                resource=resource,
            )

        outcomes = [
            await self._delete_key(bucket_name, entry.key)
            for entry in request.objects
        ]
        return _render_response(outcomes, quiet=request.quiet, resource=resource)

    async def _delete_key(self, bucket_name: str, key: str) -> KeyDeleteOutcome:
        """Delete one key in its own transaction, containing any failure.

        Wraps :func:`delete_one_key` so an exception deleting one key becomes
        that key's error entry rather than aborting the whole batch (requirement
        9.6). A fresh session per key gives each delete an independent
        transaction boundary: a rolled-back failure never undoes an earlier key's
        committed delete.
        """
        try:
            async with self._sessions.session() as session:
                return await delete_one_key(
                    session,
                    telegram=self._telegram,
                    bucket_name=bucket_name,
                    key=key,
                )
        except TelegramError as exc:
            # Telegram write failed: the per-key transaction rolled back, so the
            # index is unchanged for this key (requirement 9.4). Report it as a
            # retryable per-key error and keep processing the rest of the batch.
            return KeyDeleteOutcome(
                key=key,
                error_code="InternalError",
                error_message=f"failed to write tombstone to Telegram: {exc}",
            )
        except Exception as exc:  # noqa: BLE001 - isolate per-key failures
            # Any other failure is likewise contained to this key so one bad key
            # cannot sink the batch (requirement 9.6).
            return KeyDeleteOutcome(
                key=key,
                error_code="InternalError",
                error_message=str(exc) or "internal error",
            )


async def delete_one_key(
    session: AsyncSession,
    *,
    telegram: TelegramPort,
    bucket_name: str,
    key: str,
) -> KeyDeleteOutcome:
    """Delete a single key, writing its Tombstone before mutating the index.

    This is the shared per-key delete used by the batch handler (and reusable by
    the single-key DeleteObject handler) so both apply identical semantics:

    * A key whose bucket is unknown is reported as ``NoSuchBucket``.
    * A key with no committed head is a no-op success — deleting a key that does
      not exist succeeds (requirement 9.5), so the batch reports it as deleted.
    * Otherwise a Tombstone is written to the bucket channel *first*; only once
      Telegram confirms it does the function mark a new ``DELETED`` generation,
      supersede the previously committed one, and drop the key's head. Ordering
      the Telegram write ahead of the index mutation is what lets a Telegram
      failure leave the index unchanged (requirement 9.4) — the raised
      :class:`~gateway.app.telegram.errors.TelegramError` propagates to the
      caller, whose transaction rolls back.

    The caller owns ``session``'s transaction boundary (the batch handler uses a
    session-context-manager that commits on success and rolls back on error), so
    this function flushes but never commits.

    Args:
        session: The active async session; its transaction is the caller's.
        telegram: The Telegram port the Tombstone is written through.
        bucket_name: The S3 bucket name the key lives in.
        key: The object key to delete.

    Returns:
        A :class:`KeyDeleteOutcome` describing success or the per-key error.

    Raises:
        TelegramError: If the Tombstone write to Telegram fails. The caller
            translates this into a per-key error with the index left unchanged.
    """
    bucket = await _load_bucket(session, bucket_name)
    if bucket is None:
        return KeyDeleteOutcome(
            key=key,
            error_code="NoSuchBucket",
            error_message=f"the specified bucket {bucket_name!r} does not exist",
        )

    head_generation_id = await heads_storage.get_head(session, bucket.id, key)
    if head_generation_id is None:
        # Deleting a key that does not exist succeeds silently (requirement 9.5),
        # so the batch records it as deleted with no Telegram or index work.
        return KeyDeleteOutcome(key=key)

    generation_number = await _next_generation_number(session, bucket.id, key)

    # Reserve a DELETED generation id up front so the Tombstone caption and the
    # generation row that records the delete share one identity (and recovery can
    # correlate them). The row is written only after Telegram confirms, so the
    # Tombstone is the delete's durability boundary.
    delete_generation_id = uuid.uuid4()
    payload = tombstone_codec.serialise(
        Tombstone(
            format_version=FORMAT_VERSION,
            generation_id=delete_generation_id,
            bucket_id=bucket.id,
            key=key,
            generation_number=generation_number,
        )
    )

    # Telegram first: on failure this raises before any index mutation, so the
    # caller's transaction rolls back with the index untouched (requirement 9.4).
    commit_message_id = await telegram.send_document(
        bucket.channel_id,
        payload,
        _TOMBSTONE_FILE_NAME,
        f"{_TOMBSTONE_CAPTION_PREFIX}{delete_generation_id}",
    )

    await _record_delete(
        session,
        bucket_id=bucket.id,
        key=key,
        delete_generation_id=delete_generation_id,
        superseded_generation_id=head_generation_id,
        commit_message_id=commit_message_id,
    )
    return KeyDeleteOutcome(key=key)


async def _load_bucket(session: AsyncSession, bucket_name: str) -> Bucket | None:
    """Load the bucket row for ``bucket_name``, or ``None`` if it does not exist."""
    result = await session.execute(
        select(Bucket).where(Bucket.name == bucket_name)
    )
    return result.scalar_one_or_none()


async def _next_generation_number(
    session: AsyncSession, bucket_id: uuid.UUID, key: str
) -> int:
    """Return the monotonic generation number for the next write to a key.

    Counts the generations already recorded for ``(bucket_id, key)``; the new
    delete generation is numbered after them. This mirrors the ordinal the
    upload worker records in a Commit Record so recovery can order a key's
    history including its deletes.
    """
    count = (
        await session.execute(
            select(func.count())
            .select_from(Generation)
            .where(
                Generation.bucket_id == bucket_id,
                Generation.key == key,
            )
        )
    ).scalar_one()
    return int(count)


async def _record_delete(
    session: AsyncSession,
    *,
    bucket_id: uuid.UUID,
    key: str,
    delete_generation_id: uuid.UUID,
    superseded_generation_id: uuid.UUID,
    commit_message_id: int,
) -> None:
    """Record a confirmed delete in the index: new DELETED gen, supersede, unhead.

    Called only after the Tombstone write to Telegram is confirmed. Within the
    caller's transaction it:

    1. Inserts the delete generation (``kind="delete"``, no body) and advances it
       straight to ``DELETED`` with the Tombstone's ``commit_message_id``.
    2. Marks the previously committed generation ``SUPERSEDED`` so it becomes
       eligible for garbage collection.
    3. Removes the key's ``object_heads`` row so a subsequent GET/HEAD resolves to
       404.

    All three happen in one transaction (the caller's), so a reader never sees a
    half-applied delete.
    """
    # 1. Insert the delete generation as a STAGED, body-less row and its upload
    #    job, then advance it to DELETED. insert_staged flushes so the row exists
    #    for the state transition that follows in this same transaction.
    await gen_storage.insert_staged(
        session,
        bucket_id=bucket_id,
        key=key,
        kind="delete",
        size=0,
        staging_path=None,
        generation_id=delete_generation_id,
    )
    await _set_generation_state(
        session,
        delete_generation_id,
        state=STATE_DELETED,
        commit_message_id=commit_message_id,
    )

    # 2. The generation this key used to resolve to is now superseded.
    await gen_storage.mark_superseded(session, superseded_generation_id)

    # 3. Drop the head so reads of the key return 404.
    await _remove_head(session, bucket_id, key)


async def _set_generation_state(
    session: AsyncSession,
    generation_id: uuid.UUID,
    *,
    state: str,
    commit_message_id: int,
) -> None:
    """Set a generation's ``state`` and ``commit_message_id`` in one statement.

    Used to advance the freshly inserted delete generation to ``DELETED`` while
    recording the Tombstone's message id, which is the delete's durability
    anchor. Flushes so the change is visible to later statements in the caller's
    transaction.

    Raises:
        GenerationStateError: If no generation row matches ``generation_id``.
    """
    result = cast(
        "CursorResult[Any]",
        await session.execute(
            update(Generation)
            .where(Generation.id == generation_id)
            .values(state=state, commit_message_id=commit_message_id)
        ),
    )
    if result.rowcount == 0:
        raise gen_storage.GenerationStateError(
            f"cannot transition unknown generation {generation_id}"
        )
    await session.flush()


async def _remove_head(
    session: AsyncSession, bucket_id: uuid.UUID, key: str
) -> None:
    """Delete the ``object_heads`` row for a key so reads resolve to 404.

    The delete has no live committed generation to point the head at, so the
    pointer is removed rather than advanced. Flushes within the caller's
    transaction.
    """
    await session.execute(
        sql_delete(ObjectHead).where(
            ObjectHead.bucket_id == bucket_id,
            ObjectHead.key == key,
        )
    )
    await session.flush()


def _render_response(
    outcomes: list[KeyDeleteOutcome], *, quiet: bool, resource: str
) -> Response:
    """Frame per-key outcomes into the S3 ``DeleteResult`` XML response.

    Honours the request's ``Quiet`` flag (requirement 9.6): in quiet mode only
    the error entries are emitted; otherwise every successfully deleted key gets
    a ``<Deleted>`` entry alongside the ``<Error>`` entries. The response is
    always HTTP 200 with an ``application/xml`` body — individual key failures
    live inside the body, not in the HTTP status.
    """
    deleted = tuple(
        xml_codecs.DeletedObject(key=o.key) for o in outcomes if o.ok
    )
    errors = tuple(
        xml_codecs.DeleteError(
            key=o.key,
            code=o.error_code or "InternalError",
            message=o.error_message or "internal error",
        )
        for o in outcomes
        if not o.ok
    )
    body = xml_codecs.serialise_delete_objects_response(
        xml_codecs.DeleteObjectsResponse(
            deleted=() if quiet else deleted,
            errors=errors,
        )
    )
    return Response(content=body, status_code=200, media_type="application/xml")

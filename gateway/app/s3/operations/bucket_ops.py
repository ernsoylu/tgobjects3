"""Bucket operations — ListBuckets, CreateBucket, HeadBucket, DeleteBucket,
GetBucketLocation (Requirement 4).

This module implements the five bucket-level S3 operations the gateway exposes
(requirement 4.1). Each operation is a class holding the dependencies the
router context does not carry — the database
:class:`~gateway.app.db.session.AsyncSessionFactory` and, where Telegram access
is needed, the :class:`~gateway.app.telegram.port.TelegramPort` — and exposes the
:class:`~gateway.app.s3.router.S3Handler` async-callable protocol so each plugs
straight into the router's handler registry. This mirrors the dependency-
injection shape of the object handlers (``delete_object.py``, ``head_object.py``).
SigV4 authentication has already run in the router before any handler here is
invoked (requirement 6.1).

CreateBucket — the whole contract is ordering + rollback (requirement 4.2, 4.6)
-------------------------------------------------------------------------------
Durability lives in Telegram, not the database (product guarantee), so the three
side effects of a create happen in a strict order, each one rolled back if a
later step fails:

1. **Create the private Telegram channel.** A confirmed ``channel_id`` is the
   anchor the manifest and the index row both reference.
2. **Write the BucketManifest message to that channel** (caption
   ``tgo3-k-<kek_version>``), carrying the per-bucket KEK wrapped under the root
   key derived from ``TGOS3_MASTER_KEY``. The manifest is what lets recovery
   re-derive the bucket's KEK from the channel alone.
3. **Insert the ``buckets`` row** so the index can serve the bucket.

If any step fails, every preceding step is undone — the freshly created channel
is deleted, the half-inserted row is rolled back — and the handler returns HTTP
500 (requirement 4.2). A create for a name that already exists never reaches step
1: it short-circuits to HTTP 409 and touches neither Telegram nor the index
(requirement 4.6). The duplicate check and the row insert run in one transaction
so a losing racer surfaces as a 409 rather than a torn create.

DeleteBucket — refuse while non-empty, else tear down (requirements 4.3, 4.7)
----------------------------------------------------------------------------
A bucket is deletable only when it holds no live object. "Live" means a
generation in the ``COMMITTED`` state — the only state in which an object is
durable and observable (design "Generation State Machine"). If any committed
generation exists, the handler returns HTTP 409 and mutates nothing — neither
the channel nor the row (requirement 4.3). Otherwise it deletes the Telegram
channel, removes the ``buckets`` row, and returns HTTP 204 (requirement 4.7).
The channel is deleted before the row so a crash between the two leaves a
deletable (empty) bucket whose channel is already gone, which a retry resolves,
rather than an orphaned channel with no index row.

HeadBucket / GetBucketLocation
------------------------------
HeadBucket is a pure index existence probe: HTTP 200 (no body) when the name is
in ``buckets``, HTTP 404 otherwise (requirement 4.4). GetBucketLocation returns
the configured ``S3_REGION`` as a ``LocationConstraint`` document
(requirement 4.5).
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

from fastapi import Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.app.db.models import Bucket, Generation
from gateway.app.db.session import AsyncSessionFactory
from gateway.app.format import bucket_manifest as bucket_manifest_codec
from gateway.app.format import key_wrap
from gateway.app.format.types import BucketManifest
from gateway.app.s3 import xml_codecs
from gateway.app.s3.router import S3HandlerContext, error_response
from gateway.app.storage.generations import STATE_COMMITTED
from gateway.app.telegram.errors import TelegramError
from gateway.app.telegram.port import TelegramPort

__all__ = [
    "ListBucketsHandler",
    "CreateBucketHandler",
    "HeadBucketHandler",
    "DeleteBucketHandler",
    "GetBucketLocationHandler",
    "BUCKET_STATUS_ACTIVE",
    "INITIAL_KEK_VERSION",
    "BUCKET_KEK_LENGTH",
]

#: Caption prefix for a BucketManifest message, mirroring the design's "Telegram
#: Message Format" table (``tgo3-k-<version>``) and the prefix the recovery CLI
#: classifies on. Kept local to the handler that writes it.
_PREFIX_BUCKET_MANIFEST = "tgo3-k-"

#: Status recorded for a freshly created bucket row. A bucket is usable as soon
#: as its channel and manifest exist and the row is inserted.
BUCKET_STATUS_ACTIVE = "active"

#: ``kek_version`` of a bucket's first (and, until rotation, only) KEK. Key
#: rotation (a separate concern) bumps this and writes a new manifest.
INITIAL_KEK_VERSION = 1

#: Length in bytes of a per-bucket key-encryption key, matching the key
#: hierarchy in :mod:`gateway.app.format.key_wrap` (32-byte AES keys).
BUCKET_KEK_LENGTH = 32

#: Owner id/display name reported in the ``Owner`` block of a ListBuckets
#: response. The gateway is single-tenant, so a stable synthetic owner is used.
_OWNER_ID = "tgobjects3"
_OWNER_DISPLAY_NAME = "tgobjects3"

#: ``CreationDate`` reported for every bucket in a ListBuckets response. The
#: ``buckets`` table (design "PostgreSQL Schema") carries no creation timestamp —
#: durable truth lives in Telegram and the index is a rebuildable cache — so no
#: real per-bucket creation time is available to report. A fixed, well-formed
#: epoch timestamp keeps the response schema-valid for S3 clients that require
#: the element without inventing a misleading value.
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class ListBucketsHandler:
    """Serve ``GET /`` (ListBuckets) from the PostgreSQL index (req 4.1).

    One instance is registered on the ``LIST_BUCKETS`` slot. It reads every
    bucket name from the index and renders the standard ListBuckets XML body.
    """

    def __init__(self, session_factory: AsyncSessionFactory) -> None:
        """Store the session factory used to open per-request read sessions."""
        self._session_factory = session_factory

    async def __call__(self, context: S3HandlerContext) -> Response:
        """Return the caller's buckets as a ListBuckets XML document.

        Reads the bucket names in lexicographic order so the listing is stable,
        and renders them through
        :func:`gateway.app.s3.xml_codecs.serialise_list_buckets`.

        Args:
            context: The router-populated handler context.

        Returns:
            An ``application/xml`` ``200`` response carrying the bucket list.
        """
        async with self._session_factory.session() as session:
            names = (
                await session.execute(select(Bucket.name).order_by(Bucket.name))
            ).scalars().all()

        body = xml_codecs.serialise_list_buckets(
            xml_codecs.ListBucketsResponse(
                buckets=tuple(
                    xml_codecs.Bucket(name=name, creation_date=_EPOCH)
                    for name in names
                ),
                owner_id=_OWNER_ID,
                owner_display_name=_OWNER_DISPLAY_NAME,
            )
        )
        return Response(content=body, status_code=200, media_type="application/xml")


class CreateBucketHandler:
    """Handle ``PUT /{bucket}`` (CreateBucket) (requirements 4.2, 4.6).

    Holds the session factory and the Telegram port. Executes the ordered
    create — channel, manifest, index row — and rolls back every preceding step
    on any failure (see the module docstring). One instance is registered on the
    ``CREATE_BUCKET`` slot.
    """

    def __init__(
        self,
        session_factory: AsyncSessionFactory,
        telegram: TelegramPort,
    ) -> None:
        """Capture the storage and Telegram dependencies for every call.

        Args:
            session_factory: Hands out request-scoped async sessions.
            telegram: The boundary used to create the channel and write the
                BucketManifest.
        """
        self._session_factory = session_factory
        self._telegram = telegram

    async def __call__(self, context: S3HandlerContext) -> Response:
        """Create the bucket, rolling back all steps on any failure.

        Returns:
            ``200`` with no body on success (requirement 4.2); ``409`` when the
            name already exists, touching nothing (requirement 4.6); ``500`` with
            the standard error document if any step of the create sequence fails
            after the duplicate check, with all preceding steps rolled back
            (requirement 4.2); a ``400`` error document for a malformed target.
        """
        bucket_name = context.target.bucket
        if bucket_name is None:
            return error_response(
                code="InvalidBucketName",
                message="CreateBucket requires a bucket name",
                status_code=400,
            )

        # Short-circuit a duplicate before touching Telegram (requirement 4.6).
        async with self._session_factory.session() as session:
            if await self._bucket_exists(session, bucket_name):
                return self._conflict(bucket_name)

        return await self._create(context, bucket_name)

    async def _create(
        self, context: S3HandlerContext, bucket_name: str
    ) -> Response:
        """Run the ordered create sequence with step-by-step rollback.

        Each ``channel_id`` and manifest write is undone if a later step raises.
        The index insert and a second duplicate guard run in one transaction so a
        racing create surfaces as a 409 rather than two channels for one name.
        """
        root_key = key_wrap.derive_root_key(context.settings.TGOS3_MASTER_KEY)
        kek = os.urandom(BUCKET_KEK_LENGTH)
        kek_wrapped = key_wrap.wrap(root_key, kek)

        # 1. Create the private Telegram channel.
        try:
            channel_id = await self._telegram.create_channel(bucket_name)
        except TelegramError:
            return self._creation_failed(bucket_name)

        # 2. Write the BucketManifest. On failure, roll back the channel.
        manifest = BucketManifest(
            format_version=1,
            bucket_name=bucket_name,
            kek_wrapped=kek_wrapped,
            kek_version=INITIAL_KEK_VERSION,
        )
        try:
            await self._telegram.send_document(
                channel_id,
                bucket_manifest_codec.serialise(manifest),
                file_name=f"{bucket_name}.manifest",
                caption=f"{_PREFIX_BUCKET_MANIFEST}{INITIAL_KEK_VERSION}",
            )
        except TelegramError:
            await self._rollback_channel(channel_id)
            return self._creation_failed(bucket_name)

        # 3. Insert the index row. On failure, roll back manifest + channel.
        durability = self._durability_for(context.settings)
        try:
            inserted = await self._insert_row(
                bucket_name=bucket_name,
                channel_id=channel_id,
                kek_wrapped=kek_wrapped,
                durability=durability,
            )
        except IntegrityError:
            # A racing create won the unique-name constraint between the
            # duplicate check and here. Our channel is now redundant: tear it
            # down and report the race as a conflict (requirement 4.6).
            await self._rollback_channel(channel_id)
            return self._conflict(bucket_name)
        except Exception:
            await self._rollback_channel(channel_id)
            return self._creation_failed(bucket_name)

        if not inserted:
            # Duplicate detected inside the insert transaction; the row was not
            # written. Undo the channel and report the conflict.
            await self._rollback_channel(channel_id)
            return self._conflict(bucket_name)

        return Response(status_code=200)

    async def _insert_row(
        self,
        *,
        bucket_name: str,
        channel_id: int,
        kek_wrapped: bytes,
        durability: str,
    ) -> bool:
        """Insert the ``buckets`` row inside one transaction.

        Re-checks for a duplicate inside the same transaction that performs the
        insert so a create that raced past the pre-check still resolves to a
        single row. Returns ``True`` when the row was inserted, ``False`` when a
        duplicate name was found and nothing was written.
        """
        async with self._session_factory.session() as session:
            if await self._bucket_exists(session, bucket_name):
                return False
            session.add(
                Bucket(
                    id=uuid.uuid4(),
                    name=bucket_name,
                    channel_id=channel_id,
                    kek_wrapped=kek_wrapped,
                    kek_version=INITIAL_KEK_VERSION,
                    durability=durability,
                    created_by_us=True,
                    status=BUCKET_STATUS_ACTIVE,
                )
            )
            await session.flush()
        return True

    async def _rollback_channel(self, channel_id: int) -> None:
        """Best-effort delete of a channel created earlier in the sequence.

        Called when a later create step fails. A failure to delete the channel
        is swallowed: the create is already failing and the stronger signal to
        the client is the 500, not a secondary teardown error. A leaked channel
        is reconcilable out of band and never corrupts the index.
        """
        try:
            await self._telegram.delete_channel(channel_id)
        except TelegramError:
            return

    @staticmethod
    def _durability_for(settings: object) -> str:
        """Resolve the durability mode recorded on a new bucket.

        Uses the gateway default ``DEFAULT_DURABILITY`` (``sync``/``async``),
        falling back to ``sync`` if the configured value is unrecognised. Mirrors
        the resolution the PutObject handler applies when reading a bucket back.
        """
        default = getattr(settings, "DEFAULT_DURABILITY", "sync")
        return default if default in ("sync", "async") else "sync"

    @staticmethod
    async def _bucket_exists(session: AsyncSession, bucket_name: str) -> bool:
        """Return whether a bucket row with ``bucket_name`` exists."""
        result = await session.execute(
            select(Bucket.id).where(Bucket.name == bucket_name)
        )
        return result.scalar_one_or_none() is not None

    @staticmethod
    def _conflict(bucket_name: str) -> Response:
        """Build the ``409 BucketAlreadyExists`` document (requirement 4.6)."""
        return error_response(
            code="BucketAlreadyExists",
            message=(
                "The requested bucket name is not available; the bucket "
                "already exists."
            ),
            status_code=409,
            resource=f"/{bucket_name}",
        )

    @staticmethod
    def _creation_failed(bucket_name: str) -> Response:
        """Build the ``500 InternalError`` document for a failed create (4.2)."""
        return error_response(
            code="InternalError",
            message="Bucket creation failed; all changes were rolled back.",
            status_code=500,
            resource=f"/{bucket_name}",
        )


class HeadBucketHandler:
    """Serve ``HEAD /{bucket}`` (HeadBucket) from the index (requirement 4.4).

    One instance is registered on the ``HEAD_BUCKET`` slot. Pure existence
    probe: 200 (no body) when the bucket exists, 404 otherwise.
    """

    def __init__(self, session_factory: AsyncSessionFactory) -> None:
        """Store the session factory used to open per-request read sessions."""
        self._session_factory = session_factory

    async def __call__(self, context: S3HandlerContext) -> Response:
        """Return 200 if the bucket exists, 404 if not (requirement 4.4).

        HeadBucket carries no body in either direction, so both responses are a
        bare status.

        Args:
            context: The router-populated handler context carrying the resolved
                target.

        Returns:
            A header-only :class:`fastapi.Response`.
        """
        bucket_name = context.target.bucket
        if bucket_name is None:
            return Response(status_code=404)

        async with self._session_factory.session() as session:
            exists = (
                await session.execute(
                    select(Bucket.id).where(Bucket.name == bucket_name)
                )
            ).scalar_one_or_none() is not None

        return Response(status_code=200 if exists else 404)


class DeleteBucketHandler:
    """Handle ``DELETE /{bucket}`` (DeleteBucket) (requirements 4.3, 4.7).

    Holds the session factory and the Telegram port. Refuses to delete a bucket
    that still holds a committed object (409) and otherwise tears the bucket down
    (channel, then row). One instance is registered on the ``DELETE_BUCKET`` slot.
    """

    def __init__(
        self,
        session_factory: AsyncSessionFactory,
        telegram: TelegramPort,
    ) -> None:
        """Capture the storage and Telegram dependencies for every call.

        Args:
            session_factory: Hands out request-scoped async sessions.
            telegram: The boundary used to delete the bucket's channel.
        """
        self._session_factory = session_factory
        self._telegram = telegram

    async def __call__(self, context: S3HandlerContext) -> Response:
        """Delete the bucket, or refuse it when non-empty.

        Returns:
            ``204`` with no body on success (requirement 4.7); ``409`` when the
            bucket holds at least one committed object, mutating nothing
            (requirement 4.3); ``404`` when the bucket does not exist; ``500``
            with the standard error document if the channel deletion fails; a
            ``400`` error document for a malformed target.
        """
        bucket_name = context.target.bucket
        if bucket_name is None:
            return error_response(
                code="InvalidBucketName",
                message="DeleteBucket requires a bucket name",
                status_code=400,
            )

        # Resolve the bucket and whether it holds any committed object in one
        # read; nothing is mutated here (requirement 4.3 "SHALL NOT modify").
        async with self._session_factory.session() as session:
            row = (
                await session.execute(
                    select(Bucket.id, Bucket.channel_id).where(
                        Bucket.name == bucket_name
                    )
                )
            ).first()
            if row is None:
                return error_response(
                    code="NoSuchBucket",
                    message="The specified bucket does not exist.",
                    status_code=404,
                    resource=f"/{bucket_name}",
                )
            bucket_id, channel_id = row
            if await self._has_committed_object(session, bucket_id):
                return self._not_empty(bucket_name)

        # 1. Delete the Telegram channel first. If this fails, leave the index
        #    row untouched and return 500 (requirement 4.7 is all-or-nothing).
        try:
            await self._telegram.delete_channel(channel_id)
        except TelegramError:
            return error_response(
                code="InternalError",
                message="Bucket deletion failed; the bucket was not removed.",
                status_code=500,
                resource=f"/{bucket_name}",
            )

        # 2. The channel is gone; remove the index row. The row is deleted by id
        #    captured above so a racing create of the same name is unaffected.
        async with self._session_factory.session() as session:
            bucket = await session.get(Bucket, bucket_id)
            if bucket is not None:
                await session.delete(bucket)

        return Response(status_code=204)

    @staticmethod
    async def _has_committed_object(
        session: AsyncSession, bucket_id: uuid.UUID
    ) -> bool:
        """Return whether the bucket has any ``COMMITTED`` generation.

        A committed generation is a live object (the only durable, observable
        state); its presence makes the bucket non-empty for delete purposes
        (requirement 4.3). Uses ``EXISTS`` semantics via a bounded count so the
        check stops at the first committed row.
        """
        count = (
            await session.execute(
                select(func.count())
                .select_from(Generation)
                .where(
                    Generation.bucket_id == bucket_id,
                    Generation.state == STATE_COMMITTED,
                )
            )
        ).scalar_one()
        return int(count) > 0

    @staticmethod
    def _not_empty(bucket_name: str) -> Response:
        """Build the ``409 BucketNotEmpty`` document (requirement 4.3)."""
        return error_response(
            code="BucketNotEmpty",
            message=(
                "The bucket you tried to delete is not empty."
            ),
            status_code=409,
            resource=f"/{bucket_name}",
        )


class GetBucketLocationHandler:
    """Serve ``GET /{bucket}?location`` (GetBucketLocation) (requirement 4.5).

    One instance is registered on the ``GET_BUCKET_LOCATION`` slot. Returns the
    configured ``S3_REGION`` as a ``LocationConstraint`` document.
    """

    def __init__(self, session_factory: AsyncSessionFactory) -> None:
        """Store the session factory used to confirm the bucket exists."""
        self._session_factory = session_factory

    async def __call__(self, context: S3HandlerContext) -> Response:
        """Return the configured region as a LocationConstraint (req 4.5).

        Confirms the bucket exists so a request for an unknown bucket yields the
        standard ``NoSuchBucket`` document rather than a region for a bucket that
        is not there.

        Args:
            context: The router-populated handler context carrying the resolved
                target and the settings.

        Returns:
            An ``application/xml`` ``200`` response, or a ``404 NoSuchBucket``
            error document.
        """
        bucket_name = context.target.bucket
        if bucket_name is None:
            return error_response(
                code="InvalidBucketName",
                message="GetBucketLocation requires a bucket name",
                status_code=400,
            )

        async with self._session_factory.session() as session:
            exists = (
                await session.execute(
                    select(Bucket.id).where(Bucket.name == bucket_name)
                )
            ).scalar_one_or_none() is not None

        if not exists:
            return error_response(
                code="NoSuchBucket",
                message="The specified bucket does not exist.",
                status_code=404,
                resource=f"/{bucket_name}",
            )

        body = xml_codecs.serialise_get_bucket_location(
            xml_codecs.GetBucketLocationResponse(
                location_constraint=context.settings.S3_REGION
            )
        )
        return Response(content=body, status_code=200, media_type="application/xml")

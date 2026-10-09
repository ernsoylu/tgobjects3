"""CopyObject — duplicate an object within or between buckets on one gateway.

This module implements the S3 ``CopyObject`` operation (Requirement 11). On the
wire, CopyObject is a ``PUT /{bucket}/{key}`` that carries an
``x-amz-copy-source`` header naming the *source* ``/{src-bucket}/{src-key}``;
the request path names the *destination*. The router
(:mod:`gateway.app.s3.router`) classifies any ``PUT /{bucket}/{key}`` without
multipart query parameters as :data:`~gateway.app.s3.router.S3Operation.PUT_OBJECT`
regardless of the copy header — it does not distinguish a copy from a plain put.

Wiring requirement (task 32)
----------------------------
The application wiring MUST route a ``PUT`` carrying ``x-amz-copy-source`` to
this handler rather than to :class:`~gateway.app.s3.operations.put_object.PutObjectHandler`.
Because both share the ``PUT_OBJECT`` slot, the wiring task is expected to
register a small dispatching handler on that slot that inspects the
``x-amz-copy-source`` header and delegates to :class:`CopyObjectHandler` when it
is present, falling through to the PutObject handler otherwise. This module does
**not** modify ``router.py`` or ``put_object.py``; :meth:`CopyObjectHandler.__call__`
itself re-checks the header and returns an error if it is somehow invoked for a
non-copy request, so the contract is enforced defensively on both sides.

Scope: the dedup copy path (Requirement 11.2)
---------------------------------------------
The authoritative, durable bytes of every object live in Telegram; the
PostgreSQL index is a rebuildable cache (product guarantee). A copy therefore
needs no new *data* upload when the source object's content already exists as a
COMMITTED blob in the destination bucket — the destination generation simply
points at that existing blob and bumps its refcount (Requirement 11.2). Because
dedup is scoped to ``(bucket_id, sha256)`` (see
:func:`gateway.app.storage.blobs.claim_or_wait`), the within-bucket copy — the
common ``rename``/``duplicate`` case, and the one the design's integration test
exercises ("CopyObject within bucket (dedup path verified via upload_jobs
count)") — always hits this path: the source object's blob is, by definition,
already COMMITTED in that same bucket.

For a cross-bucket copy whose content is *not* yet present in the destination
bucket, no COMMITTED blob can be reused and the bytes would have to be
re-uploaded to the destination channel. That full-upload copy path is out of
scope for this task; such a request is rejected with ``501 NotImplemented`` so
the client gets a well-formed, honest error rather than a silent partial copy.
This keeps the handler's guarantee crisp: it either performs a no-upload dedup
copy or declines, and it never uploads object data to Telegram.

Request flow
------------
Given an authenticated ``PUT /{dst-bucket}/{dst-key}`` with
``x-amz-copy-source: /{src-bucket}/{src-key}``::

    parse copy-source -> resolve destination bucket (404 if missing) ->
    resolve source head + generation + blob (404 if missing) ->
    [same bucket & same key] -> no-op: return existing ETag/last-modified ->
    find COMMITTED blob in dst bucket by source sha256 ->
      [hit]  -> write CommitRecord to Telegram -> insert generation ->
                 bump blob refcount -> advance head -> CopyObjectResult
      [miss] -> 501 NotImplemented (full-upload copy out of scope)

The CommitRecord is still written to Telegram on the dedup path (mirroring the
Upload Worker's ``_commit_dedup``): the index entry is only recoverable if a
Commit Record for the new generation exists in the channel. No *data* chunks and
no BlobManifest are written — those already exist for the reused blob — so this
path performs exactly one Telegram message write and zero data uploads.

Requirement mapping
-------------------
* **11.1** — supports copy for source/destination within buckets served by this
  gateway (both resolved through the same index/channels).
* **11.2** — a source SHA-256 matching a COMMITTED blob in the destination
  bucket records the destination generation against that blob with no data
  upload to Telegram.
* **11.3** — a source key that does not exist returns ``404`` and creates no
  destination object.
* **11.4** — on success returns the destination object's ETag and the
  last-modified timestamp of the newly created generation, framed as the S3
  ``CopyObjectResult`` XML body.
* **11.5** — a missing destination bucket returns ``404`` and creates nothing.
* **11.6** — identical source and destination (same bucket, same key) is a
  no-op returning the existing ETag and last-modified with no Telegram write.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import unquote
from xml.etree import ElementTree as ET

from fastapi import Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.app.db.models import Blob, Bucket, Generation
from gateway.app.db.session import AsyncSessionFactory
from gateway.app.format import commit_record as commit_record_codec
from gateway.app.format.types import CommitRecord
from gateway.app.s3 import xml_codecs
from gateway.app.s3.router import S3HandlerContext, error_response
from gateway.app.storage import blobs as blobs_storage
from gateway.app.storage import generations as generations_storage
from gateway.app.storage import heads
from gateway.app.telegram.errors import TelegramError
from gateway.app.telegram.port import TelegramPort

__all__ = ["CopyObjectHandler", "serialise_copy_object_result"]

#: The header naming the copy source, ``/{src-bucket}/{src-key}`` (optionally
#: URL-encoded and optionally suffixed with ``?versionId=…``). Lower-cased
#: because the router snapshots request headers unchanged and callers may send
#: any case.
_HDR_COPY_SOURCE = "x-amz-copy-source"

#: Caption prefix for a Commit Record message, mirroring the design's "Telegram
#: Message Format" table (``tgo3-c-<generation_id>``) and the prefix the recovery
#: CLI classifies on. Kept local to the handler that writes it, matching the
#: Upload Worker's own private constant.
_PREFIX_COMMIT = "tgo3-c-"


@dataclass(frozen=True)
class _CopySource:
    """A parsed ``x-amz-copy-source`` header.

    Attributes:
        bucket: The source bucket name.
        key: The source object key (percent-decoded; may contain ``/``).
    """

    bucket: str
    key: str


@dataclass(frozen=True)
class _SourceObject:
    """The resolved source object a copy reads from.

    Produced by :meth:`CopyObjectHandler._resolve_source`. Carries the content
    digest used to look for a reusable blob in the destination bucket, together
    with the metadata propagated onto the destination generation and echoed in
    the response.

    Attributes:
        generation_id: The source generation the head currently points at.
        blob_id: The blob the source generation references.
        sha256: The 32-byte content digest (the dedup key within a bucket).
        size: The source object's plaintext size in bytes.
        etag: The source object's ETag, propagated to the destination.
        checksums: The source generation's checksum map, propagated along.
        content_type: The source generation's content type, propagated along.
        user_meta: The source generation's user metadata, propagated along.
        last_modified: The source generation's last-modified timestamp; used only
            for the same-bucket same-key no-op response (Requirement 11.6).
    """

    generation_id: uuid.UUID
    blob_id: uuid.UUID
    sha256: bytes
    size: int
    etag: str | None
    checksums: dict[str, str]
    content_type: str | None
    user_meta: dict[str, str]
    last_modified: datetime


class _CopyObjectError(Exception):
    """Internal control-flow signal carrying a ready-to-send error response.

    Raised by the handler's private steps to unwind to
    :meth:`CopyObjectHandler.__call__`, which returns the attached response. This
    keeps the happy path linear while every failure maps to the right S3 error
    document at a single return point.
    """

    def __init__(self, response: Response) -> None:
        self.response = response
        super().__init__(f"CopyObject failed with HTTP {response.status_code}")


class CopyObjectHandler:
    """Handle ``PUT /{bucket}/{key}`` carrying ``x-amz-copy-source`` (Requirement 11).

    Holds the dependencies the router context does not carry — the database
    session factory and the Telegram port — captured at construction time. The
    application wiring (task 32) delegates copy requests on the ``PUT_OBJECT``
    slot to one instance of this handler; see the module docstring for the wiring
    contract.
    """

    def __init__(
        self,
        session_factory: AsyncSessionFactory,
        telegram: TelegramPort,
    ) -> None:
        """Capture the storage and Telegram dependencies for every call.

        Args:
            session_factory: Hands out request-scoped async sessions for index
                reads and the commit transaction.
            telegram: The boundary used to write the destination generation's
                Commit Record (no data is uploaded on the dedup path).
        """
        self._session_factory = session_factory
        self._telegram = telegram

    async def __call__(self, context: S3HandlerContext) -> Response:
        """Execute CopyObject and return its HTTP response.

        SigV4 is already verified by the router (``context.auth``). The
        destination is the request target; the source comes from the
        ``x-amz-copy-source`` header. All failures surface through
        :class:`_CopyObjectError` and are rendered as S3 error documents here.
        """
        dst_bucket = context.target.bucket
        dst_key = context.target.key
        if dst_bucket is None or dst_key is None:
            # The router only dispatches object-level PUTs here; defensive guard.
            return error_response(
                code="InvalidRequest",
                message="CopyObject requires a destination bucket and key",
                status_code=400,
                resource=None,
            )

        try:
            source = self._parse_copy_source(context, dst_bucket, dst_key)
            return await self._handle(context, dst_bucket, dst_key, source)
        except _CopyObjectError as exc:
            return exc.response

    # -- Copy-source parsing --------------------------------------------------

    def _parse_copy_source(
        self, context: S3HandlerContext, dst_bucket: str, dst_key: str
    ) -> _CopySource:
        """Parse the ``x-amz-copy-source`` header into a bucket/key pair.

        The header value is ``/{src-bucket}/{src-key}`` (a leading slash is
        optional in practice) and may be percent-encoded and carry a
        ``?versionId=…`` suffix; versioning is not supported here, so the suffix
        is discarded. A missing or malformed header is a client error.

        Raises:
            _CopyObjectError: HTTP 400 ``InvalidArgument`` when the header is
                absent (this handler must only be invoked for copies) or does not
                name a ``bucket/key`` pair.
        """
        raw = context.request.headers.get(_HDR_COPY_SOURCE)
        if raw is None or not raw.strip():
            raise _CopyObjectError(
                error_response(
                    code="InvalidArgument",
                    message=(
                        "CopyObject requires a non-empty x-amz-copy-source header"
                    ),
                    status_code=400,
                    resource=f"/{dst_bucket}/{dst_key}",
                )
            )

        # Drop an optional ?versionId=… suffix (versioning is unsupported) and a
        # single leading slash, then percent-decode and split on the first "/".
        value = raw.strip()
        value = value.split("?", 1)[0]
        if value.startswith("/"):
            value = value[1:]
        decoded = unquote(value)
        bucket, slash, key = decoded.partition("/")
        if not bucket or not slash or not key:
            raise _CopyObjectError(
                error_response(
                    code="InvalidArgument",
                    message=(
                        "x-amz-copy-source must be of the form "
                        "/{bucket}/{key}"
                    ),
                    status_code=400,
                    resource=f"/{dst_bucket}/{dst_key}",
                )
            )
        return _CopySource(bucket=bucket, key=key)

    # -- Orchestration --------------------------------------------------------

    async def _handle(
        self,
        context: S3HandlerContext,
        dst_bucket: str,
        dst_key: str,
        source: _CopySource,
    ) -> Response:
        """Resolve both sides, handle the no-op, then perform the dedup copy.

        Resolves the destination bucket (404 if missing, Requirement 11.5) and
        the source object (404 if missing, Requirement 11.3) in one read session.
        A same-bucket same-key copy short-circuits as a no-op (Requirement 11.6).
        Otherwise it looks for a reusable COMMITTED blob in the destination bucket
        and, on a hit, records the new generation with no data upload
        (Requirement 11.2).
        """
        async with self._session_factory.session() as session:
            dst = await self._resolve_bucket(session, dst_bucket)
            if dst is None:
                # Requirement 11.5: unknown destination bucket, create nothing.
                raise _CopyObjectError(self._not_found_bucket(dst_bucket, dst_key))

            src_bucket_row = await self._resolve_bucket(session, source.bucket)
            if src_bucket_row is None:
                # No source bucket means no source key: 404 (Requirement 11.3).
                raise _CopyObjectError(
                    self._not_found_key(source.bucket, source.key)
                )

            resolved_source = await self._resolve_source(
                session, src_bucket_row, source.key
            )

        # Requirement 11.6: an identical source and destination is a no-op; echo
        # the existing object's ETag and last-modified with no Telegram write.
        if source.bucket == dst_bucket and source.key == dst_key:
            return self._copy_result_response(
                resolved_source.etag, resolved_source.last_modified
            )

        return await self._dedup_copy(
            dst, dst_key, resolved_source
        )

    async def _dedup_copy(
        self,
        dst_bucket_row: Bucket,
        dst_key: str,
        source: _SourceObject,
    ) -> Response:
        """Record the destination generation against a reused blob, no upload.

        Looks for a COMMITTED blob in the destination bucket whose content digest
        equals the source object's. On a miss, the content is absent from the
        destination bucket and a full re-upload would be required, which is out of
        scope — this returns ``501 NotImplemented`` (see module docstring). On a
        hit it writes the destination generation's Commit Record to Telegram
        (keeping the index recoverable), then in one transaction inserts the
        COMMITTED generation pointing at the reused blob, bumps the blob's
        refcount, and advances the destination key's head (Requirement 11.2).
        """
        new_generation_id = uuid.uuid4()
        now = datetime.now(UTC)

        async with self._session_factory.session() as session:
            dedup_blob = await self._find_committed_blob(
                session, dst_bucket_row.id, source.sha256
            )
            if dedup_blob is None:
                raise _CopyObjectError(self._cross_bucket_unsupported(dst_key))

            generation_number = await self._generation_number(
                session, dst_bucket_row.id, dst_key
            )

            # 1. Write the Commit Record to Telegram FIRST (outside the DB
            #    transaction's visibility window): only a confirmed message id
            #    makes the new generation recoverable. No data chunks or manifest
            #    are sent — the reused blob already owns those.
            commit_message_id = await self._send_commit_record(
                channel_id=dst_bucket_row.channel_id,
                generation_id=new_generation_id,
                bucket_id=dst_bucket_row.id,
                key=dst_key,
                generation_number=generation_number,
                blob_manifest_message_id=dedup_blob.manifest_message_id or 0,
                source=source,
                last_modified=now,
            )

            # 2. The Commit Record is durable. Record the index entry atomically:
            #    insert the COMMITTED generation, bump the reused blob's refcount,
            #    and advance the head so reads resolve to the copy.
            async with session.begin():
                await self._insert_committed_generation(
                    session,
                    generation_id=new_generation_id,
                    bucket_id=dst_bucket_row.id,
                    key=dst_key,
                    blob_id=dedup_blob.id,
                    commit_message_id=commit_message_id,
                    source=source,
                    last_modified=now,
                )
                await blobs_storage.increment_refcount(session, dedup_blob.id)
                await heads.advance_head(
                    session, dst_bucket_row.id, dst_key, new_generation_id
                )

        return self._copy_result_response(source.etag, now)

    # -- Index resolution -----------------------------------------------------

    @staticmethod
    async def _resolve_bucket(
        session: AsyncSession, name: str
    ) -> Bucket | None:
        """Load a bucket row by name, or ``None`` when it does not exist."""
        return (
            await session.execute(select(Bucket).where(Bucket.name == name))
        ).scalar_one_or_none()

    async def _resolve_source(
        self, session: AsyncSession, src_bucket_row: Bucket, src_key: str
    ) -> _SourceObject:
        """Resolve the source key's live, readable generation and its blob.

        Follows the source key's object head to its generation and blob,
        requiring the generation to be COMMITTED with a real blob (a delete
        tombstone or a not-yet-committed write is not a copyable source). The
        blob supplies the content digest used to find a reusable blob in the
        destination bucket.

        Raises:
            _CopyObjectError: HTTP 404 ``NoSuchKey`` when the key has no live
                head, the generation is missing/uncommitted, or its blob is
                missing (Requirement 11.3).
        """
        generation_id = await heads.get_head(session, src_bucket_row.id, src_key)
        if generation_id is None:
            raise _CopyObjectError(
                self._not_found_key(src_bucket_row.name, src_key)
            )

        generation = (
            await session.execute(
                select(Generation).where(Generation.id == generation_id)
            )
        ).scalar_one_or_none()
        if (
            generation is None
            or generation.state != generations_storage.STATE_COMMITTED
            or generation.blob_id is None
        ):
            raise _CopyObjectError(
                self._not_found_key(src_bucket_row.name, src_key)
            )

        blob = (
            await session.execute(
                select(Blob).where(Blob.id == generation.blob_id)
            )
        ).scalar_one_or_none()
        if blob is None:
            raise _CopyObjectError(
                self._not_found_key(src_bucket_row.name, src_key)
            )

        return _SourceObject(
            generation_id=generation.id,
            blob_id=blob.id,
            sha256=blob.sha256,
            size=blob.size,
            etag=generation.etag,
            checksums=_as_str_map(generation.checksums),
            content_type=generation.content_type,
            user_meta=_as_str_map(generation.user_meta),
            last_modified=_as_utc(generation.last_modified),
        )

    @staticmethod
    async def _find_committed_blob(
        session: AsyncSession, bucket_id: uuid.UUID, sha256: bytes
    ) -> Blob | None:
        """Return a COMMITTED blob in ``bucket_id`` matching ``sha256``, or None.

        This is the dedup lookup of Requirement 11.2, scoped to the destination
        bucket to match the ``UNIQUE(bucket_id, sha256)`` invariant: a blob is
        reusable only within its own bucket. A within-bucket copy always finds
        the source object's own blob here; a cross-bucket copy only finds one
        when identical content was previously written to the destination bucket.
        """
        return (
            await session.execute(
                select(Blob)
                .where(
                    Blob.bucket_id == bucket_id,
                    Blob.sha256 == sha256,
                    Blob.state == blobs_storage.BLOB_STATE_COMMITTED,
                )
                .limit(1)
            )
        ).scalar_one_or_none()

    @staticmethod
    async def _generation_number(
        session: AsyncSession, bucket_id: uuid.UUID, key: str
    ) -> int:
        """Compute the monotonic generation number for the next write to a key.

        Counts the COMMITTED generations already recorded for ``(bucket_id,
        key)``; the new generation is numbered after them. Mirrors the Upload
        Worker's ``_generation_number`` so a Commit Record written here orders
        consistently with ones written by the normal write path.
        """
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

    # -- Durable commit -------------------------------------------------------

    async def _send_commit_record(
        self,
        *,
        channel_id: int,
        generation_id: uuid.UUID,
        bucket_id: uuid.UUID,
        key: str,
        generation_number: int,
        blob_manifest_message_id: int,
        source: _SourceObject,
        last_modified: datetime,
    ) -> int:
        """Serialise and send the destination generation's Commit Record.

        The Commit Record references the reused blob's manifest message so a
        recovery scan can reconstruct the destination generation without any new
        data or manifest message. A Telegram failure leaves the index untouched
        and surfaces as HTTP 503 (nothing has been written to PostgreSQL yet).

        Returns:
            The Telegram message id of the confirmed Commit Record.

        Raises:
            _CopyObjectError: HTTP 503 ``ServiceUnavailable`` if the Telegram
                write fails; no destination object is created.
        """
        record = CommitRecord(
            format_version=1,
            generation_id=generation_id,
            bucket_id=bucket_id,
            key=key,
            generation_number=generation_number,
            blob_manifest_message_id=blob_manifest_message_id,
            etag=source.etag or "",
            checksums=source.checksums,
            content_type=source.content_type or "application/octet-stream",
            user_meta=source.user_meta,
            last_modified=last_modified,
        )
        try:
            return await self._telegram.send_document(
                channel_id,
                commit_record_codec.serialise(record),
                file_name=f"{generation_id.hex}.commit",
                caption=f"{_PREFIX_COMMIT}{generation_id.hex}",
            )
        except TelegramError as exc:
            raise _CopyObjectError(
                error_response(
                    code="ServiceUnavailable",
                    message=(
                        "Telegram was unreachable; the object was not copied"
                    ),
                    status_code=503,
                    resource=f"/{record.key}",
                )
            ) from exc

    @staticmethod
    async def _insert_committed_generation(
        session: AsyncSession,
        *,
        generation_id: uuid.UUID,
        bucket_id: uuid.UUID,
        key: str,
        blob_id: uuid.UUID,
        commit_message_id: int,
        source: _SourceObject,
        last_modified: datetime,
    ) -> None:
        """Insert the destination generation already in the COMMITTED state.

        Unlike a normal PutObject, a dedup copy has nothing to upload, so the
        generation is born COMMITTED referencing the reused blob — there is no
        STAGED phase and no upload job (which is how the design's integration
        test distinguishes a dedup copy: the ``upload_jobs`` count does not
        change). The row is added and flushed within the caller's transaction so
        the subsequent head advance can reference it.
        """
        generation = Generation(
            id=generation_id,
            bucket_id=bucket_id,
            key=key,
            kind="copy",
            blob_id=blob_id,
            size=source.size,
            etag=source.etag,
            checksums=dict(source.checksums) or None,
            content_type=source.content_type,
            user_meta=dict(source.user_meta) or None,
            last_modified=last_modified,
            state=generations_storage.STATE_COMMITTED,
            commit_message_id=commit_message_id,
            staging_path=None,
        )
        session.add(generation)
        await session.flush()

    # -- Response / error builders -------------------------------------------

    def _copy_result_response(
        self, etag: str | None, last_modified: datetime
    ) -> Response:
        """Build the ``200 CopyObjectResult`` body (Requirements 11.4, 11.6)."""
        body = serialise_copy_object_result(etag or "", last_modified)
        return Response(
            content=body,
            status_code=200,
            media_type="application/xml",
        )

    @staticmethod
    def _not_found_bucket(bucket: str, key: str) -> Response:
        """Return the S3 ``NoSuchBucket`` / HTTP 404 document (Requirement 11.5)."""
        return error_response(
            code="NoSuchBucket",
            message="The specified bucket does not exist.",
            status_code=404,
            resource=f"/{bucket}/{key}",
        )

    @staticmethod
    def _not_found_key(bucket: str, key: str) -> Response:
        """Return the S3 ``NoSuchKey`` / HTTP 404 document (Requirement 11.3)."""
        return error_response(
            code="NoSuchKey",
            message="The specified key does not exist.",
            status_code=404,
            resource=f"/{bucket}/{key}",
        )

    @staticmethod
    def _cross_bucket_unsupported(key: str) -> Response:
        """Return ``501 NotImplemented`` for a copy that would need a re-upload.

        The content is not present as a COMMITTED blob in the destination bucket,
        so completing the copy would require uploading data to Telegram — out of
        scope for this handler (see module docstring). The honest, well-formed
        response is NotImplemented rather than a silent partial copy.
        """
        return error_response(
            code="NotImplemented",
            message=(
                "cross-bucket copy of content not already present in the "
                "destination bucket is not supported"
            ),
            status_code=501,
            resource=f"/{key}",
        )


# ---------------------------------------------------------------------------
# CopyObjectResult XML
#
# The S3 ``CopyObjectResult`` shape is not covered by ``xml_codecs.py`` (it has
# no request/response dataclass there), so it is built here with ``xml.etree`` in
# the same S3 namespace and millisecond-ISO timestamp form the shared codecs use,
# rather than by string concatenation.
# ---------------------------------------------------------------------------


def serialise_copy_object_result(etag: str, last_modified: datetime) -> bytes:
    """Serialise a ``CopyObjectResult`` body (Requirement 11.4).

    Emits ``<CopyObjectResult><ETag/><LastModified/></CopyObjectResult>`` in the
    S3 2006-03-01 namespace. The ETag is wrapped in double quotes if it is not
    already, matching S3's quoted-ETag convention; the timestamp uses the same
    millisecond-precision ISO 8601 form as the other S3 bodies.

    Args:
        etag: The destination object's ETag (with or without surrounding quotes).
        last_modified: The newly created generation's last-modified time.

    Returns:
        The UTF-8, XML-declared ``CopyObjectResult`` document.
    """
    root = ET.Element(f"{{{xml_codecs.S3_NAMESPACE}}}CopyObjectResult")
    etag_el = ET.SubElement(root, f"{{{xml_codecs.S3_NAMESPACE}}}ETag")
    etag_el.text = _quote_etag(etag)
    modified_el = ET.SubElement(root, f"{{{xml_codecs.S3_NAMESPACE}}}LastModified")
    modified_el.text = _iso(last_modified)
    out = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    return bytes(out)


def _quote_etag(etag: str) -> str:
    """Return ``etag`` wrapped in double quotes, idempotently."""
    if etag.startswith('"') and etag.endswith('"'):
        return etag
    return f'"{etag}"'


def _iso(value: datetime) -> str:
    """Render a timestamp in the millisecond-precision ISO 8601 form S3 uses."""
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


def _as_utc(value: datetime) -> datetime:
    """Return ``value`` as a timezone-aware UTC datetime.

    Generations persist ``last_modified`` without a stored timezone; treat a
    naive value as UTC so the response timestamp is unambiguous.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _as_str_map(value: object) -> dict[str, str]:
    """Coerce a JSONB map column to ``dict[str, str]`` for the format layer.

    ``user_meta`` and ``checksums`` are stored as JSONB and may be ``None``; the
    Commit Record requires string-to-string maps, so non-string values are
    stringified and ``None`` becomes an empty map. Mirrors the Upload Worker's
    own coercion so a copied generation's Commit Record matches the original's.
    """
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items()}

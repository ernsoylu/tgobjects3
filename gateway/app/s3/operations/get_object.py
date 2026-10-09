"""GetObject and ranged reads (Requirement 8).

This module implements the S3 ``GetObject`` handler, including HTTP range
requests and conditional requests. It plugs into the router as the
:data:`gateway.app.s3.router.S3Operation.GET_OBJECT` handler.

Read path (design "GET (ranged read, partial cache hit)")
---------------------------------------------------------
Given an authenticated request for ``/{bucket}/{key}``::

    resolve head -> load generation/blob/bucket/chunk index ->
    validate Range -> map range to the minimal record set ->
    for each record: cache lookup -> [miss] fetch chunk from Telegram
    (with READ_AHEAD) -> verify AES-256-GCM tag -> stream bytes

The authoritative bytes live in Telegram; the PostgreSQL index tells us *where*
(which bucket channel, which chunk documents, how records pack into them) and
the record cache (``gateway/app/cache/store.py``) accelerates repeat reads. Every
record's plaintext is produced by :func:`gateway.app.format.record_codec.unseal`,
which authenticates the AES-256-GCM tag before returning any bytes — so a record
is verified *before* its bytes reach the response stream (requirement 8.5).

Integrity failure semantics (requirement 8.6)
---------------------------------------------
A tag failure must never surface corrupt bytes to the client. We therefore fetch
and verify the **first** record of the response eagerly, before the streaming
response status line is sent: if that first record fails authentication, no bytes
have been flushed and we return ``500`` with an S3 error document (design
"Request Errors": AES-GCM tag verification failure → 500, connection closed, no
bytes flushed). Once streaming has begun and later records fail, the status line
is already on the wire, so we abort the response body mid-stream (raise out of the
generator) rather than complete it, closing the connection without a clean end.

Range and conditional requests (requirements 8.2, 8.3, 8.7–8.10)
----------------------------------------------------------------
A ``Range: bytes=…`` header maps to the minimal covering record set via
:func:`gateway.app.format.range_mapper.records_for_range` and yields ``206
Partial Content`` with a matching ``Content-Range``; a syntactically invalid
range, or one starting beyond the object, yields ``416`` with a
``Content-Range: bytes */<size>`` header and no body. The conditional headers
``If-Match``/``If-Unmodified-Since`` (precondition → ``412`` on failure) and
``If-None-Match``/``If-Modified-Since`` (``304`` on failure) are evaluated against
the object's ETag and last-modified time before any body is produced.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import format_datetime, parsedate_to_datetime

from fastapi import Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.app.cache.store import CacheFetchError, RecordCache
from gateway.app.config import Settings
from gateway.app.db.models import Blob, Bucket, Chunk, Generation
from gateway.app.db.session import AsyncSessionFactory
from gateway.app.format import record_codec
from gateway.app.format.key_wrap import KeyUnwrapError, derive_root_key, unwrap
from gateway.app.format.range_mapper import records_for_range
from gateway.app.format.record_codec import RecordAuthError
from gateway.app.s3 import router as s3_router
from gateway.app.storage import generations as generations_storage
from gateway.app.storage.heads import get_head
from gateway.app.telegram.errors import TelegramError
from gateway.app.telegram.port import TelegramPort

__all__ = [
    "GetObjectHandler",
    "ObjectIntegrityError",
    "ObjectNotFoundError",
    "create_get_object_handler",
]

# On-wire layout constants for a sealed record inside a chunk document. A chunk
# document is the concatenation of sealed records (see the Upload Worker's
# ``_group_records``); each sealed record is ``nonce(12) || ciphertext || tag``.
# Every record except the final record of a blob carries a full MAX_PLAINTEXT
# payload, so a full sealed record occupies a fixed stride and the first record
# of any chunk begins at a whole multiple of that stride.
_SEALED_FULL_BLOCK = record_codec.NONCE_SIZE + record_codec.CIPHERTEXT_BLOCK_SIZE


class ObjectNotFoundError(Exception):
    """The requested key has no readable COMMITTED generation.

    Raised internally when head resolution finds no head, the generation is not
    COMMITTED, or the generation is a delete tombstone. The handler renders it as
    an S3 ``NoSuchKey`` / HTTP 404 (design "Request Errors": object not found).
    """


class ObjectIntegrityError(Exception):
    """A record failed AES-256-GCM authentication before any bytes were flushed.

    Raised while preparing the response body (verifying the first record) so the
    handler can return HTTP 500 with no body on the wire (requirement 8.6; design
    "Request Errors": AES-GCM tag verification failure → 500, no bytes flushed).
    Once streaming has begun this is *not* used — a later failure aborts the body
    stream instead.
    """


@dataclass(frozen=True)
class _ObjectMeta:
    """Resolved index metadata for one object generation.

    A frozen value object assembled from the ``generations``, ``blobs``,
    ``buckets`` and ``chunks`` rows, carrying everything the read needs without
    re-querying: the identity used for cache keys and AAD, the response headers,
    and the record→chunk-document map.

    Attributes:
        generation_id: The committed generation being read.
        blob_id: The blob holding the object's encrypted records.
        channel_id: The Telegram channel backing the bucket.
        size: The object's plaintext size in bytes.
        etag: The object ETag for conditional checks and the response header.
        content_type: The stored content type, or a default when absent.
        last_modified: The generation's last-modified timestamp (UTC).
        blob_key: The unwrapped 32-byte AES key for the blob's records.
        chunk_records: Records packed into each chunk document.
        total_records: Total number of records in the blob.
        chunk_message_ids: Telegram message id per chunk index, in blob order.
    """

    generation_id: uuid.UUID
    blob_id: uuid.UUID
    channel_id: int
    size: int
    etag: str | None
    content_type: str
    last_modified: datetime
    blob_key: bytes
    chunk_records: int
    total_records: int
    chunk_message_ids: list[int]


@dataclass(frozen=True)
class _ByteRange:
    """A resolved, satisfiable byte range within an object.

    Both bounds are inclusive. For a full-object read ``start`` is ``0`` and
    ``end`` is ``size - 1``; ``is_partial`` distinguishes a response that must be
    ``206`` with a ``Content-Range`` from a plain ``200``.
    """

    start: int
    end: int
    is_partial: bool

    @property
    def length(self) -> int:
        """Number of bytes the range covers (inclusive on both ends)."""
        return self.end - self.start + 1


class _InvalidRangeError(Exception):
    """The ``Range`` header is unsatisfiable (malformed or beyond the object).

    Rendered as HTTP 416 with ``Content-Range: bytes */<size>`` and no body
    (requirement 8.8).
    """


class _PreconditionFailedError(Exception):
    """An ``If-Match`` / ``If-Unmodified-Since`` precondition failed (→ 412)."""


class _NotModifiedError(Exception):
    """An ``If-None-Match`` / ``If-Modified-Since`` condition failed (→ 304)."""


class GetObjectHandler:
    """Serves ``GetObject`` requests, including ranged and conditional reads.

    One instance is constructed per gateway process and registered on the
    router's :class:`gateway.app.s3.router.S3Handlers` for the
    ``GET_OBJECT`` slot. It owns the collaborators the read path needs — the
    database session factory (index reads), the :class:`TelegramPort` (record
    downloads) and the :class:`RecordCache` (acceleration) — and derives the root
    key once from configuration to unwrap per-blob keys.
    """

    def __init__(
        self,
        session_factory: AsyncSessionFactory,
        telegram: TelegramPort,
        cache: RecordCache,
        settings: Settings,
    ) -> None:
        """Build the handler with its index/transport/cache collaborators.

        Args:
            session_factory: Opens short-lived sessions for index reads.
            telegram: The Telegram boundary used to download chunk documents.
            cache: The record cache; coordinates miss dedup and READ_AHEAD.
            settings: Gateway configuration; supplies ``TGOS3_MASTER_KEY`` (for
                root-key derivation) and ``READ_AHEAD``.
        """
        self._session_factory = session_factory
        self._telegram = telegram
        self._cache = cache
        self._settings = settings
        self._root_key = derive_root_key(settings.TGOS3_MASTER_KEY)

    async def __call__(self, context: s3_router.S3HandlerContext) -> Response:
        """Handle one GetObject request and return its HTTP response.

        Resolves the object, evaluates conditional headers, validates any range,
        and either returns an error document (404/416/412/304/500) or a streaming
        body (200 full / 206 partial). See the module docstring for the integrity
        and ordering guarantees.
        """
        target = context.target
        if target.bucket is None or target.key is None:
            # The router only dispatches object-level requests here, but guard so
            # a misrouted request becomes a clean error rather than an exception.
            return s3_router.error_response(
                code="InvalidRequest",
                message="GetObject requires a bucket and key",
                status_code=400,
                resource=None,
            )

        try:
            meta = await self._resolve_object(target.bucket, target.key)
        except ObjectNotFoundError:
            return self._not_found(target)

        try:
            self._check_conditional_headers(context.request, meta)
        except _NotModifiedError:
            return self._conditional_response(304, meta)
        except _PreconditionFailedError:
            return self._conditional_response(412, meta)

        try:
            byte_range = self._resolve_range(context.request, meta.size)
        except _InvalidRangeError:
            return self._range_not_satisfiable(target, meta.size)

        return await self._build_body_response(meta, byte_range)

    # -- Index resolution -------------------------------------------------

    async def _resolve_object(self, bucket_name: str, key: str) -> _ObjectMeta:
        """Load and assemble the metadata needed to read ``bucket_name/key``.

        Reads the bucket, head pointer, generation, blob and chunk rows in one
        session, unwraps the blob key through the bucket KEK, and returns a frozen
        :class:`_ObjectMeta`. All reads share one transaction so the view is
        consistent.

        Raises:
            ObjectNotFoundError: If the bucket, head, generation or blob is
                missing, the generation is not COMMITTED, or it is a delete.
        """
        async with self._session_factory.session() as session:
            bucket = await self._load_bucket(session, bucket_name)
            if bucket is None:
                raise ObjectNotFoundError(bucket_name)

            generation_id = await get_head(session, bucket.id, key)
            if generation_id is None:
                raise ObjectNotFoundError(key)

            generation = await self._load_generation(session, generation_id)
            if generation is None:
                raise ObjectNotFoundError(key)
            if generation.state != generations_storage.STATE_COMMITTED:
                raise ObjectNotFoundError(key)
            if generation.blob_id is None:
                # A committed generation with no blob is a delete tombstone's
                # successor state or a malformed row; neither is readable.
                raise ObjectNotFoundError(key)

            blob = await self._load_blob(session, generation.blob_id)
            if blob is None:
                raise ObjectNotFoundError(key)

            chunk_message_ids = await self._load_chunk_message_ids(session, blob.id)

            blob_key = self._unwrap_blob_key(bucket.kek_wrapped, blob.key_wrapped)
            total_records = _record_count(blob.size)

            return _ObjectMeta(
                generation_id=generation.id,
                blob_id=blob.id,
                channel_id=bucket.channel_id,
                size=blob.size,
                etag=generation.etag,
                content_type=generation.content_type or "application/octet-stream",
                last_modified=_as_utc(generation.last_modified),
                blob_key=blob_key,
                chunk_records=blob.chunk_records,
                total_records=total_records,
                chunk_message_ids=chunk_message_ids,
            )

    @staticmethod
    async def _load_bucket(session: AsyncSession, name: str) -> Bucket | None:
        """Load a bucket row by name, or ``None`` when it does not exist."""
        return (
            await session.execute(select(Bucket).where(Bucket.name == name))
        ).scalar_one_or_none()

    @staticmethod
    async def _load_generation(
        session: AsyncSession, generation_id: uuid.UUID
    ) -> Generation | None:
        """Load a generation row by id, or ``None`` when it does not exist."""
        return (
            await session.execute(
                select(Generation).where(Generation.id == generation_id)
            )
        ).scalar_one_or_none()

    @staticmethod
    async def _load_blob(session: AsyncSession, blob_id: uuid.UUID) -> Blob | None:
        """Load a blob row by id, or ``None`` when it does not exist."""
        return (
            await session.execute(select(Blob).where(Blob.id == blob_id))
        ).scalar_one_or_none()

    @staticmethod
    async def _load_chunk_message_ids(
        session: AsyncSession, blob_id: uuid.UUID
    ) -> list[int]:
        """Return the Telegram message id per chunk index, in blob order.

        The chunks table holds one row per uploaded data document; ordering by
        ``idx`` yields the message ids in the same order the Upload Worker packed
        records into documents, so chunk ``c`` is ``result[c]``.
        """
        rows = (
            await session.execute(
                select(Chunk.message_id)
                .where(Chunk.blob_id == blob_id)
                .order_by(Chunk.idx)
            )
        ).scalars()
        return list(rows)

    def _unwrap_blob_key(self, kek_wrapped: bytes, blob_key_wrapped: bytes) -> bytes:
        """Unwrap the per-blob AES key through the bucket KEK and root key.

        The root key (derived from ``TGOS3_MASTER_KEY``) unwraps the bucket KEK,
        which in turn unwraps the per-blob key — mirroring the key hierarchy the
        Upload Worker wraps with (``key_wrap``).

        Raises:
            ObjectNotFoundError: If either unwrap fails. A key that cannot be
                unwrapped makes the object unreadable; surfacing it as "not
                found" avoids leaking key-state detail to the client.
        """
        try:
            bucket_kek = unwrap(self._root_key, kek_wrapped)
            return unwrap(bucket_kek, blob_key_wrapped)
        except KeyUnwrapError as exc:
            raise ObjectNotFoundError("object key material is unreadable") from exc

    # -- Conditional headers (requirements 8.7, 8.9, 8.10) ----------------

    def _check_conditional_headers(
        self, request: Request, meta: _ObjectMeta
    ) -> None:
        """Evaluate the four conditional headers against the object.

        Precondition headers (``If-Match``, ``If-Unmodified-Since``) are checked
        first; a failure raises :class:`_PreconditionFailedError` (→ 412). The
        cache-validation headers (``If-None-Match``, ``If-Modified-Since``) raise
        :class:`_NotModifiedError` (→ 304) when they indicate the client's copy is
        current. ``If-Match`` takes precedence over ``If-None-Match`` and
        ``If-Unmodified-Since`` over ``If-Modified-Since`` when both are present,
        matching HTTP conditional-request precedence.
        """
        headers = request.headers
        if_match = headers.get("if-match")
        if_none_match = headers.get("if-none-match")
        if_modified_since = headers.get("if-modified-since")
        if_unmodified_since = headers.get("if-unmodified-since")

        if if_match is not None:
            if not _etag_matches(if_match, meta.etag):
                raise _PreconditionFailedError
        elif if_unmodified_since is not None:
            since = _parse_http_date(if_unmodified_since)
            if since is not None and meta.last_modified > since:
                raise _PreconditionFailedError

        if if_none_match is not None:
            if _etag_matches(if_none_match, meta.etag):
                raise _NotModifiedError
        elif if_modified_since is not None:
            since = _parse_http_date(if_modified_since)
            if since is not None and meta.last_modified <= since:
                raise _NotModifiedError

    # -- Range parsing (requirements 8.1, 8.2, 8.8) -----------------------

    def _resolve_range(self, request: Request, size: int) -> _ByteRange:
        """Resolve the effective byte range for the request.

        With no ``Range`` header the full object ``[0, size-1]`` is returned as a
        non-partial range (requirement 8.1). A present header is parsed to an
        inclusive, clamped ``[start, end]``; an unsatisfiable header raises
        :class:`_InvalidRangeError` (→ 416, requirement 8.8).

        Only a single ``bytes=`` range is supported; the two S3 forms are
        ``bytes=start-end``, ``bytes=start-`` (to end of object) and
        ``bytes=-suffix`` (final ``suffix`` bytes).
        """
        header = request.headers.get("range")
        if header is None:
            # A zero-length object yields a valid full read of 0 bytes: end is
            # ``size - 1 == -1`` so :attr:`_ByteRange.length` computes to 0.
            return _ByteRange(start=0, end=size - 1, is_partial=False)
        return _parse_range_header(header, size)

    # -- Response assembly ------------------------------------------------

    async def _build_body_response(
        self, meta: _ObjectMeta, byte_range: _ByteRange
    ) -> Response:
        """Build the streaming 200/206 response, verifying the first record.

        The first record of the range is fetched and authenticated eagerly: if it
        fails authentication no bytes have been written, so a clean 500 is
        returned (requirement 8.6). On success a :class:`StreamingResponse` is
        returned whose generator yields the already-verified first record and then
        fetches, verifies and yields the remaining records in order. A verification
        failure past the first record aborts the stream mid-body (connection
        closed without completion) rather than emitting corrupt bytes.
        """
        if byte_range.length == 0:
            # Empty body (zero-length object, or an empty satisfiable range).
            return self._empty_body_response(meta, byte_range)

        record_indices = records_for_range(
            byte_range.start, byte_range.end, record_codec.MAX_PLAINTEXT
        )
        read_indices = self._with_read_ahead(record_indices, meta.total_records)

        try:
            first_plaintext = await self._fetch_record(meta, record_indices[0])
        except ObjectIntegrityError:
            return self._integrity_error()
        except TelegramError:
            return self._telegram_unavailable()

        stream = self._stream_body(
            meta, byte_range, record_indices, read_indices, first_plaintext
        )
        status = 206 if byte_range.is_partial else 200
        headers = self._body_headers(meta, byte_range)
        return StreamingResponse(
            stream,
            status_code=status,
            media_type=meta.content_type,
            headers=headers,
        )

    def _with_read_ahead(
        self, record_indices: list[int], total_records: int
    ) -> list[int]:
        """Extend the required record set with up to READ_AHEAD trailing records.

        Pre-fetching sequential records beyond the requested set warms the cache
        for the common forward-scan access pattern (requirement 8.4). The extra
        records are bounded by ``READ_AHEAD`` (0–32) and never run past the last
        record of the blob.
        """
        read_ahead = self._settings.READ_AHEAD
        if read_ahead <= 0 or not record_indices:
            return list(record_indices)
        last = record_indices[-1]
        extra_end = min(last + read_ahead, total_records - 1)
        return list(range(record_indices[0], extra_end + 1))

    async def _stream_body(
        self,
        meta: _ObjectMeta,
        byte_range: _ByteRange,
        record_indices: list[int],
        read_indices: list[int],
        first_plaintext: bytes,
    ) -> AsyncIterator[bytes]:
        """Yield the requested bytes, record by record, trimmed to the range.

        The first record was verified by the caller and is passed in; it is sliced
        to the range and yielded first. Remaining *required* records are fetched
        and verified in order and sliced to the range. Any READ_AHEAD-only records
        (present in ``read_indices`` but not ``record_indices``) are fetched to
        warm the cache but not streamed. A verification failure after streaming has
        begun raises, aborting the response body without a clean completion
        (requirement 8.6).
        """
        record_size = record_codec.MAX_PLAINTEXT

        # The first (already-verified) record.
        yield _slice_record(
            first_plaintext, record_indices[0], record_size, byte_range
        )

        for record_index in record_indices[1:]:
            plaintext = await self._fetch_record(meta, record_index)
            yield _slice_record(plaintext, record_index, record_size, byte_range)

        # READ_AHEAD-only records: fetch (warming the cache) but do not stream.
        for record_index in read_indices[len(record_indices):]:
            try:
                await self._fetch_record(meta, record_index)
            except (ObjectIntegrityError, TelegramError):
                # Read-ahead is best-effort; a failure must not break a response
                # whose required bytes have already been served.
                return

    async def _fetch_record(self, meta: _ObjectMeta, record_index: int) -> bytes:
        """Return one record's authenticated plaintext via the cache.

        Delegates to :meth:`RecordCache.fetch`, which returns a cached plaintext
        directly or, on a miss, invokes the loader exactly once (deduplicating
        concurrent misses) and records an ``access_stats`` access for the record
        served (requirement 8.11). The loader downloads the record's chunk
        document from Telegram, slices out the record's sealed block, and unseals
        it — the AES-256-GCM tag is authenticated inside ``unseal`` before any
        plaintext is produced (requirement 8.5).

        Raises:
            ObjectIntegrityError: If the record failed authentication.
            TelegramError: If the chunk download failed at the Telegram boundary.
        """
        blob_id_hex = meta.blob_id.hex

        async def loader() -> bytes:
            return await self._load_record_from_telegram(meta, record_index)

        try:
            return await self._cache.fetch(
                blob_id_hex, record_index, meta.size, loader
            )
        except CacheFetchError as exc:
            cause = exc.__cause__
            if isinstance(cause, RecordAuthError):
                raise ObjectIntegrityError(str(cause)) from exc
            if isinstance(cause, TelegramError):
                raise cause from exc
            # Any other upstream failure is surfaced as a Telegram-boundary
            # unavailability so the handler returns a 5xx rather than crash.
            raise TelegramError(str(exc)) from exc

    async def _load_record_from_telegram(
        self, meta: _ObjectMeta, record_index: int
    ) -> bytes:
        """Download, slice and unseal a single record (the cache miss loader).

        Locates the record's chunk document via the record→chunk mapping,
        downloads that document, slices out the record's sealed block at its
        on-wire offset, and unseals it. ``unseal`` verifies the AES-256-GCM tag
        and raises :class:`RecordAuthError` on a mismatch, so corrupt bytes are
        never returned (requirements 8.4, 8.5).
        """
        chunk_index = record_index // meta.chunk_records
        if chunk_index >= len(meta.chunk_message_ids):
            raise RecordAuthError(
                f"record {record_index} maps to missing chunk {chunk_index}"
            )
        message_id = meta.chunk_message_ids[chunk_index]
        document = await self._telegram.download_document(meta.channel_id, message_id)

        sealed = _extract_sealed_record(
            document, record_index, meta.chunk_records
        )
        is_last = record_index == meta.total_records - 1
        return record_codec.unseal(
            sealed,
            meta.blob_key,
            meta.blob_id.bytes,
            record_index=record_index,
            total_plaintext=meta.size,
            is_last=is_last,
        )

    # -- Header / error builders ------------------------------------------

    def _body_headers(
        self, meta: _ObjectMeta, byte_range: _ByteRange
    ) -> dict[str, str]:
        """Build the common response headers for a 200/206 body.

        Always sets ``Content-Length``, ``Last-Modified``, ``ETag`` and
        ``Accept-Ranges``; adds ``Content-Range`` for a partial (206) response so
        it matches the bytes actually served (requirement 8.3).
        """
        headers: dict[str, str] = {
            "Content-Length": str(byte_range.length),
            "Last-Modified": format_datetime(meta.last_modified, usegmt=True),
            "Accept-Ranges": "bytes",
        }
        if meta.etag is not None:
            headers["ETag"] = _quote_etag(meta.etag)
        if byte_range.is_partial:
            headers["Content-Range"] = (
                f"bytes {byte_range.start}-{byte_range.end}/{meta.size}"
            )
        return headers

    def _empty_body_response(
        self, meta: _ObjectMeta, byte_range: _ByteRange
    ) -> Response:
        """Return a 200/206 response with no body (zero-length read)."""
        status = 206 if byte_range.is_partial else 200
        headers = self._body_headers(meta, byte_range)
        return Response(
            content=b"",
            status_code=status,
            media_type=meta.content_type,
            headers=headers,
        )

    def _conditional_response(self, status: int, meta: _ObjectMeta) -> Response:
        """Return a bodyless 304/412 response carrying the validators.

        A 304 echoes the ``ETag`` and ``Last-Modified`` so the client can refresh
        its cache metadata; a 412 is a bare precondition failure. Neither
        transmits the object body (requirements 8.9, 8.10).
        """
        headers: dict[str, str] = {
            "Last-Modified": format_datetime(meta.last_modified, usegmt=True),
        }
        if meta.etag is not None:
            headers["ETag"] = _quote_etag(meta.etag)
        return Response(status_code=status, headers=headers)

    def _range_not_satisfiable(
        self, target: s3_router.ResolvedTarget, size: int
    ) -> Response:
        """Return HTTP 416 with ``Content-Range`` naming the actual size.

        No object body is transmitted (requirement 8.8; design "Request Errors":
        invalid Range → 416 with Content-Range of the actual size).
        """
        body = _error_body("InvalidRange", "The requested range is not satisfiable",
                            target)
        return Response(
            content=body,
            status_code=416,
            media_type="application/xml",
            headers={"Content-Range": f"bytes */{size}"},
        )

    def _not_found(self, target: s3_router.ResolvedTarget) -> Response:
        """Return the S3 ``NoSuchKey`` / HTTP 404 error document."""
        return s3_router.error_response(
            code="NoSuchKey",
            message="The specified key does not exist.",
            status_code=404,
            resource=_resource(target),
        )

    def _integrity_error(self) -> Response:
        """Return HTTP 500 for a pre-stream integrity failure (requirement 8.6)."""
        return s3_router.error_response(
            code="InternalError",
            message="Object integrity verification failed.",
            status_code=500,
            resource=None,
        )

    def _telegram_unavailable(self) -> Response:
        """Return HTTP 503 when the backing store cannot serve the records."""
        return s3_router.error_response(
            code="ServiceUnavailable",
            message="The backing store is temporarily unavailable.",
            status_code=503,
            resource=None,
        )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _record_count(size: int) -> int:
    """Return the number of records a blob of ``size`` plaintext bytes holds.

    A zero-length blob still has exactly one (empty) record, matching the Upload
    Worker's ``_split_records`` which emits one empty record for an empty body.
    """
    if size <= 0:
        return 1
    record_size = record_codec.MAX_PLAINTEXT
    return (size + record_size - 1) // record_size


def _extract_sealed_record(
    document: bytes, record_index: int, chunk_records: int
) -> bytes:
    """Slice the sealed block for ``record_index`` out of its chunk document.

    A chunk document is the concatenation of sealed records, each
    ``nonce(12) || ciphertext || tag``. Every record except the final record of a
    blob is full-size, so the record at position ``j`` within its chunk begins at
    ``j * _SEALED_FULL_BLOCK``. The final record of the blob is the last record of
    its chunk, so its block runs to the end of the document — taking the slice to
    the document end handles both the full and the short-final cases.
    """
    position_in_chunk = record_index % chunk_records
    offset = position_in_chunk * _SEALED_FULL_BLOCK
    end = offset + _SEALED_FULL_BLOCK
    if offset >= len(document):
        raise RecordAuthError(
            f"record {record_index} offset {offset} is beyond chunk document"
        )
    return document[offset:end]


def _slice_record(
    plaintext: bytes, record_index: int, record_size: int, byte_range: _ByteRange
) -> bytes:
    """Return the portion of a record's plaintext that falls inside the range.

    A record spans blob bytes ``[record_index * R, record_index * R + len)``. The
    returned slice is the intersection of that span with the inclusive
    ``[byte_range.start, byte_range.end]`` request range, re-based to the record's
    own bytes. Interior records are returned whole; only the first and last
    records of a range are trimmed.
    """
    record_start = record_index * record_size
    record_end = record_start + len(plaintext) - 1  # inclusive

    lo = max(record_start, byte_range.start)
    hi = min(record_end, byte_range.end)
    if lo > hi:
        return b""
    return plaintext[lo - record_start : hi - record_start + 1]


def _parse_range_header(header: str, size: int) -> _ByteRange:
    """Parse a single-range ``Range: bytes=…`` header to a :class:`_ByteRange`.

    Supports the three S3-honoured forms — ``bytes=start-end``, ``bytes=start-``
    and ``bytes=-suffix`` — for a single range only. The result is clamped to the
    object so ``end`` never exceeds ``size - 1``.

    Raises:
        _InvalidRangeError: If the header is syntactically invalid, names more
            than one range, or begins at or beyond the end of the object
            (requirement 8.8).
    """
    prefix = "bytes="
    if not header.startswith(prefix):
        raise _InvalidRangeError
    spec = header[len(prefix):].strip()
    if "," in spec:
        # Multiple ranges are not supported; treat as unsatisfiable.
        raise _InvalidRangeError
    if "-" not in spec:
        raise _InvalidRangeError

    start_str, _, end_str = spec.partition("-")
    start_str = start_str.strip()
    end_str = end_str.strip()

    if start_str == "":
        # Suffix form: the final ``suffix`` bytes.
        if end_str == "":
            raise _InvalidRangeError
        suffix = _parse_int(end_str)
        if suffix <= 0:
            raise _InvalidRangeError
        if size == 0:
            raise _InvalidRangeError
        start = max(size - suffix, 0)
        end = size - 1
        return _ByteRange(start=start, end=end, is_partial=True)

    start = _parse_int(start_str)
    if start >= size:
        # A range beginning at or beyond the last byte is unsatisfiable.
        raise _InvalidRangeError

    if end_str == "":
        end = size - 1
    else:
        end = _parse_int(end_str)
        if end < start:
            raise _InvalidRangeError
        end = min(end, size - 1)

    return _ByteRange(start=start, end=end, is_partial=True)


def _parse_int(value: str) -> int:
    """Parse a non-negative integer from a range bound, or reject the range.

    Raises:
        _InvalidRangeError: If ``value`` is not a non-negative decimal integer.
    """
    if not value.isdigit():
        raise _InvalidRangeError
    return int(value)


def _etag_matches(header_value: str, etag: str | None) -> bool:
    """Return whether a conditional ETag header matches the object's ETag.

    A ``*`` matches any existing object. Otherwise the comma-separated list of
    (optionally quoted) ETags is compared against the object's ETag after
    stripping surrounding quotes and any weak-validator ``W/`` prefix, which is
    the comparison S3/HTTP uses for ``If-Match`` / ``If-None-Match``.
    """
    if etag is None:
        return False
    candidates = [item.strip() for item in header_value.split(",")]
    if "*" in candidates:
        return True
    normalised = _normalise_etag(etag)
    return any(_normalise_etag(candidate) == normalised for candidate in candidates)


def _normalise_etag(value: str) -> str:
    """Strip a weak-validator prefix and surrounding quotes from an ETag."""
    text = value.strip()
    if text.startswith("W/"):
        text = text[2:]
    return text.strip('"')


def _quote_etag(etag: str) -> str:
    """Return the ETag wrapped in double quotes, as S3 emits it on the wire."""
    if etag.startswith('"') and etag.endswith('"'):
        return etag
    return f'"{etag}"'


def _parse_http_date(value: str) -> datetime | None:
    """Parse an HTTP-date header to an aware UTC datetime, or ``None`` if invalid.

    An unparseable conditional date is ignored (returns ``None``) so the condition
    is treated as absent rather than failing the request, matching lenient HTTP
    conditional-header handling.
    """
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    return _as_utc(parsed)


def _as_utc(value: datetime) -> datetime:
    """Return ``value`` as an aware UTC datetime.

    Naive datetimes (as PostgreSQL may hand back for a ``timestamp without time
    zone`` column) are assumed to be UTC, which is how the gateway stores
    ``last_modified``.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _resource(target: s3_router.ResolvedTarget) -> str | None:
    """Format the error-document ``Resource`` value for an object target."""
    if target.bucket is None:
        return None
    if target.key is None:
        return f"/{target.bucket}"
    return f"/{target.bucket}/{target.key}"


def _error_body(code: str, message: str, target: s3_router.ResolvedTarget) -> bytes:
    """Serialise an S3 error XML body for a non-``error_response`` status path.

    Used where the response needs headers (e.g. the 416 ``Content-Range``) that
    :func:`gateway.app.s3.router.error_response` does not set, so the body is
    built directly from the shared XML codec.
    """
    from gateway.app.s3 import xml_codecs

    return xml_codecs.serialise_error(
        xml_codecs.Error(code=code, message=message, resource=_resource(target))
    )


def create_get_object_handler(
    session_factory: AsyncSessionFactory,
    telegram: TelegramPort,
    cache: RecordCache,
    settings: Settings,
) -> GetObjectHandler:
    """Construct a :class:`GetObjectHandler` for registration on the router.

    A thin factory so the application-wiring task can build the handler without
    importing the class directly, mirroring the construction seams used by the
    other operation handlers.
    """
    return GetObjectHandler(session_factory, telegram, cache, settings)

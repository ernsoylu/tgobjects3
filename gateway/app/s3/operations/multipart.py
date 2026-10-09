"""Multipart upload — the six S3 operations that assemble a large object from
independently uploaded parts (Requirement 10).

S3 multipart upload lets a client split one object into ordered parts, upload
them in any order and with independent retries, then stitch them together into
a single object. This module implements the whole surface behind the router's
multipart slots:

    CreateMultipartUpload   POST /{bucket}/{key}?uploads
    UploadPart              PUT  /{bucket}/{key}?uploadId=..&partNumber=N
    CompleteMultipartUpload POST /{bucket}/{key}?uploadId=..
    AbortMultipartUpload    DELETE /{bucket}/{key}?uploadId=..
    ListParts               GET  /{bucket}/{key}?uploadId=..
    ListMultipartUploads    GET  /{bucket}?uploads

Where state lives
-----------------
An in-progress upload is tracked by two index tables (design "PostgreSQL
Schema"): ``multipart_uploads`` holds the session (``upload_id``, bucket, key,
``expires_at``) and ``multipart_parts`` holds one row per staged part
(``part_no``, ``size``, ``etag``, ``checksum``, ``staging_path``). The part
bodies themselves are fsynced into the staging directory exactly like a
PutObject body, so an interrupted or expired upload leaves only staging files
and index rows the GC Worker can sweep (Requirement 10.10, implemented by task
35). The index is a cache: nothing about an *in-progress* multipart upload is
durable in Telegram until Complete runs.

How Complete reuses the write path
----------------------------------
CompleteMultipartUpload does not invent a second durability path. It
concatenates the staged part bodies in ascending part-number order into one
content-addressed staging file, inserts a STAGED ``generation`` (kind
``multipart``) plus its upload job exactly as PutObject does, and then drives
that generation to COMMITTED through the same :class:`CommitDriver` seam the
PutObject handler uses (Requirement 10.4). The composite multipart ETag
(``<md5-of-concatenated-part-md5s>-<part-count>``, Requirement 10.9) is recorded
on the generation and returned in the final XML body.

Because stitching and committing a large object can take long enough to trip a
client/proxy idle timeout, Complete streams a ``200 OK`` whose body is a run of
insignificant whitespace emitted while the work proceeds, then the real
``CompleteMultipartUploadResult`` (or an inlined ``Error``) document — the
keep-alive pattern S3 itself uses (Requirement 10.4). Because the status line is
already sent, a late failure is reported as an ``Error`` document in the body
rather than a non-200 status.

Requirement mapping
-------------------
* **10.1** — :class:`CreateMultipartUpload` inserts a ``multipart_uploads`` row
  and returns its ``upload_id``.
* **10.2 / 10.3** — :class:`UploadPart` stages the part body with fsync +
  checksum and returns its ETag; it rejects an unknown/completed/aborted
  ``uploadId`` with ``NoSuchUpload`` and enforces the 5 MiB minimum part size
  for every non-final part at Complete.
* **10.4 / 10.5 / 10.9** — :class:`CompleteMultipartUpload` concatenates parts
  ascending, reuses the Upload Worker path, returns the composite ETag, and
  rejects a part list that names a missing part or has zero parts without
  touching stored data.
* **10.6** — :class:`AbortMultipartUpload` deletes every staged part file and
  removes the ``multipart_uploads`` row.
* **10.7 / 10.8** — :class:`ListParts` and :class:`ListMultipartUploads` report
  the staged parts and the in-progress uploads respectively.

Dependency wiring
-----------------
The router's :class:`~gateway.app.s3.router.S3HandlerContext` carries only the
request/target/auth/settings, so the collaborators a write needs — a session
factory, the staging reservation counter, the staging directory and the
:class:`CommitDriver` seam — are injected into :class:`MultipartHandler` at
construction time, mirroring :class:`~gateway.app.s3.operations.put_object.PutObjectHandler`.
A single instance is registered on all six multipart slots; it dispatches on
``context.operation`` in :meth:`MultipartHandler.__call__`.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import Response
from fastapi.responses import StreamingResponse
from sqlalchemy import delete as sql_delete
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.app.db.models import Bucket, Generation, MultipartPart, MultipartUpload
from gateway.app.db.session import AsyncSessionFactory
from gateway.app.s3 import xml_codecs
from gateway.app.s3.operations.put_object import CommitDriver
from gateway.app.s3.router import S3HandlerContext, S3Operation, error_response
from gateway.app.storage import generations as generations_storage
from gateway.app.storage.staging import (
    StagingHandle,
    StagingReservation,
    fsync_dir,
    fsync_file,
)

logger = logging.getLogger(__name__)

__all__ = ["MultipartHandler", "MultipartError", "MIN_PART_SIZE"]

# --- Protocol constants -------------------------------------------------------
#: The S3 minimum part size: every part except the highest-numbered one must be
#: at least 5 MiB (Requirement 10.2). Enforced at Complete, when the final part
#: is known, so a client may upload parts in any order.
MIN_PART_SIZE = 5 * 1024 * 1024

#: Size of each slice read from the request body / part files, in bytes.
_STREAM_CHUNK_SIZE = 1024 * 1024

#: One whitespace keep-alive frame emitted while CompleteMultipartUpload stitches
#: and commits, so idle timeouts do not fire before the final body (Requirement
#: 10.4). A single space is insignificant whitespace in XML, so a parser that
#: buffers the whole body still sees a valid document once the real result lands.
_KEEPALIVE_FRAME = b" "

#: Interval between keep-alive whitespace frames during a long Complete.
_KEEPALIVE_INTERVAL_SECONDS = 5.0

_HDR_CONTENT_LENGTH = "content-length"


class MultipartError(Exception):
    """Internal control-flow signal carrying a ready-to-send error response.

    Raised by the private steps to unwind to the dispatching ``__call__``, which
    returns the attached :class:`fastapi.Response`. Using an exception keeps the
    happy path linear while still guaranteeing staging files and reservations are
    released in the ``finally`` blocks that wrap each step, mirroring
    :class:`~gateway.app.s3.operations.put_object.PutObjectError`.
    """

    def __init__(self, response: Response) -> None:
        self.response = response
        super().__init__(f"multipart operation failed with HTTP {response.status_code}")


class MultipartHandler:
    """Handle all six multipart operations (Requirement 10).

    Constructed once with its collaborators and registered on the
    ``CREATE_MULTIPART_UPLOAD``, ``UPLOAD_PART``, ``COMPLETE_MULTIPART_UPLOAD``,
    ``ABORT_MULTIPART_UPLOAD``, ``LIST_PARTS`` and ``LIST_MULTIPART_UPLOADS``
    slots of the router's handler registry. :meth:`__call__` dispatches on the
    classified operation.
    """

    def __init__(
        self,
        *,
        session_factory: AsyncSessionFactory,
        staging: StagingReservation,
        staging_dir: Path,
        commit_driver: CommitDriver,
    ) -> None:
        """Create the handler.

        Args:
            session_factory: Hands out request-scoped async sessions for the
                ``multipart_uploads`` / ``multipart_parts`` reads and writes and,
                at Complete, the STAGED generation + upload-job insert.
            staging: The bounded in-memory reservation counter guarding
                ``STAGING_MAX_SIZE`` while a part body is streamed to disk
                (Requirements 6.3/6.4, reused for part staging).
            staging_dir: Root directory part bodies are streamed into and the
                concatenated object is assembled within.
            commit_driver: Drives the Complete-time generation to COMMITTED and
                reports the outcome, reused from the PutObject write path
                (Requirement 10.4).
        """
        self._session_factory = session_factory
        self._staging = staging
        self._staging_dir = Path(staging_dir)
        self._commit_driver = commit_driver

    async def __call__(self, context: S3HandlerContext) -> Response:
        """Dispatch to the operation named by ``context.operation``.

        Each branch catches :class:`MultipartError` and returns its attached
        response, so the per-operation methods can raise ready-made error
        documents from any depth.
        """
        try:
            match context.operation:
                case S3Operation.CREATE_MULTIPART_UPLOAD:
                    return await self._create(context)
                case S3Operation.UPLOAD_PART:
                    return await self._upload_part(context)
                case S3Operation.COMPLETE_MULTIPART_UPLOAD:
                    return await self._complete(context)
                case S3Operation.ABORT_MULTIPART_UPLOAD:
                    return await self._abort(context)
                case S3Operation.LIST_PARTS:
                    return await self._list_parts(context)
                case S3Operation.LIST_MULTIPART_UPLOADS:
                    return await self._list_uploads(context)
                case _:  # pragma: no cover - router never dispatches other ops here
                    return error_response(
                        code="InvalidRequest",
                        message=(
                            f"operation {context.operation.name} is not a "
                            "multipart operation"
                        ),
                        status_code=400,
                        resource=self._resource(context),
                    )
        except MultipartError as exc:
            return exc.response

    # -- CreateMultipartUpload (Requirement 10.1) ---------------------------

    async def _create(self, context: S3HandlerContext) -> Response:
        """Create a multipart upload record and return its ``UploadId``.

        Resolves the target bucket, inserts one ``multipart_uploads`` row with an
        ``expires_at`` derived from ``MULTIPART_EXPIRY`` (so the GC Worker can
        later sweep an abandoned upload, Requirement 10.10), and returns the
        generated ``upload_id`` in an ``InitiateMultipartUploadResult`` body.
        """
        bucket, key = self._require_object_target(context)
        upload_id = uuid.uuid4()
        # ``multipart_uploads.expires_at`` is a naive ``TIMESTAMP WITHOUT TIME
        # ZONE`` column (design "PostgreSQL Schema"); store a naive UTC instant
        # so the asyncpg timestamp codec can bind it, and so the naive
        # ``expires_at - MULTIPART_EXPIRY`` arithmetic in ListMultipartUploads
        # stays well-defined (Requirements 10.1, 10.8).
        expires_at = (
            datetime.now(UTC) + timedelta(seconds=context.settings.MULTIPART_EXPIRY)
        ).replace(tzinfo=None)

        async with self._session_factory.session() as session:
            bucket_row = await self._resolve_bucket(session, bucket, context)
            session.add(
                MultipartUpload(
                    upload_id=upload_id,
                    bucket_id=bucket_row.id,
                    key=key,
                    expires_at=expires_at,
                )
            )

        body = xml_codecs.serialise_initiate_multipart_upload(
            xml_codecs.InitiateMultipartUploadResponse(
                bucket=bucket, key=key, upload_id=upload_id.hex
            )
        )
        return Response(content=body, status_code=200, media_type="application/xml")

    # -- UploadPart (Requirements 10.2, 10.3) -------------------------------

    async def _upload_part(self, context: S3HandlerContext) -> Response:
        """Stage one part body with fsync + checksum and return its ETag.

        Validates the ``uploadId`` and ``partNumber`` query parameters, rejects
        an unknown/completed/aborted upload with ``NoSuchUpload`` (Requirement
        10.3), streams the body to a staging file while computing its MD5 (the
        part ETag), fsyncs it, and upserts the ``multipart_parts`` row. Re-uploading
        an existing part number replaces its staged body — the standard S3 retry
        behaviour — and the superseded staging file is removed. The 5 MiB minimum
        is *not* enforced here (Requirement 10.2 defers it to Complete, where the
        final part is known).
        """
        bucket, key = self._require_object_target(context)
        upload_id = self._require_upload_id(context)
        part_number = self._require_part_number(context)

        async with self._session_factory.session() as session:
            upload = await self._resolve_upload(session, upload_id, context)
            self._check_upload_key(upload, bucket, key, context)

        handle, staging_path, md5_hex, size = await self._stage_part_body(
            context, bucket, key
        )
        committed = False
        try:
            previous_path = await self._record_part(
                upload_id, part_number, size, md5_hex, os.fspath(staging_path)
            )
            committed = True
        finally:
            await self._staging.release(handle)
            if not committed:
                _unlink_quietly(staging_path)

        if previous_path is not None and previous_path != os.fspath(staging_path):
            # A re-upload of the same part number replaced an earlier body.
            _unlink_quietly(Path(previous_path))

        return Response(status_code=200, headers={"ETag": f'"{md5_hex}"'})

    async def _stage_part_body(
        self, context: S3HandlerContext, bucket: str, key: str
    ) -> tuple[StagingHandle, Path, str, int]:
        """Reserve, stream, hash and fsync a part body into the staging dir.

        Returns the reservation handle (the caller releases it), the final
        content-addressed staging path, the MD5 hex of the body (the part ETag)
        and the received size. Raises :class:`MultipartError` on a missing/invalid
        ``Content-Length`` (HTTP 411/400) or a full staging area (HTTP 507),
        reusing the admission control and fsync helpers of the PutObject path.
        """
        content_length = self._content_length(context)
        handle = await self._reserve(content_length, bucket, key)
        temp_path = self._new_staging_path()
        md5 = hashlib.md5()
        total = 0
        self._staging_dir.mkdir(parents=True, exist_ok=True)
        try:
            with open(temp_path, "wb") as fh:
                async for chunk in context.request.stream():
                    if not chunk:
                        continue
                    total += len(chunk)
                    fh.write(chunk)
                    md5.update(chunk)
            md5_hex = md5.hexdigest()
            final_path = self._content_addressed_part_path(md5_hex, total)
            final_path.parent.mkdir(parents=True, exist_ok=True)
            fsync_file(temp_path)
            os.replace(temp_path, final_path)
            fsync_dir(final_path.parent)
        except BaseException:
            _unlink_quietly(temp_path)
            await self._staging.release(handle)
            raise
        return handle, final_path, md5_hex, total

    async def _record_part(
        self,
        upload_id: uuid.UUID,
        part_number: int,
        size: int,
        md5_hex: str,
        staging_path: str,
    ) -> str | None:
        """Insert or replace the ``multipart_parts`` row for ``part_number``.

        Returns the staging path of any part body this upload replaced (so the
        caller can unlink the superseded file), or ``None`` for a first upload of
        this part number.
        """
        async with self._session_factory.session() as session:
            existing = (
                await session.execute(
                    select(MultipartPart).where(
                        MultipartPart.upload_id == upload_id,
                        MultipartPart.part_no == part_number,
                    )
                )
            ).scalar_one_or_none()
            previous_path = existing.staging_path if existing is not None else None
            if existing is None:
                session.add(
                    MultipartPart(
                        upload_id=upload_id,
                        part_no=part_number,
                        size=size,
                        etag=f'"{md5_hex}"',
                        checksum={"md5": md5_hex},
                        staging_path=staging_path,
                    )
                )
            else:
                existing.size = size
                existing.etag = f'"{md5_hex}"'
                existing.checksum = {"md5": md5_hex}
                existing.staging_path = staging_path
        return previous_path

    # -- CompleteMultipartUpload (Requirements 10.4, 10.5, 10.9) ------------

    async def _complete(self, context: S3HandlerContext) -> Response:
        """Validate the part list, then stream a keep-alive 200 while committing.

        The request body names the parts (number + ETag) the client expects to
        assemble. This validates them against the staged rows *before* any
        keep-alive is sent (Requirement 10.5: a bad list changes no stored data),
        then returns a :class:`StreamingResponse` whose generator emits whitespace
        while :meth:`_assemble_and_commit` stitches the parts and drives the
        commit, finishing with the real ``CompleteMultipartUploadResult`` body
        (Requirement 10.4). A failure after the status line is reported as an
        inlined ``Error`` document.
        """
        bucket, key = self._require_object_target(context)
        upload_id = self._require_upload_id(context)

        body = await context.request.body()
        try:
            requested = xml_codecs.parse_complete_multipart_upload_request(body)
        except xml_codecs.MalformedXMLError as exc:
            raise MultipartError(
                error_response(
                    code="MalformedXML",
                    message=str(exc),
                    status_code=400,
                    resource=self._resource(context),
                )
            ) from exc

        async with self._session_factory.session() as session:
            upload = await self._resolve_upload(session, upload_id, context)
            self._check_upload_key(upload, bucket, key, context)
            staged = await self._load_parts(session, upload_id)

        ordered = self._validate_part_list(requested, staged, context)

        async def stream() -> AsyncIterator[bytes]:
            # Hand the stitch/commit work a channel to emit keep-alive frames on
            # while it runs, then yield the final document it produces.
            async for frame in self._complete_stream(
                context, bucket, key, upload_id, ordered
            ):
                yield frame

        return StreamingResponse(
            stream(),
            status_code=200,
            media_type="application/xml",
        )

    async def _complete_stream(
        self,
        context: S3HandlerContext,
        bucket: str,
        key: str,
        upload_id: uuid.UUID,
        ordered: list[MultipartPart],
    ) -> AsyncIterator[bytes]:
        """Yield keep-alive whitespace, then the final Complete body.

        Runs the stitch-and-commit as a background task and emits a whitespace
        frame every :data:`_KEEPALIVE_INTERVAL_SECONDS` until it finishes, so a
        slow Complete does not trip an idle timeout (Requirement 10.4). The final
        frame is either the ``CompleteMultipartUploadResult`` document or, on a
        late failure, an inlined ``Error`` document.
        """
        task = asyncio.ensure_future(
            self._assemble_and_commit(context, bucket, key, upload_id, ordered)
        )
        while True:
            done, _ = await asyncio.wait(
                {task}, timeout=_KEEPALIVE_INTERVAL_SECONDS
            )
            if done:
                break
            yield _KEEPALIVE_FRAME

        try:
            yield task.result()
        except MultipartError as exc:
            yield _error_body(exc.response)
        except Exception as exc:  # noqa: BLE001 - surfaced as an inlined Error body
            logger.exception(
                "CompleteMultipartUpload %s/%s (upload %s) failed",
                bucket,
                key,
                upload_id.hex,
            )
            yield xml_codecs.serialise_error(
                xml_codecs.Error(
                    code="InternalError",
                    message=f"the multipart upload could not be completed: {exc}",
                    resource=self._resource(context),
                )
            )

    async def _assemble_and_commit(
        self,
        context: S3HandlerContext,
        bucket: str,
        key: str,
        upload_id: uuid.UUID,
        ordered: list[MultipartPart],
    ) -> bytes:
        """Concatenate parts, insert the generation, drive commit, return the body.

        Stitches the staged part bodies in ascending part-number order into one
        content-addressed staging file (Requirement 10.4), inserts a STAGED
        ``generation`` (kind ``multipart``) plus its upload job, records the
        composite multipart ETag (Requirement 10.9), and drives the generation to
        COMMITTED through the shared :class:`CommitDriver`. On success it removes
        the ``multipart_uploads`` / ``multipart_parts`` rows and the per-part
        staging files, then returns the serialised ``CompleteMultipartUploadResult``.
        """
        etag = self._composite_etag(ordered)
        total_size = sum(part.size for part in ordered)

        handle = await self._reserve(max(total_size, 1), bucket, key)
        assembled_path = self._new_staging_path()
        generation_id: uuid.UUID | None = None
        try:
            sha256_hex = self._concatenate_parts(ordered, assembled_path)
            final_path = self._content_addressed_object_path(sha256_hex)
            final_path.parent.mkdir(parents=True, exist_ok=True)
            fsync_file(assembled_path)
            os.replace(assembled_path, final_path)
            fsync_dir(final_path.parent)
            assembled_path = final_path

            generation_id = await self._insert_generation(
                context, bucket, key, final_path, total_size, etag, sha256_hex
            )
        finally:
            await self._staging.release(handle)
            if generation_id is None:
                _unlink_quietly(assembled_path)

        outcome = await self._commit_driver.drive_to_commit(generation_id)
        if not outcome.committed:
            raise MultipartError(self._commit_failure_response(outcome, context))

        await self._finalise_upload(upload_id, ordered)

        return xml_codecs.serialise_complete_multipart_upload(
            xml_codecs.CompleteMultipartUploadResponse(
                location=f"/{bucket}/{key}",
                bucket=bucket,
                key=key,
                etag=f'"{etag}"',
            )
        )

    def _concatenate_parts(
        self, ordered: list[MultipartPart], destination: Path
    ) -> str:
        """Stream the ordered part bodies into ``destination``; return SHA-256 hex.

        The parts are already in ascending part-number order (Requirement 10.4).
        The concatenation is hashed with SHA-256 as it is written so the resulting
        object is content-addressed exactly like a single-part PutObject body,
        which lets the Upload Worker dedup it against identical content.
        """
        sha256 = hashlib.sha256()
        self._staging_dir.mkdir(parents=True, exist_ok=True)
        with open(destination, "wb") as out:
            for part in ordered:
                with open(part.staging_path, "rb") as src:
                    while True:
                        chunk = src.read(_STREAM_CHUNK_SIZE)
                        if not chunk:
                            break
                        out.write(chunk)
                        sha256.update(chunk)
        return sha256.hexdigest()

    async def _insert_generation(
        self,
        context: S3HandlerContext,
        bucket: str,
        key: str,
        staging_path: Path,
        size: int,
        etag: str,
        sha256_hex: str,
    ) -> uuid.UUID:
        """Insert the STAGED multipart generation + its upload job, return its id.

        Mirrors the PutObject staging insert: one transaction creates the STAGED
        ``generation`` (kind ``multipart``) and its queued upload job, then
        records the composite ETag and the dedup SHA-256 the Upload Worker reads.
        """
        content_type = context.request.headers.get("content-type")
        async with self._session_factory.session() as session:
            bucket_row = await self._resolve_bucket(session, bucket, context)
            generation_id = await generations_storage.insert_staged(
                session,
                bucket_id=bucket_row.id,
                key=key,
                kind="multipart",
                size=size,
                staging_path=os.fspath(staging_path),
                content_type=content_type,
                # ``generations.last_modified`` is a naive ``TIMESTAMP WITHOUT
                # TIME ZONE`` column; store a naive UTC instant so the asyncpg
                # timestamp codec can bind it (Requirement 10.4).
                last_modified=datetime.now(UTC).replace(tzinfo=None),
            )
            await session.execute(
                update(Generation)
                .where(Generation.id == generation_id)
                .values(etag=f'"{etag}"', checksums={"sha256": sha256_hex})
            )
        return generation_id

    async def _finalise_upload(
        self, upload_id: uuid.UUID, ordered: list[MultipartPart]
    ) -> None:
        """Remove the upload's index rows and its per-part staging files.

        Called only after the generation is COMMITTED, so the staged parts are no
        longer needed. The ``multipart_parts`` and ``multipart_uploads`` rows are
        deleted in one transaction; the per-part staging files are unlinked
        best-effort afterwards (a leftover file is a GC concern, not a
        correctness one).
        """
        async with self._session_factory.session() as session:
            await session.execute(
                sql_delete(MultipartPart).where(
                    MultipartPart.upload_id == upload_id
                )
            )
            await session.execute(
                sql_delete(MultipartUpload).where(
                    MultipartUpload.upload_id == upload_id
                )
            )
        for part in ordered:
            _unlink_quietly(Path(part.staging_path))

    def _commit_failure_response(
        self, outcome: object, context: S3HandlerContext
    ) -> Response:
        """Build the error document for a non-committed Complete outcome.

        A Telegram-unreachable timeout maps to ``ServiceUnavailable`` (503) with
        the generation retained for retry; any other terminal state maps to
        ``InternalError`` (500), mirroring the PutObject commit-failure mapping
        (Requirements 6.14/6.13 reused by 10.4).
        """
        timed_out = bool(getattr(outcome, "timed_out", False))
        if timed_out:
            return error_response(
                code="ServiceUnavailable",
                message="Telegram was unreachable; the object is staged for retry",
                status_code=503,
                resource=self._resource(context),
            )
        return error_response(
            code="InternalError",
            message="the multipart object could not be committed to durable storage",
            status_code=500,
            resource=self._resource(context),
        )

    def _validate_part_list(
        self,
        requested: tuple[xml_codecs.CompletePart, ...],
        staged: dict[int, MultipartPart],
        context: S3HandlerContext,
    ) -> list[MultipartPart]:
        """Validate the client's part list against the staged parts.

        Enforces Requirement 10.5 (reject a list with zero parts, or that names a
        part number that was never uploaded), Requirement 10.2 (every part except
        the highest-numbered one must be at least 5 MiB), and that the client's
        ETags match the staged ones. On any failure a :class:`MultipartError` is
        raised *before* any stored data is touched; on success the staged parts
        are returned in ascending part-number order ready for concatenation.
        """
        if not requested:
            raise MultipartError(
                error_response(
                    code="InvalidRequest",
                    message="the part list must contain at least one part",
                    status_code=400,
                    resource=self._resource(context),
                )
            )

        # The client must list parts in ascending order with no duplicates.
        numbers = [part.part_number for part in requested]
        if numbers != sorted(numbers) or len(set(numbers)) != len(numbers):
            raise MultipartError(
                error_response(
                    code="InvalidPartOrder",
                    message="the part list must be in ascending part-number order",
                    status_code=400,
                    resource=self._resource(context),
                )
            )

        ordered: list[MultipartPart] = []
        last_index = len(requested) - 1
        for position, requested_part in enumerate(requested):
            staged_part = staged.get(requested_part.part_number)
            if staged_part is None:
                raise MultipartError(
                    error_response(
                        code="InvalidPart",
                        message=(
                            f"part {requested_part.part_number} was not uploaded"
                        ),
                        status_code=400,
                        resource=self._resource(context),
                    )
                )
            if not _etags_match(requested_part.etag, staged_part.etag):
                raise MultipartError(
                    error_response(
                        code="InvalidPart",
                        message=(
                            f"the ETag for part {requested_part.part_number} does "
                            "not match the staged part"
                        ),
                        status_code=400,
                        resource=self._resource(context),
                    )
                )
            if position != last_index and staged_part.size < MIN_PART_SIZE:
                raise MultipartError(
                    error_response(
                        code="EntityTooSmall",
                        message=(
                            f"part {requested_part.part_number} is smaller than the "
                            f"{MIN_PART_SIZE}-byte minimum part size"
                        ),
                        status_code=400,
                        resource=self._resource(context),
                    )
                )
            ordered.append(staged_part)
        return ordered

    def _composite_etag(self, ordered: list[MultipartPart]) -> str:
        """Compute the composite multipart ETag (Requirement 10.9).

        The ETag is ``<md5-of-concatenated-part-md5s>-<part-count>``: the raw MD5
        digests of the parts (in ascending part-number order) are concatenated,
        hashed with MD5, and the hex digest is suffixed with ``-<count>``.
        """
        digest = hashlib.md5()
        for part in ordered:
            digest.update(bytes.fromhex(_strip_etag(part.etag)))
        return f"{digest.hexdigest()}-{len(ordered)}"

    # -- AbortMultipartUpload (Requirement 10.6) ----------------------------

    async def _abort(self, context: S3HandlerContext) -> Response:
        """Delete all staged parts and remove the multipart upload record.

        Resolves the upload (``NoSuchUpload`` if unknown), collects the staged
        part staging paths, deletes the ``multipart_parts`` and
        ``multipart_uploads`` rows in one transaction, then unlinks the part
        staging files. Returns HTTP 204 with no body, as S3 does.
        """
        bucket, key = self._require_object_target(context)
        upload_id = self._require_upload_id(context)

        async with self._session_factory.session() as session:
            upload = await self._resolve_upload(session, upload_id, context)
            self._check_upload_key(upload, bucket, key, context)
            staged = await self._load_parts(session, upload_id)
            paths = [part.staging_path for part in staged.values()]
            await session.execute(
                sql_delete(MultipartPart).where(
                    MultipartPart.upload_id == upload_id
                )
            )
            await session.execute(
                sql_delete(MultipartUpload).where(
                    MultipartUpload.upload_id == upload_id
                )
            )

        for path in paths:
            _unlink_quietly(Path(path))
        return Response(status_code=204)

    # -- ListParts (Requirement 10.7) ---------------------------------------

    async def _list_parts(self, context: S3HandlerContext) -> Response:
        """Return the part number, size and ETag for each staged part.

        Resolves the upload (``NoSuchUpload`` if unknown) and emits a
        ``ListPartsResult`` body with the staged parts in ascending part-number
        order. A ``LastModified`` is required by the schema but not tracked per
        part, so the current time is reported.
        """
        bucket, key = self._require_object_target(context)
        upload_id = self._require_upload_id(context)

        async with self._session_factory.session() as session:
            upload = await self._resolve_upload(session, upload_id, context)
            self._check_upload_key(upload, bucket, key, context)
            staged = await self._load_parts(session, upload_id)

        now = datetime.now(UTC)
        parts = tuple(
            xml_codecs.Part(
                part_number=part.part_no,
                last_modified=now,
                etag=part.etag,
                size=part.size,
            )
            for part in sorted(staged.values(), key=lambda p: p.part_no)
        )
        body = xml_codecs.serialise_list_parts(
            xml_codecs.ListPartsResponse(
                bucket=bucket,
                key=key,
                upload_id=upload_id.hex,
                parts=parts,
            )
        )
        return Response(content=body, status_code=200, media_type="application/xml")

    # -- ListMultipartUploads (Requirement 10.8) ----------------------------

    async def _list_uploads(self, context: S3HandlerContext) -> Response:
        """Return the UploadId, key and initiation timestamp for each in-progress upload.

        The ``multipart_uploads`` schema stores ``expires_at`` rather than the
        initiation time, so the initiation timestamp reported here is derived by
        subtracting ``MULTIPART_EXPIRY`` from ``expires_at`` — the exact instant
        CreateMultipartUpload used to set it (Requirement 10.8).
        """
        bucket = context.target.bucket
        if bucket is None:
            raise MultipartError(
                error_response(
                    code="InvalidRequest",
                    message="ListMultipartUploads requires a bucket",
                    status_code=400,
                    resource=None,
                )
            )
        expiry = timedelta(seconds=context.settings.MULTIPART_EXPIRY)

        async with self._session_factory.session() as session:
            bucket_row = await self._resolve_bucket(session, bucket, context)
            rows = (
                (
                    await session.execute(
                        select(MultipartUpload)
                        .where(MultipartUpload.bucket_id == bucket_row.id)
                        .order_by(MultipartUpload.key, MultipartUpload.upload_id)
                    )
                )
                .scalars()
                .all()
            )

        uploads = tuple(
            xml_codecs.MultipartUpload(
                key=row.key,
                upload_id=row.upload_id.hex,
                initiated=row.expires_at - expiry,
            )
            for row in rows
        )
        body = xml_codecs.serialise_list_multipart_uploads(
            xml_codecs.ListMultipartUploadsResponse(bucket=bucket, uploads=uploads)
        )
        return Response(content=body, status_code=200, media_type="application/xml")

    # -- Shared resolution + validation helpers -----------------------------

    def _require_object_target(self, context: S3HandlerContext) -> tuple[str, str]:
        """Return the (bucket, key) of an object-level request, or raise HTTP 400.

        The router only dispatches object-level multipart operations here, so a
        missing bucket/key is a defensive guard rather than an expected branch.
        """
        bucket = context.target.bucket
        key = context.target.key
        if bucket is None or key is None:
            raise MultipartError(
                error_response(
                    code="InvalidRequest",
                    message="this multipart operation requires a bucket and key",
                    status_code=400,
                    resource=self._resource(context),
                )
            )
        return bucket, key

    def _require_upload_id(self, context: S3HandlerContext) -> uuid.UUID:
        """Parse the ``uploadId`` query parameter into a UUID, or raise NoSuchUpload.

        A malformed ``uploadId`` can never match a staged upload, so it is
        rejected with the same ``NoSuchUpload`` error as a syntactically valid but
        unknown id (Requirement 10.3).
        """
        raw = context.request.query_params.get("uploadId")
        if not raw:
            raise MultipartError(self._no_such_upload(context))
        try:
            return uuid.UUID(raw)
        except ValueError as exc:
            raise MultipartError(self._no_such_upload(context)) from exc

    def _require_part_number(self, context: S3HandlerContext) -> int:
        """Parse and validate the ``partNumber`` query parameter.

        S3 part numbers are integers in the inclusive range 1–10000; anything
        else is rejected with ``InvalidArgument`` (HTTP 400).
        """
        raw = context.request.query_params.get("partNumber")
        try:
            value = int(raw) if raw is not None else -1
        except ValueError:
            value = -1
        if value < 1 or value > 10000:
            raise MultipartError(
                error_response(
                    code="InvalidArgument",
                    message="partNumber must be an integer between 1 and 10000",
                    status_code=400,
                    resource=self._resource(context),
                )
            )
        return value

    async def _resolve_bucket(
        self, session: AsyncSession, bucket: str, context: S3HandlerContext
    ) -> Bucket:
        """Load the bucket row, raising HTTP 404 ``NoSuchBucket`` if absent."""
        row = (
            await session.execute(select(Bucket).where(Bucket.name == bucket))
        ).scalar_one_or_none()
        if row is None:
            raise MultipartError(
                error_response(
                    code="NoSuchBucket",
                    message="the specified bucket does not exist",
                    status_code=404,
                    resource=self._resource(context),
                )
            )
        return row

    async def _resolve_upload(
        self,
        session: AsyncSession,
        upload_id: uuid.UUID,
        context: S3HandlerContext,
    ) -> MultipartUpload:
        """Load the ``multipart_uploads`` row, or raise ``NoSuchUpload``.

        An upload that was completed or aborted has had its row removed, so a
        missing row covers "unknown", "completed" and "aborted" alike
        (Requirement 10.3).
        """
        upload = (
            await session.execute(
                select(MultipartUpload).where(
                    MultipartUpload.upload_id == upload_id
                )
            )
        ).scalar_one_or_none()
        if upload is None:
            raise MultipartError(self._no_such_upload(context))
        return upload

    def _check_upload_key(
        self,
        upload: MultipartUpload,
        bucket: str,
        key: str,
        context: S3HandlerContext,
    ) -> None:
        """Reject an ``uploadId`` that belongs to a different (bucket, key).

        A valid ``uploadId`` addressed at the wrong object is treated as unknown
        rather than silently acting on the real upload, mirroring S3's refusal to
        cross object boundaries (Requirement 10.3). ``bucket`` is accepted for a
        symmetric signature; the upload row is already scoped to its bucket via
        ``upload_id``, so the key check is the discriminating one.
        """
        del bucket
        if upload.key != key:
            raise MultipartError(self._no_such_upload(context))

    async def _load_parts(
        self, session: AsyncSession, upload_id: uuid.UUID
    ) -> dict[int, MultipartPart]:
        """Load every staged part of an upload keyed by its part number."""
        rows = (
            (
                await session.execute(
                    select(MultipartPart).where(
                        MultipartPart.upload_id == upload_id
                    )
                )
            )
            .scalars()
            .all()
        )
        return {row.part_no: row for row in rows}

    def _no_such_upload(self, context: S3HandlerContext) -> Response:
        """Build the standard ``NoSuchUpload`` (HTTP 404) error document."""
        return error_response(
            code="NoSuchUpload",
            message="the specified multipart upload does not exist",
            status_code=404,
            resource=self._resource(context),
        )

    # -- Staging / filesystem helpers (shared with the PutObject path) ------

    def _content_length(self, context: S3HandlerContext) -> int:
        """Return the declared ``Content-Length`` of a part, or raise HTTP 411/400.

        A part body must declare its length so it can be reserved against the
        staging ceiling before any byte is read (Requirement 6.3, reused).
        """
        raw = context.request.headers.get(_HDR_CONTENT_LENGTH)
        if raw is None:
            raise MultipartError(
                error_response(
                    code="MissingContentLength",
                    message="UploadPart requires a Content-Length header",
                    status_code=411,
                    resource=self._resource(context),
                )
            )
        try:
            value = int(raw)
        except ValueError as exc:
            raise MultipartError(
                error_response(
                    code="InvalidRequest",
                    message="Content-Length is not an integer",
                    status_code=400,
                    resource=self._resource(context),
                )
            ) from exc
        if value < 0:
            raise MultipartError(
                error_response(
                    code="InvalidRequest",
                    message="Content-Length must be non-negative",
                    status_code=400,
                    resource=self._resource(context),
                )
            )
        return value

    async def _reserve(
        self, size: int, bucket: str, key: str
    ) -> StagingHandle:
        """Reserve ``size`` bytes of staging, or raise HTTP 507.

        Reuses the bounded admission counter of the PutObject path; a refused
        reservation means the staging area is full and no body is read/written
        (Requirement 6.4, reused for part staging).
        """
        handle = await self._staging.reserve(size if size > 0 else 1)
        if handle is None:
            logger.info(
                "staging full; refusing multipart body %s/%s (%d bytes)",
                bucket,
                key,
                size,
            )
            raise MultipartError(
                error_response(
                    code="InsufficientStorage",
                    message="staging area is full",
                    status_code=507,
                    resource=f"/{bucket}/{key}",
                )
            )
        return handle

    def _new_staging_path(self) -> Path:
        """Return a unique temp path inside the staging dir for an incoming body."""
        return self._staging_dir / f"incoming-{uuid.uuid4().hex}"

    def _content_addressed_part_path(self, md5_hex: str, size: int) -> Path:
        """Return a staging path for a part body addressed by its MD5 and size.

        Parts live under a ``parts/`` subtree with a two-level fan-out so no
        single directory accumulates unbounded entries. The size is folded into
        the name so two different bodies that collide on MD5 do not clobber each
        other's staging file.
        """
        return (
            self._staging_dir
            / "parts"
            / md5_hex[:2]
            / f"{md5_hex}-{size}"
        )

    def _content_addressed_object_path(self, sha256_hex: str) -> Path:
        """Return the content-addressed staging path for the assembled object.

        Uses the same two-level SHA-256 fan-out as the PutObject write path so
        the Upload Worker can dedup an assembled multipart object against
        identical single-part content.
        """
        return self._staging_dir / sha256_hex[:2] / sha256_hex

    @staticmethod
    def _resource(context: S3HandlerContext) -> str | None:
        """Format the error-document ``Resource`` value for the request target."""
        target = context.target
        if target.bucket is None:
            return None
        if target.key is None:
            return f"/{target.bucket}"
        return f"/{target.bucket}/{target.key}"


def _strip_etag(etag: str) -> str:
    """Return an ETag's hex MD5 payload without surrounding quotes."""
    return etag.strip().strip('"')


def _etags_match(left: str, right: str) -> bool:
    """Compare two ETags ignoring surrounding quotes and case."""
    return _strip_etag(left).lower() == _strip_etag(right).lower()


def _error_body(response: Response) -> bytes:
    """Extract the XML body bytes from a prebuilt error :class:`Response`.

    Complete has already sent its ``200`` status line before a late failure is
    known, so the failure is delivered as the response body rather than a status
    code; this pulls the serialised ``Error`` document out of the response the
    error helpers built.
    """
    body = response.body
    if isinstance(body, bytes):
        return body
    if isinstance(body, memoryview):
        return bytes(body)
    return str(body).encode("utf-8")


def _unlink_quietly(path: Path) -> None:
    """Remove ``path`` if it exists, ignoring a missing file.

    Used in cleanup paths where the staging file may already be gone (never
    created, renamed away, or removed by an earlier failure branch).
    """
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as exc:  # pragma: no cover - best-effort cleanup
        logger.warning("could not remove staging file %s: %s", path, exc)

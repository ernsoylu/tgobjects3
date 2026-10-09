"""PutObject — stage a body, verify its integrity, and commit it durably.

This module implements the second half of the design's write path (the first
half — SigV4 verification, addressing, operation classification — is done by the
router before this handler runs):

    staging reserve -> stream + checksum -> fsync + rename ->
    DB: insert generation(STAGED) + upload_job ->
    [sync] block until COMMITTED -> HTTP 200
    [async] return HTTP 200 at STAGED

It is the concrete handler behind :data:`S3Operation.PUT_OBJECT`. Because the
router's :class:`~gateway.app.s3.router.S3HandlerContext` carries only
request/target/auth/settings, the extra collaborators a write needs — a database
session factory, the staging reservation counter, the staging directory, and the
driver that advances a generation to COMMITTED — are injected into
:class:`PutObjectHandler` at construction time. The instance is itself an
``S3Handler`` (it defines ``__call__``) and is registered on the router's
``S3Handlers`` registry by the application wiring.

Requirement mapping
-------------------
The acceptance criteria of Requirement 6 map onto the handler as follows:

* **6.1 / 6.2** — SigV4 is verified by the router before this handler is reached
  (``context.auth`` is already populated); the handler never writes to staging
  when auth has failed because it is never invoked in that case.
* **6.3 / 6.4** — :meth:`PutObjectHandler._reserve` reserves the declared
  ``Content-Length`` against :class:`~gateway.app.storage.staging.StagingReservation`
  before any body byte is read; a refused reservation returns HTTP 507 with no
  body consumed.
* **6.5** — :meth:`PutObjectHandler._stream_to_staging` aborts and returns HTTP
  413 as soon as the running body size exceeds ``MAX_OBJECT_SIZE``, deleting the
  partial staging file.
* **6.6** — MD5, SHA-256, and any client-declared checksum algorithm are
  computed over the received bytes while they are streamed.
* **6.7 / 6.8** — a mismatching ``Content-MD5`` or ``x-amz-checksum-*`` value
  returns HTTP 400 and deletes the staging file before any commit.
* **6.9** — on success the body is fsynced, renamed to its content-addressed
  path, the directory is fsynced, and the STAGED generation plus its upload job
  are inserted in one database transaction.
* **6.10–6.14** — the actual Telegram upload and the COMMITTED transition are
  owned by the Upload Worker; the handler drives that worker to completion for a
  sync bucket via the injected :class:`CommitDriver`, surfacing HTTP 503 when
  Telegram is unreachable past the 30 s boundary and 500 on an exhausted upload.
* **6.15** — for an async-durability bucket the handler returns HTTP 200 as soon
  as the generation is STAGED and the job enqueued, without waiting.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from fastapi import Response
from sqlalchemy import Update, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gateway.app.db.models import Bucket, Generation
from gateway.app.s3 import checksums as checksums_mod
from gateway.app.s3.router import S3HandlerContext, error_response
from gateway.app.storage import generations as generations_storage
from gateway.app.storage.staging import (
    StagingHandle,
    StagingReservation,
    fsync_dir,
    fsync_file,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DURABILITY_ASYNC",
    "DURABILITY_SYNC",
    "CommitDriver",
    "CommitOutcome",
    "PutObjectHandler",
    "PutObjectError",
]

# --- Durability modes ---------------------------------------------------------
#: Bucket durability where PutObject blocks until the generation is COMMITTED.
DURABILITY_SYNC = "sync"
#: Bucket durability where PutObject returns 200 at STAGED without waiting.
DURABILITY_ASYNC = "async"

# --- Streaming -----------------------------------------------------------------
#: Size of each chunk read from the request body stream, in bytes.
_STREAM_CHUNK_SIZE = 1024 * 1024

# --- Headers -------------------------------------------------------------------
_HDR_CONTENT_LENGTH = "content-length"
_HDR_CONTENT_MD5 = "content-md5"
_HDR_CONTENT_TYPE = "content-type"
_HDR_CHECKSUM_ALGORITHM = "x-amz-checksum-algorithm"
#: Prefix of the per-algorithm checksum value headers, e.g.
#: ``x-amz-checksum-sha256``. The suffix is the lower-cased algorithm name with
#: hyphens removed (``sha256``, ``crc32c``, …).
_HDR_CHECKSUM_PREFIX = "x-amz-checksum-"
#: Prefix of user-metadata headers preserved on the generation.
_HDR_META_PREFIX = "x-amz-meta-"

#: Map the client's declared checksum algorithm name to the header suffix S3
#: uses to carry its value. Keys are the normalised algorithm names accepted by
#: :func:`gateway.app.s3.checksums.compute`.
_CHECKSUM_HEADER_SUFFIX: dict[str, str] = {
    "CRC32": "crc32",
    "CRC32C": "crc32c",
    "CRC64NVME": "crc64nvme",
    "SHA-1": "sha1",
    "SHA-256": "sha256",
}


class PutObjectError(Exception):
    """Internal control-flow signal carrying a ready-to-send error response.

    Raised by the handler's private steps to unwind to :meth:`PutObjectHandler.__call__`,
    which returns the attached :class:`fastapi.Response`. Using an exception keeps
    the happy path linear while still guaranteeing the staging file and
    reservation are released in the ``finally`` blocks that wrap each step.
    """

    def __init__(self, response: Response) -> None:
        self.response = response
        super().__init__(f"PutObject failed with HTTP {response.status_code}")


@dataclass(frozen=True)
class CommitOutcome:
    """The terminal result of driving a sync-durability generation to commit.

    Attributes:
        committed: ``True`` when the generation reached COMMITTED (durable in
            Telegram); ``False`` for any non-durable terminal state.
        state: The generation's final state string (``COMMITTED`` / ``FAILED`` /
            ``RETRY_WAIT`` / …) for diagnostics and response selection.
        etag: The committed object's ETag, when known, echoed in the 200 response.
        timed_out: ``True`` when the driver gave up because Telegram was
            unreachable past the 30 s boundary; the handler maps this to HTTP 503
            and the STAGED generation is retained for retry (Requirement 6.14).
    """

    committed: bool
    state: str
    etag: str | None = None
    timed_out: bool = False


@runtime_checkable
class CommitDriver(Protocol):
    """Advances a STAGED generation to its terminal state for a sync PUT.

    A seam over the Upload Worker so the handler does not depend on Telegram or
    the worker's internals directly. The production implementation claims and
    processes the generation's upload job (sealing records, sending chunks, the
    blob manifest, and the commit record) and reports the result; a test can
    supply a fake that returns a canned :class:`CommitOutcome`. The driver must
    honour the 30 s Telegram-unreachable boundary (Requirement 6.14) by returning
    an outcome with ``timed_out=True`` rather than blocking indefinitely.
    """

    async def drive_to_commit(self, generation_id: uuid.UUID) -> CommitOutcome:
        """Process ``generation_id``'s upload job and return its terminal outcome."""
        ...


class PutObjectHandler:
    """The ``PUT /{bucket}/{key}`` operation handler.

    Constructed once with its collaborators and registered as the handler for
    :data:`~gateway.app.s3.router.S3Operation.PUT_OBJECT`. Each invocation drives
    one request through the write path described in the module docstring.

    Attributes are private; see :meth:`__init__` for what each collaborator does.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        staging: StagingReservation,
        staging_dir: Path,
        commit_driver: CommitDriver,
    ) -> None:
        """Create the handler.

        Args:
            session_factory: Produces ``AsyncSession`` instances; the STAGED
                generation + upload-job insert runs in one transaction opened
                here (Requirement 6.9).
            staging: The bounded in-memory reservation counter guarding
                ``STAGING_MAX_SIZE`` (Requirements 6.3/6.4).
            staging_dir: Root directory the body is streamed into and the
                content-addressed file is renamed within (Requirement 6.9).
            commit_driver: Drives a sync-durability generation to COMMITTED and
                reports the outcome (Requirements 6.13/6.14); unused for async
                buckets.
        """
        self._session_factory = session_factory
        self._staging = staging
        self._staging_dir = Path(staging_dir)
        self._commit_driver = commit_driver

    async def __call__(self, context: S3HandlerContext) -> Response:
        """Execute PutObject for ``context`` and return its HTTP response.

        Orchestrates the write path and guarantees cleanup: the staging
        reservation and any partial staging file are always released, whether the
        request succeeds, fails validation, or raises. SigV4 is already verified
        by the router (``context.auth``), so this method begins at staging
        reservation (Requirement 6.3).
        """
        target = context.target
        if target.bucket is None or target.key is None:
            # The router only dispatches object-level PUTs here, so this is a
            # defensive guard rather than an expected branch.
            return error_response(
                code="InvalidRequest",
                message="PutObject requires a bucket and key",
                status_code=400,
                resource=None,
            )

        try:
            return await self._handle(context, target.bucket, target.key)
        except PutObjectError as exc:
            return exc.response

    async def _handle(
        self, context: S3HandlerContext, bucket: str, key: str
    ) -> Response:
        """Run the reserve → stream → verify → stage → commit pipeline."""
        settings = context.settings
        content_length = self._content_length(context)

        handle = await self._reserve(content_length, bucket, key)
        staging_path = self._new_staging_path()
        committed = False
        try:
            digests = await self._stream_to_staging(
                context, staging_path, settings.MAX_OBJECT_SIZE, bucket, key
            )
            self._verify_checksums(context, digests, bucket, key)

            final_path = self._content_addressed_path(digests.sha256)
            self._fsync_and_rename(staging_path, final_path)
            # From here the body is at ``final_path``; the original temp name is
            # gone, so only the final file needs cleanup if a later step fails.
            staging_path = final_path

            response = await self._stage_and_commit(
                context, bucket, key, final_path, content_length, digests
            )
            committed = response.status_code == 200
            return response
        finally:
            # Release the reservation unconditionally; the bytes are either
            # handed off to the durable store or being discarded.
            await self._staging.release(handle)
            if not committed:
                _unlink_quietly(staging_path)

    # -- Step 1: staging reservation (Requirements 6.3, 6.4) ----------------

    def _content_length(self, context: S3HandlerContext) -> int:
        """Return the declared ``Content-Length``, or raise HTTP 400 if absent.

        A PutObject without a declared length cannot be reserved against the
        staging ceiling before the body is read (Requirement 6.3), so it is
        rejected rather than admitted unbounded.
        """
        raw = context.request.headers.get(_HDR_CONTENT_LENGTH)
        if raw is None:
            raise PutObjectError(
                error_response(
                    code="MissingContentLength",
                    message="PutObject requires a Content-Length header",
                    status_code=411,
                    resource=self._resource(context),
                )
            )
        try:
            value = int(raw)
        except ValueError as exc:
            raise PutObjectError(
                error_response(
                    code="InvalidRequest",
                    message="Content-Length is not an integer",
                    status_code=400,
                    resource=self._resource(context),
                )
            ) from exc
        if value < 0:
            raise PutObjectError(
                error_response(
                    code="InvalidRequest",
                    message="Content-Length must be non-negative",
                    status_code=400,
                    resource=self._resource(context),
                )
            )
        return value

    async def _reserve(
        self, content_length: int, bucket: str, key: str
    ) -> StagingHandle:
        """Reserve ``content_length`` bytes of staging, or raise HTTP 507.

        A zero-length body still needs a (trivially satisfiable) handle so the
        release in ``finally`` is symmetric; a refused reservation means the
        staging area is full and the body is never read (Requirement 6.4).
        """
        # ``StagingReservation.reserve`` rejects a non-positive size, but a
        # zero-length object is legal; reserve a single byte so the counter
        # bookkeeping stays balanced without ever admitting unbounded data.
        reservation_size = content_length if content_length > 0 else 1
        handle = await self._staging.reserve(reservation_size)
        if handle is None:
            logger.info(
                "staging full; refusing PutObject %s/%s (%d bytes)",
                bucket,
                key,
                content_length,
            )
            raise PutObjectError(
                error_response(
                    code="InsufficientStorage",
                    message="staging area is full",
                    status_code=507,
                    resource=f"/{bucket}/{key}",
                )
            )
        return handle

    # -- Step 2: stream body + checksum (Requirements 6.5, 6.6) -------------

    async def _stream_to_staging(
        self,
        context: S3HandlerContext,
        staging_path: Path,
        max_object_size: int,
        bucket: str,
        key: str,
    ) -> _BodyDigests:
        """Stream the request body to ``staging_path`` while hashing it.

        Reads the body in :data:`_STREAM_CHUNK_SIZE` slices, updating the MD5,
        SHA-256, and (when a client algorithm is declared) the client checksum as
        bytes arrive, so no second pass over the body is needed (Requirement 6.6).
        As soon as the running total exceeds ``max_object_size`` the file is
        truncated/removed and HTTP 413 is raised with the partial bytes discarded
        (Requirement 6.5).

        Note: the client checksum (CRC/SHA family) is computed over the fully
        received body via :func:`gateway.app.s3.checksums.compute`, so this method
        accumulates the raw bytes needed for that single call lazily — it buffers
        only when a client algorithm is actually declared, keeping the common
        no-checksum path streaming-only.
        """
        algorithm = self._declared_checksum_algorithm(context)

        md5 = hashlib.md5()
        sha256 = hashlib.sha256()
        # Only retain the full body when a client checksum must be computed over
        # it; the CRC family has no incremental API exposed here.
        client_body: bytearray | None = bytearray() if algorithm is not None else None

        total = 0
        self._staging_dir.mkdir(parents=True, exist_ok=True)
        try:
            with open(staging_path, "wb") as handle:
                async for chunk in context.request.stream():
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_object_size:
                        raise PutObjectError(
                            error_response(
                                code="EntityTooLarge",
                                message=(
                                    "object body exceeds the maximum permitted "
                                    "size"
                                ),
                                status_code=413,
                                resource=f"/{bucket}/{key}",
                            )
                        )
                    handle.write(chunk)
                    md5.update(chunk)
                    sha256.update(chunk)
                    if client_body is not None:
                        client_body.extend(chunk)
        except PutObjectError:
            _unlink_quietly(staging_path)
            raise

        client_checksum: str | None = None
        if algorithm is not None and client_body is not None:
            client_checksum = checksums_mod.compute(algorithm, bytes(client_body))

        return _BodyDigests(
            size=total,
            md5_hex=md5.hexdigest(),
            md5_b64=base64.b64encode(md5.digest()).decode("ascii"),
            sha256=sha256.digest(),
            sha256_hex=sha256.hexdigest(),
            client_algorithm=algorithm,
            client_checksum=client_checksum,
        )

    def _declared_checksum_algorithm(
        self, context: S3HandlerContext
    ) -> str | None:
        """Return the normalised client checksum algorithm, or ``None``.

        The algorithm may be named explicitly by ``x-amz-checksum-algorithm`` or
        implied by the presence of a single ``x-amz-checksum-<alg>`` value header.
        An unsupported algorithm raises HTTP 400 before the body is processed
        (Requirement 7.11), matching the design's "Unsupported checksum
        algorithm → 400 InvalidRequest" row.
        """
        headers = context.request.headers
        declared = headers.get(_HDR_CHECKSUM_ALGORITHM)
        if declared is None:
            declared = self._infer_algorithm_from_value_header(headers)
        if declared is None:
            return None
        try:
            # Reuse the checksum module's normalisation + validation.
            checksums_mod.compute(declared, b"")
        except checksums_mod.UnsupportedChecksumAlgorithm as exc:
            raise PutObjectError(
                error_response(
                    code="InvalidRequest",
                    message=f"unsupported checksum algorithm: {exc.algorithm}",
                    status_code=400,
                    resource=self._resource(context),
                )
            ) from exc
        return self._canonical_algorithm(declared)

    @staticmethod
    def _infer_algorithm_from_value_header(
        headers: object,
    ) -> str | None:
        """Infer the algorithm from an ``x-amz-checksum-<alg>`` value header.

        Scans for the first checksum value header (ignoring the algorithm
        selector header) and maps its suffix back to a canonical algorithm name.
        Returns ``None`` when no value header is present.
        """
        items = getattr(headers, "items", None)
        if items is None:
            return None
        suffix_to_alg = {v: k for k, v in _CHECKSUM_HEADER_SUFFIX.items()}
        for name, _ in items():
            lname = name.lower()
            if lname == _HDR_CHECKSUM_ALGORITHM:
                continue
            if lname.startswith(_HDR_CHECKSUM_PREFIX):
                suffix = lname[len(_HDR_CHECKSUM_PREFIX) :]
                if suffix in suffix_to_alg:
                    return suffix_to_alg[suffix]
        return None

    @staticmethod
    def _canonical_algorithm(declared: str) -> str:
        """Normalise a client algorithm name to the checksum module's spelling."""
        canonical = declared.strip().upper()
        if canonical in {"SHA1", "SHA-1"}:
            return "SHA-1"
        if canonical in {"SHA256", "SHA-256"}:
            return "SHA-256"
        return canonical

    # -- Step 3: checksum verification (Requirements 6.7, 6.8) --------------

    def _verify_checksums(
        self,
        context: S3HandlerContext,
        digests: _BodyDigests,
        bucket: str,
        key: str,
    ) -> None:
        """Compare client-supplied integrity values against the computed ones.

        A mismatching ``Content-MD5`` (Requirement 6.7) or a mismatching
        ``x-amz-checksum-<alg>`` value (Requirement 6.8) raises HTTP 400; the
        caller deletes the staging file in its ``finally`` cleanup. Both checks
        compare base64-encoded forms, which is how S3 clients transmit them.
        """
        headers = context.request.headers

        content_md5 = headers.get(_HDR_CONTENT_MD5)
        if content_md5 is not None and content_md5.strip() != digests.md5_b64:
            raise PutObjectError(
                error_response(
                    code="BadDigest",
                    message="the Content-MD5 does not match the received body",
                    status_code=400,
                    resource=f"/{bucket}/{key}",
                )
            )

        if digests.client_algorithm is not None:
            suffix = _CHECKSUM_HEADER_SUFFIX[digests.client_algorithm]
            supplied = headers.get(f"{_HDR_CHECKSUM_PREFIX}{suffix}")
            if (
                supplied is not None
                and digests.client_checksum is not None
                and supplied.strip() != digests.client_checksum
            ):
                raise PutObjectError(
                    error_response(
                        code="BadDigest",
                        message=(
                            f"the {digests.client_algorithm} checksum does not "
                            "match the received body"
                        ),
                        status_code=400,
                        resource=f"/{bucket}/{key}",
                    )
                )

    # -- Step 4: fsync + rename (Requirement 6.9) ---------------------------

    def _fsync_and_rename(self, staging_path: Path, final_path: Path) -> None:
        """Fsync the staged body, rename it into place, and fsync the directory.

        Ordering matters for crash safety (Requirement 6.9): the file's contents
        must be durable *before* the rename, and the rename itself is only
        durable once the containing directory entry is synced. If a file already
        exists at ``final_path`` (identical content from a concurrent in-flight
        write of the same bytes) the rename simply replaces it with byte-identical
        data.
        """
        final_path.parent.mkdir(parents=True, exist_ok=True)
        fsync_file(staging_path)
        os.replace(staging_path, final_path)
        fsync_dir(final_path.parent)

    # -- Step 5: insert STAGED + drive commit (Requirements 6.9–6.15) -------

    async def _stage_and_commit(
        self,
        context: S3HandlerContext,
        bucket: str,
        key: str,
        staging_path: Path,
        declared_length: int,
        digests: _BodyDigests,
    ) -> Response:
        """Insert the STAGED generation and either block for commit or return.

        Resolves the bucket row to obtain its durability mode and id, inserts the
        STAGED generation plus its upload job in one transaction (Requirement
        6.9), then: for an async bucket returns HTTP 200 immediately (Requirement
        6.15); for a sync bucket drives the generation to COMMITTED via the
        injected :class:`CommitDriver` and maps the outcome to 200 / 503 / 500
        (Requirements 6.13, 6.14).
        """
        etag = f'"{digests.md5_hex}"'
        checksums = self._build_checksum_map(digests)
        content_type = context.request.headers.get(_HDR_CONTENT_TYPE)
        user_meta = self._collect_user_meta(context)

        async with self._session_factory() as session, session.begin():
            # Resolve the bucket inside the transaction so the durability read
            # and the STAGED generation + upload-job insert share one unit of
            # work (Requirement 6.9). Running the SELECT before ``begin()`` would
            # autobegin the session's transaction and make the explicit
            # ``begin()`` raise "transaction already begun".
            bucket_row = await self._resolve_bucket(session, bucket, context)
            durability = self._durability_for(bucket_row, context.settings)

            generation_id = await generations_storage.insert_staged(
                session,
                bucket_id=bucket_row.id,
                key=key,
                kind="put",
                size=digests.size,
                staging_path=os.fspath(staging_path),
                content_type=content_type,
                user_meta=user_meta or None,
                last_modified=_naive_utcnow(),
            )
            # Record the computed digests on the generation so the Upload
            # Worker can resolve the dedup SHA-256 and echo checksums in the
            # commit record (see upload_worker._resolve_sha256).
            await session.execute(
                _generation_metadata_update(generation_id, etag, checksums)
            )

        if durability == DURABILITY_ASYNC:
            # Requirement 6.15: durable-enough at STAGED; the worker commits
            # asynchronously. Return success without waiting for Telegram.
            return _success_response(etag, digests, async_staged=True)

        return await self._await_commit(generation_id, etag, digests, bucket, key)

    async def _await_commit(
        self,
        generation_id: uuid.UUID,
        etag: str,
        digests: _BodyDigests,
        bucket: str,
        key: str,
    ) -> Response:
        """Drive a sync generation to its terminal state and map it to a response.

        A COMMITTED outcome returns HTTP 200 (Requirement 6.13). A timeout past
        the 30 s Telegram-unreachable boundary returns HTTP 503 with the STAGED
        generation retained for retry (Requirement 6.14). Any other terminal
        state (notably FAILED after exhausted attempts) returns HTTP 500.
        """
        outcome = await self._commit_driver.drive_to_commit(generation_id)
        if outcome.committed:
            return _success_response(etag, digests, async_staged=False)
        if outcome.timed_out:
            raise PutObjectError(
                error_response(
                    code="ServiceUnavailable",
                    message="Telegram was unreachable; the object is staged for retry",
                    status_code=503,
                    resource=f"/{bucket}/{key}",
                )
            )
        logger.error(
            "PutObject %s/%s did not commit (generation %s state=%s)",
            bucket,
            key,
            generation_id,
            outcome.state,
        )
        raise PutObjectError(
            error_response(
                code="InternalError",
                message="the object could not be committed to durable storage",
                status_code=500,
                resource=f"/{bucket}/{key}",
            )
        )

    # -- helpers ------------------------------------------------------------

    async def _resolve_bucket(
        self, session: AsyncSession, bucket: str, context: S3HandlerContext
    ) -> Bucket:
        """Load the bucket row, raising HTTP 404 ``NoSuchBucket`` if absent."""
        row = (
            await session.execute(select(Bucket).where(Bucket.name == bucket))
        ).scalar_one_or_none()
        if row is None:
            raise PutObjectError(
                error_response(
                    code="NoSuchBucket",
                    message="the specified bucket does not exist",
                    status_code=404,
                    resource=self._resource(context),
                )
            )
        return row

    @staticmethod
    def _durability_for(bucket_row: Bucket, settings: object) -> str:
        """Resolve the effective durability mode for a bucket.

        Uses the bucket's own ``durability`` column when it names a known mode,
        otherwise falls back to the gateway default ``DEFAULT_DURABILITY``.
        """
        value = (bucket_row.durability or "").strip().lower()
        if value in (DURABILITY_SYNC, DURABILITY_ASYNC):
            return value
        default = getattr(settings, "DEFAULT_DURABILITY", DURABILITY_SYNC)
        return default if default in (DURABILITY_SYNC, DURABILITY_ASYNC) else DURABILITY_SYNC

    @staticmethod
    def _build_checksum_map(digests: _BodyDigests) -> dict[str, str]:
        """Assemble the checksum map recorded on the generation.

        Always records the hex SHA-256 (the dedup key the Upload Worker reads)
        and, when the client declared one, the base64 client checksum under its
        algorithm name.
        """
        checksums: dict[str, str] = {"sha256": digests.sha256_hex}
        if digests.client_algorithm is not None and digests.client_checksum is not None:
            checksums[digests.client_algorithm] = digests.client_checksum
        return checksums

    @staticmethod
    def _collect_user_meta(context: S3HandlerContext) -> dict[str, str]:
        """Collect ``x-amz-meta-*`` headers into a plain string map."""
        meta: dict[str, str] = {}
        for name, value in context.request.headers.items():
            lname = name.lower()
            if lname.startswith(_HDR_META_PREFIX):
                meta[lname[len(_HDR_META_PREFIX) :]] = value
        return meta

    def _new_staging_path(self) -> Path:
        """Return a unique temp path for the incoming body inside the staging dir."""
        return self._staging_dir / f"incoming-{uuid.uuid4().hex}"

    def _content_addressed_path(self, sha256: bytes) -> Path:
        """Return the content-addressed staging path for a body's digest.

        Uses a two-level fan-out (``{sha[:2]}/{sha_hex}``) to keep any single
        directory from accumulating an unbounded number of entries.
        """
        digest = sha256.hex()
        return self._staging_dir / digest[:2] / digest

    @staticmethod
    def _resource(context: S3HandlerContext) -> str | None:
        """Format the error-document ``Resource`` value for the request target."""
        target = context.target
        if target.bucket is None:
            return None
        if target.key is None:
            return f"/{target.bucket}"
        return f"/{target.bucket}/{target.key}"


@dataclass(frozen=True)
class _BodyDigests:
    """The integrity values computed while streaming a PutObject body.

    Attributes:
        size: The received body size in bytes.
        md5_hex: Lower-case hex MD5 (the ETag payload for a single PUT).
        md5_b64: Base64 MD5, for comparison against a client ``Content-MD5``.
        sha256: Raw 32-byte SHA-256 digest (the content-address / dedup key).
        sha256_hex: Hex SHA-256 recorded on the generation for the Upload Worker.
        client_algorithm: The normalised client checksum algorithm, if declared.
        client_checksum: The base64 client checksum computed over the body, if a
            client algorithm was declared.
    """

    size: int
    md5_hex: str
    md5_b64: str
    sha256: bytes
    sha256_hex: str
    client_algorithm: str | None
    client_checksum: str | None


def _naive_utcnow() -> datetime:
    """Return the current UTC time as a naive datetime.

    The ``generations.last_modified`` column is ``TIMESTAMP WITHOUT TIME ZONE``
    (see the data model), and asyncpg rejects a timezone-aware value for it with
    "can't subtract offset-naive and offset-aware datetimes". The Upload Worker
    carries this same value into the Commit Record and the COMMITTED advance and
    works in naive UTC throughout for the identical reason, so the write path
    stores a naive UTC timestamp here to stay byte-for-byte comparable with what
    the column holds.
    """
    return datetime.now(UTC).replace(tzinfo=None)


def _generation_metadata_update(
    generation_id: uuid.UUID, etag: str, checksums: dict[str, str]
) -> Update:
    """Build the UPDATE that records the ETag and checksum map on a generation.

    Kept as a small factory so the handler body reads as prose; the Upload
    Worker later reads the ``sha256`` entry of the checksum map as the dedup key.
    """
    return (
        update(Generation)
        .where(Generation.id == generation_id)
        .values(etag=etag, checksums=checksums)
    )


def _success_response(
    etag: str, digests: _BodyDigests, *, async_staged: bool
) -> Response:
    """Build the HTTP 200 PutObject response with the ETag and checksum headers.

    S3 returns an empty body on a successful PutObject, with the ETag in a header
    and — when the client requested one — the checksum echoed back. ``async_staged``
    only affects an advisory log/semantic note; the status code is 200 either way
    (Requirements 6.13, 6.15).
    """
    headers: dict[str, str] = {"ETag": etag}
    if digests.client_algorithm is not None and digests.client_checksum is not None:
        suffix = _CHECKSUM_HEADER_SUFFIX[digests.client_algorithm]
        headers[f"x-amz-checksum-{suffix}"] = digests.client_checksum
    return Response(status_code=200, headers=headers)


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

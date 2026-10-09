"""HeadObject — object existence and metadata probe (Requirements 9.1, 9.2).

``HEAD /{bucket}/{key}`` lets an S3 client check whether an object exists and
read its metadata without transferring the body. The response carries headers
only, never a body, in both the found and not-found cases:

* **Requirement 9.1** — when the key resolves to a live object, return HTTP 200
  with an empty body and the metadata headers ``Content-Length`` (the object
  size in bytes), ``Content-Type``, ``ETag`` and ``Last-Modified``.
* **Requirement 9.2** — when the key does not resolve, return HTTP 404 with an
  empty body. HeadObject is defined to carry no body in either direction, so the
  404 is a bare status with no S3 error XML document (unlike GET, which renders
  the ``NoSuchKey`` document).

Visibility follows the read-path contract in the design ("resolve head"): the
object a HeadObject sees is whatever generation the key's ``object_heads``
pointer currently names, and that generation is only observable once it reaches
``COMMITTED`` — durability lives in Telegram and a generation is durable only at
``COMMITTED`` (design "Generation State Machine"). A key whose head points at a
generation that has since moved to a non-``COMMITTED`` state (for example a
tombstone's ``DELETED`` or a superseded row that has not yet had its head moved)
is treated as absent, so HeadObject never reports a non-live object as present.

This handler performs only index reads; it never touches Telegram, so it is
unaffected by the Telegram-reconnection 503 path. It is a class holding the
database :class:`~gateway.app.db.session.AsyncSessionFactory` (the same
dependency-injection shape used by the cache and worker components) and exposes
the :class:`~gateway.app.s3.router.S3Handler` async-callable protocol so it plugs
straight into the router's handler registry. SigV4 authentication has already
run in the router before this handler is invoked (requirement 6.1).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from email.utils import format_datetime

from fastapi import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.app.db.models import Bucket, Generation
from gateway.app.db.session import AsyncSessionFactory
from gateway.app.s3.router import S3HandlerContext
from gateway.app.storage import heads
from gateway.app.storage.generations import STATE_COMMITTED

__all__ = ["HeadObjectHandler", "DEFAULT_CONTENT_TYPE"]

#: Fallback ``Content-Type`` when a committed generation recorded none. Mirrors
#: the Upload Worker's commit-record default (``application/octet-stream``) so a
#: HeadObject and a later GetObject agree on the media type for the same object.
DEFAULT_CONTENT_TYPE = "application/octet-stream"


class HeadObjectHandler:
    """Serve ``HEAD /{bucket}/{key}`` from the PostgreSQL index.

    One instance is registered against
    :attr:`gateway.app.s3.router.S3Operation.HEAD_OBJECT`. It resolves the
    target key's committed generation and returns a header-only response
    (Requirements 9.1, 9.2); it owns the short read transaction via the injected
    session factory and performs no Telegram I/O.
    """

    def __init__(self, session_factory: AsyncSessionFactory) -> None:
        """Store the session factory used to open per-request read sessions.

        Args:
            session_factory: Opens short-lived :class:`AsyncSession` instances
                for the index reads. The handler owns the transaction for each
                probe, consistent with the "config is centralized / inject
                dependencies" conventions used elsewhere in the gateway.
        """
        self._session_factory = session_factory

    async def __call__(self, context: S3HandlerContext) -> Response:
        """Resolve the target object and return its metadata headers.

        Returns HTTP 200 with ``Content-Length``, ``Content-Type``, ``ETag`` and
        ``Last-Modified`` headers and an empty body when the key names a live
        (``COMMITTED``) object (requirement 9.1); otherwise HTTP 404 with an
        empty body (requirement 9.2). The target bucket and key are taken from
        the router-resolved :class:`~gateway.app.s3.router.ResolvedTarget`; a
        request missing either (which the router's object-level classification
        does not produce for HeadObject) is treated as not found.

        Args:
            context: The router-populated handler context carrying the resolved
                target, the authenticated identity and the settings.

        Returns:
            A header-only :class:`fastapi.Response`.
        """
        bucket_name = context.target.bucket
        key = context.target.key
        if bucket_name is None or key is None:
            return self._not_found()

        async with self._session_factory.session() as session:
            generation = await self._resolve_committed(session, bucket_name, key)
            if generation is None:
                return self._not_found()
            return self._found(generation)

    async def _resolve_committed(
        self, session: AsyncSession, bucket_name: str, key: str
    ) -> Generation | None:
        """Return the key's committed generation, or ``None`` if not live.

        Resolves the bucket name to its id, follows the ``object_heads`` pointer
        to the current generation, and loads that generation only if it is in
        the ``COMMITTED`` state. Any missing link in that chain — unknown bucket,
        no head row, a dangling head, or a head pointing at a non-``COMMITTED``
        generation — yields ``None`` so the caller reports the object as absent
        (the read-path "resolve head" contract; durability is observable only at
        ``COMMITTED``).
        """
        bucket_id = await self._bucket_id(session, bucket_name)
        if bucket_id is None:
            return None

        generation_id = await heads.get_head(session, bucket_id, key)
        if generation_id is None:
            return None

        generation = await session.get(Generation, generation_id)
        if generation is None or generation.state != STATE_COMMITTED:
            return None
        return generation

    async def _bucket_id(
        self, session: AsyncSession, bucket_name: str
    ) -> uuid.UUID | None:
        """Look up a bucket's id by name, or ``None`` when it does not exist."""
        result = await session.execute(
            select(Bucket.id).where(Bucket.name == bucket_name)
        )
        return result.scalar_one_or_none()

    def _found(self, generation: Generation) -> Response:
        """Build the 200 header-only response for a live object (req 9.1)."""
        headers = {
            "Content-Length": str(generation.size),
            "Content-Type": generation.content_type or DEFAULT_CONTENT_TYPE,
            "Last-Modified": _http_date(generation.last_modified),
        }
        etag = _format_etag(generation.etag)
        if etag is not None:
            headers["ETag"] = etag
        # Empty body: HeadObject never returns a payload (requirement 9.1).
        return Response(status_code=200, headers=headers)

    def _not_found(self) -> Response:
        """Build the 404 bare response for an absent object (req 9.2).

        HeadObject returns no body in the not-found case, so this is a bare
        status with an empty body rather than an S3 error XML document.
        """
        return Response(status_code=404)


def _format_etag(etag: str | None) -> str | None:
    """Return the ETag wrapped in the double quotes S3 clients expect.

    The index stores the raw ETag string (the Upload Worker records the value
    computed by PutObject). S3 presents the ETag header as a quoted string; this
    adds the surrounding quotes idempotently so an already-quoted stored value is
    not double-wrapped. ``None`` (a generation with no recorded ETag) is passed
    through so the header is simply omitted.
    """
    if etag is None:
        return None
    if len(etag) >= 2 and etag.startswith('"') and etag.endswith('"'):
        return etag
    return f'"{etag}"'


def _http_date(value: object) -> str:
    """Render a ``Last-Modified`` value as an RFC 7231 IMF-fixdate string.

    ``Generation.last_modified`` is a timezone-aware UTC ``datetime`` (the
    generations helper stores ``datetime.now(UTC)``). :func:`email.utils.
    format_datetime` with ``usegmt=True`` emits the ``Sun, 06 Nov 1994 08:49:37
    GMT`` form S3 and HTTP use. A naive datetime is assumed to be UTC so the
    header is still well-formed; anything that is not a ``datetime`` falls back
    to its string form rather than raising inside the response path.
    """
    if not isinstance(value, datetime):
        return str(value)
    dt = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return format_datetime(dt, usegmt=True)

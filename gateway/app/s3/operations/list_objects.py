"""ListObjectsV2 and ListObjects (v1) — object listing (Requirement 5).

``GET /{bucket}`` lists the objects of a bucket. S3 exposes two dialects of the
same operation and the router classifies them separately:

* **ListObjectsV2** (``?list-type=2``) — paginates with an opaque
  ``continuation-token`` and reports ``KeyCount`` / ``NextContinuationToken``.
* **ListObjects v1** — paginates with a ``marker`` and reports ``NextMarker``.

Both share one listing engine (:class:`_Listing`) and differ only in how the
pagination cursor is spelled on the wire and in which XML body is emitted. The
engine implements the behaviour Requirement 5 pins down:

* **Requirement 5.1** — accept ``prefix``, ``delimiter``, ``max-keys`` and the
  cursor parameter; an absent ``max-keys`` defaults to 1000, and a value outside
  the inclusive range 1–1000 is rejected with ``InvalidArgument``.
* **Requirement 5.2** — with a ``delimiter``, keys that share a common prefix up
  to and including the first delimiter *beyond* ``prefix`` collapse into a single
  ``CommonPrefixes`` entry; both ``Contents`` and ``CommonPrefixes`` are returned
  in lexicographic order and no key appears in both.
* **Requirement 5.3** — when more keys match than fit in ``max-keys`` the
  response is truncated and carries a cursor (``NextContinuationToken`` for v2,
  ``NextMarker`` for v1) that resumes at the immediately following lexicographic
  page.
* **Requirement 5.4** — only generations in state ``COMMITTED`` are listed; a key
  whose head points at a non-committed generation is invisible.
* **Requirement 5.5** — a continuation token that is malformed or names an
  unknown pagination state is rejected with an invalid-argument error and *no*
  partial results are returned.

The listing reads the PostgreSQL index only (``object_heads`` joined to its
``COMMITTED`` ``generations``); it never touches Telegram. Like the sibling
object handlers it is a class holding the
:class:`~gateway.app.db.session.AsyncSessionFactory` and conforms to the
:class:`~gateway.app.s3.router.S3Handler` async-callable protocol so it plugs
straight into the router registry. SigV4 authentication has already run in the
router before the handler is invoked (requirement 6.1).
"""

from __future__ import annotations

import base64
import binascii
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from fastapi import Response
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.app.db.models import Bucket, Generation, ObjectHead
from gateway.app.db.session import AsyncSessionFactory
from gateway.app.s3 import xml_codecs
from gateway.app.s3.router import (
    ResolvedTarget,
    S3HandlerContext,
    S3Operation,
    error_response,
)
from gateway.app.storage.generations import STATE_COMMITTED

__all__ = [
    "ListObjectsHandler",
    "DEFAULT_MAX_KEYS",
    "MAX_MAX_KEYS",
    "MIN_MAX_KEYS",
]

#: Default ``max-keys`` when the client omits it (requirement 5.1).
DEFAULT_MAX_KEYS = 1000
#: Lower bound of the accepted ``max-keys`` range, inclusive (requirement 5.1).
MIN_MAX_KEYS = 1
#: Upper bound of the accepted ``max-keys`` range, inclusive (requirement 5.1).
MAX_MAX_KEYS = 1000

#: Prefix stamped into every v2 continuation token. A supplied token missing
#: this marker is treated as unknown/invalid (requirement 5.5), which both
#: rejects client-fabricated tokens and lets us evolve the encoding later.
_TOKEN_MAGIC = b"v2:"

#: Fallback ``Content-Type``/ETag defaults mirror the HeadObject handler so a
#: listing entry and a later HEAD/GET agree on the object's metadata.
_DEFAULT_ETAG = ""


class _InvalidToken(Exception):
    """Raised when a continuation token cannot be decoded (requirement 5.5)."""


class _InvalidMaxKeys(Exception):
    """Raised when ``max-keys`` is non-integer or out of range (requirement 5.1)."""


@dataclass(frozen=True)
class _Listing:
    """One fully-resolved listing request, independent of the v1/v2 dialect.

    Produced by parsing the query string once; the shared engine consumes it so
    the two dialect handlers differ only in cursor spelling and response body.

    Attributes:
        prefix: The key prefix filter (empty string lists the whole bucket).
        delimiter: The grouping delimiter, or ``None`` for a flat listing.
        max_keys: The validated page size, in ``[MIN_MAX_KEYS, MAX_MAX_KEYS]``.
        start_after: The exclusive lower bound on keys to return — the decoded
            cursor (continuation token or marker) combined with v2 ``start-after``.
            ``None`` means start at the beginning of the prefix range.
    """

    prefix: str
    delimiter: str | None
    max_keys: int
    start_after: str | None


@dataclass
class _Page:
    """The accumulated result of walking the sorted key stream for one page.

    Attributes:
        contents: Live object entries, in lexicographic key order.
        common_prefixes: Rolled-up delimiter groups, in lexicographic order.
        is_truncated: Whether more entries exist beyond this page.
        next_cursor: The key to resume *after* on the next request when
            truncated; ``None`` on the final page.
    """

    contents: list[xml_codecs.ObjectEntry] = field(default_factory=list)
    common_prefixes: list[xml_codecs.CommonPrefix] = field(default_factory=list)
    is_truncated: bool = False
    next_cursor: str | None = None

    @property
    def entry_count(self) -> int:
        """Number of emitted entries (Contents + CommonPrefixes) so far."""
        return len(self.contents) + len(self.common_prefixes)


class ListObjectsHandler:
    """Serve ListObjectsV2 and ListObjects v1 from the PostgreSQL index.

    One instance is registered against both
    :attr:`gateway.app.s3.router.S3Operation.LIST_OBJECTS_V2` and
    :attr:`~gateway.app.s3.router.S3Operation.LIST_OBJECTS_V1`; the handler reads
    ``context.operation`` to pick the dialect. It owns a short read transaction
    per request via the injected session factory and performs no Telegram I/O
    (requirement 5.4 is enforced purely against the index).
    """

    def __init__(self, session_factory: AsyncSessionFactory) -> None:
        """Store the session factory used to open per-request read sessions.

        Args:
            session_factory: Opens short-lived :class:`AsyncSession` instances
                for the index reads, consistent with the dependency-injection
                shape used by the other object handlers.
        """
        self._session_factory = session_factory

    async def __call__(self, context: S3HandlerContext) -> Response:
        """Resolve the listing and return the matching S3 XML body.

        Parses and validates the query parameters (requirement 5.1), rejecting a
        bad ``max-keys`` or an undecodable v2 continuation token *before* any
        index read so no partial results are produced (requirements 5.1, 5.5).
        The bucket is resolved to its id; an unknown bucket yields
        ``NoSuchBucket``. The shared engine then walks the committed keys in
        lexicographic order and the dialect-specific serialiser renders the body.

        Args:
            context: The router-populated handler context carrying the resolved
                target, the classified operation and the authenticated identity.

        Returns:
            A ready-to-send XML :class:`fastapi.Response`.
        """
        target = context.target
        bucket_name = target.bucket
        if bucket_name is None:
            # The router only dispatches bucket-level GETs here; guard defensively.
            return error_response(
                code="InvalidRequest",
                message="ListObjects requires a bucket",
                status_code=400,
                resource=_resource(target),
            )

        is_v2 = context.operation is S3Operation.LIST_OBJECTS_V2
        query = dict(context.request.query_params)

        try:
            listing = self._parse(query, is_v2=is_v2)
        except _InvalidMaxKeys as exc:
            return error_response(
                code="InvalidArgument",
                message=str(exc),
                status_code=400,
                resource=_resource(target),
            )
        except _InvalidToken as exc:
            # Requirement 5.5: an invalid/unknown token is rejected outright and
            # must not yield partial results — we fail before touching the index.
            return error_response(
                code="InvalidArgument",
                message=str(exc),
                status_code=400,
                resource=_resource(target),
            )

        async with self._session_factory.session() as session:
            bucket_id = await self._bucket_id(session, bucket_name)
            if bucket_id is None:
                return error_response(
                    code="NoSuchBucket",
                    message="the specified bucket does not exist",
                    status_code=404,
                    resource=_resource(target),
                )
            page = await self._collect(session, bucket_id, listing)

        if is_v2:
            return self._render_v2(bucket_name, query, listing, page)
        return self._render_v1(bucket_name, query, listing, page)

    # -- parsing -----------------------------------------------------------

    def _parse(self, query: dict[str, str], *, is_v2: bool) -> _Listing:
        """Parse and validate the query string into a :class:`_Listing`.

        Applies the ``max-keys`` default and range check (requirement 5.1) and
        decodes the dialect's pagination cursor into an exclusive ``start_after``
        key (requirement 5.3). For v2 the cursor is the opaque
        ``continuation-token``; a ``start-after`` is honoured only on the first
        page (when no token is present), matching S3. For v1 the cursor is the
        plain ``marker`` (an ordinary key, not an opaque token).

        Raises:
            _InvalidMaxKeys: If ``max-keys`` is non-integer or out of range.
            _InvalidToken: If a v2 ``continuation-token`` cannot be decoded.
        """
        prefix = query.get("prefix", "")
        delimiter = query.get("delimiter") or None
        max_keys = _parse_max_keys(query.get("max-keys"))

        start_after: str | None
        if is_v2:
            token = query.get("continuation-token")
            if token is not None:
                start_after = _decode_token(token)
            else:
                start_after = query.get("start-after") or None
        else:
            start_after = query.get("marker") or None

        return _Listing(
            prefix=prefix,
            delimiter=delimiter,
            max_keys=max_keys,
            start_after=start_after,
        )

    # -- querying ----------------------------------------------------------

    async def _bucket_id(
        self, session: AsyncSession, bucket_name: str
    ) -> uuid.UUID | None:
        """Look up a bucket's id by name, or ``None`` when it does not exist."""
        result = await session.execute(
            select(Bucket.id).where(Bucket.name == bucket_name)
        )
        return result.scalar_one_or_none()

    async def _collect(
        self, session: AsyncSession, bucket_id: uuid.UUID, listing: _Listing
    ) -> _Page:
        """Walk the committed keys in order and build one page of results.

        Streams ``object_heads`` joined to their ``COMMITTED`` generations in
        ascending key order (requirement 5.4), restricted to the ``prefix`` range
        and to keys strictly greater than ``start_after`` (requirement 5.3). Each
        row is routed either into ``Contents`` or folded into a ``CommonPrefixes``
        group (requirement 5.2); a key already represented by an emitted common
        prefix is skipped, so no key appears in both collections. Accumulation
        stops once ``max_keys`` entries have been emitted; if any committed key
        remains after that point the page is marked truncated and the resume
        cursor is recorded (requirement 5.3).

        Because the database returns rows already sorted by key, both ``Contents``
        and ``CommonPrefixes`` come out in lexicographic order without an extra
        sort.
        """
        page = _Page()
        current_prefix: str | None = None  # the common prefix most recently rolled up

        result = await session.stream(self._query(bucket_id, listing))
        try:
            async for row in result:
                key: str = row.key
                generation: Generation = row.Generation

                group = _delimiter_group(key, listing.prefix, listing.delimiter)
                if group is not None:
                    if group == current_prefix:
                        # Same roll-up group as the last emitted CommonPrefix: the
                        # key is already represented, so it is neither counted nor
                        # repeated (requirement 5.2, disjointness).
                        continue
                    if page.entry_count >= listing.max_keys:
                        page.is_truncated = True
                        page.next_cursor = _cursor_for(page)
                        break
                    current_prefix = group
                    page.common_prefixes.append(xml_codecs.CommonPrefix(prefix=group))
                    continue

                if page.entry_count >= listing.max_keys:
                    page.is_truncated = True
                    page.next_cursor = _cursor_for(page)
                    break
                page.contents.append(_object_entry(key, generation))
        finally:
            await result.close()

        return page

    def _query(
        self, bucket_id: uuid.UUID, listing: _Listing
    ) -> Select[str, Generation]:
        """Build the ordered committed-key query for a listing.

        Joins ``object_heads`` to the generation it names, keeps only
        ``COMMITTED`` rows (requirement 5.4), applies the prefix range and the
        exclusive ``start_after`` lower bound, and orders by key ascending so the
        caller can group and paginate in a single forward pass.
        """
        stmt = (
            select(ObjectHead.key, Generation)
            .join(Generation, ObjectHead.generation_id == Generation.id)
            .where(
                ObjectHead.bucket_id == bucket_id,
                Generation.state == STATE_COMMITTED,
            )
            .order_by(ObjectHead.key.asc())
        )
        if listing.prefix:
            stmt = stmt.where(ObjectHead.key.startswith(listing.prefix, autoescape=True))
        if listing.start_after is not None:
            stmt = stmt.where(ObjectHead.key > listing.start_after)
        return stmt

    # -- rendering ---------------------------------------------------------

    def _render_v2(
        self,
        bucket_name: str,
        query: dict[str, str],
        listing: _Listing,
        page: _Page,
    ) -> Response:
        """Serialise a ListObjectsV2 response body (requirements 5.2, 5.3)."""
        next_token = (
            _encode_token(page.next_cursor)
            if page.is_truncated and page.next_cursor is not None
            else None
        )
        response = xml_codecs.ListObjectsV2Response(
            name=bucket_name,
            prefix=listing.prefix,
            key_count=page.entry_count,
            max_keys=listing.max_keys,
            is_truncated=page.is_truncated,
            delimiter=listing.delimiter,
            continuation_token=query.get("continuation-token"),
            next_continuation_token=next_token,
            start_after=query.get("start-after") or None,
            contents=tuple(page.contents),
            common_prefixes=tuple(page.common_prefixes),
        )
        return _xml_response(xml_codecs.serialise_list_objects_v2(response))

    def _render_v1(
        self,
        bucket_name: str,
        query: dict[str, str],
        listing: _Listing,
        page: _Page,
    ) -> Response:
        """Serialise a ListObjects v1 response body (requirements 5.2, 5.3)."""
        next_marker = (
            page.next_cursor if page.is_truncated and page.next_cursor is not None else None
        )
        response = xml_codecs.ListObjectsResponse(
            name=bucket_name,
            prefix=listing.prefix,
            marker=query.get("marker", ""),
            max_keys=listing.max_keys,
            is_truncated=page.is_truncated,
            delimiter=listing.delimiter,
            next_marker=next_marker,
            contents=tuple(page.contents),
            common_prefixes=tuple(page.common_prefixes),
        )
        return _xml_response(xml_codecs.serialise_list_objects(response))


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _parse_max_keys(raw: str | None) -> int:
    """Validate the ``max-keys`` parameter per requirement 5.1.

    An absent value defaults to :data:`DEFAULT_MAX_KEYS`. A present value must be
    an integer within ``[MIN_MAX_KEYS, MAX_MAX_KEYS]``; anything else raises
    :class:`_InvalidMaxKeys`, which the handler maps to ``InvalidArgument``.
    """
    if raw is None or raw == "":
        return DEFAULT_MAX_KEYS
    try:
        value = int(raw)
    except ValueError as exc:
        raise _InvalidMaxKeys(f"max-keys must be an integer, got {raw!r}") from exc
    if value < MIN_MAX_KEYS or value > MAX_MAX_KEYS:
        raise _InvalidMaxKeys(
            f"max-keys must be between {MIN_MAX_KEYS} and {MAX_MAX_KEYS}, got {value}"
        )
    return value


def _delimiter_group(key: str, prefix: str, delimiter: str | None) -> str | None:
    """Return the ``CommonPrefixes`` group a key rolls into, or ``None``.

    Implements requirement 5.2: the group is the substring of ``key`` from its
    start up to and including the first ``delimiter`` that occurs *at or after*
    the end of ``prefix``. A key with no such delimiter (or when no delimiter was
    requested) belongs in ``Contents`` and yields ``None``. ``key`` is assumed to
    already start with ``prefix`` (the query enforces the prefix filter).
    """
    if not delimiter:
        return None
    index = key.find(delimiter, len(prefix))
    if index == -1:
        return None
    return key[: index + len(delimiter)]


def _cursor_for(page: _Page) -> str:
    """Return the resume key for the next page: the last entry emitted so far.

    The next request resumes *after* this key. The last emitted entry is either
    the final ``Contents`` key or the final ``CommonPrefixes`` value, whichever
    sorts higher — and since entries are emitted in lexicographic order it is
    simply the greater of the two tails. The page always has at least one entry
    when this is called (truncation only happens after ``max_keys >= 1`` entries).
    """
    last_content = page.contents[-1].key if page.contents else ""
    last_prefix = page.common_prefixes[-1].prefix if page.common_prefixes else ""
    return max(last_content, last_prefix)


def _object_entry(key: str, generation: Generation) -> xml_codecs.ObjectEntry:
    """Build a ``Contents`` entry from a committed generation row."""
    return xml_codecs.ObjectEntry(
        key=key,
        last_modified=_as_utc(generation.last_modified),
        etag=_format_etag(generation.etag),
        size=generation.size,
    )


def _as_utc(value: datetime) -> datetime:
    """Coerce a stored timestamp to timezone-aware UTC for the serialiser."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _format_etag(etag: str | None) -> str:
    """Return the ETag wrapped in the double quotes S3 clients expect.

    Mirrors the HeadObject handler: the index stores the raw ETag; S3 presents
    it quoted. Quoting is idempotent so an already-quoted stored value is not
    double-wrapped, and a generation with no recorded ETag yields a bare pair of
    quotes rather than omitting the element the listing schema requires.
    """
    if etag is None:
        return '""'
    if len(etag) >= 2 and etag.startswith('"') and etag.endswith('"'):
        return etag
    return f'"{etag}"'


def _encode_token(resume_key: str) -> str:
    """Encode a resume key into an opaque, self-identifying v2 token.

    The token is ``base64url(_TOKEN_MAGIC + utf8(resume_key))``. Stamping the
    magic marker lets :func:`_decode_token` reject tokens this gateway did not
    mint (requirement 5.5) while keeping the body opaque to clients.
    """
    payload = _TOKEN_MAGIC + resume_key.encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii")


def _decode_token(token: str) -> str:
    """Decode a v2 continuation token back into its resume key.

    Raises:
        _InvalidToken: If the token is not valid base64url or does not carry the
            :data:`_TOKEN_MAGIC` marker — i.e. it is malformed or was not minted
            by this gateway, which requirement 5.5 treats as invalid/unknown.
    """
    try:
        payload = base64.urlsafe_b64decode(_add_b64_padding(token))
    except (binascii.Error, UnicodeEncodeError, ValueError) as exc:
        raise _InvalidToken("the continuation token is not valid") from exc
    if not payload.startswith(_TOKEN_MAGIC):
        raise _InvalidToken("the continuation token is not recognised")
    try:
        return payload[len(_TOKEN_MAGIC) :].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _InvalidToken("the continuation token is corrupt") from exc


def _add_b64_padding(token: str) -> bytes:
    """Restore any stripped ``=`` padding so base64url decoding succeeds.

    Clients (and some intermediaries) may drop trailing ``=`` padding when
    echoing a token; base64 requires the length to be a multiple of four.
    """
    ascii_token = token.encode("ascii", errors="strict")
    pad = (-len(ascii_token)) % 4
    return ascii_token + (b"=" * pad)


def _xml_response(body: bytes) -> Response:
    """Wrap a serialised XML body in a 200 ``application/xml`` response."""
    return Response(content=body, status_code=200, media_type="application/xml")


def _resource(target: ResolvedTarget) -> str | None:
    """Format the error-document ``Resource`` value for a listing target."""
    if target.bucket is None:
        return None
    return f"/{target.bucket}"

"""Typed serialisers/deserialisers for S3 XML response and request bodies.

The S3 wire protocol frames most non-object payloads as XML in the
``http://s3.amazonaws.com/doc/2006-03-01/`` namespace. This module is the single
place where those bodies are produced and parsed, so the router, bucket and
multipart handlers deal in typed value objects rather than raw element trees.

Design conventions honoured here:

* **No string formatting for XML.** Every document is built with
  :mod:`xml.etree.ElementTree` and serialised via :func:`ElementTree.tostring`,
  which escapes text and attribute values. ``lxml`` is deliberately *not* a
  dependency (it is absent from the pinned ``pyproject.toml``), so the standard
  library is used throughout.
* **Typed value objects at boundaries.** Each request/response shape is a frozen
  :class:`dataclasses.dataclass`, matching the project's immutable boundary-type
  convention. Serialisers take one of these objects and return ``bytes``;
  deserialisers take ``bytes`` and return one.

Covered shapes and the acceptance criteria they back:

* ``ListBucketsResponse`` / ``GetBucketLocationResponse`` — bucket operations
  (requirement 4.1; GetBucketLocation returns the configured region per 4.5).
* ``ListObjectsV2Response`` / ``ListObjectsResponse`` (v1) — object listing with
  ``Contents``, ``CommonPrefixes`` and truncation tokens
  (requirements 5.1, 5.2, 5.3).
* ``InitiateMultipartUploadResponse``, ``CompleteMultipartUploadResponse``,
  ``ListPartsResponse``, ``ListMultipartUploadsResponse`` — multipart upload
  (requirements 10.1, 10.7, 10.8, 10.9).
* ``DeleteObjectsRequest`` / ``DeleteObjectsResponse`` — batch delete
  (requirement 9.6; parsing the request body and framing per-key results).
* ``Error`` — the standard S3 error document returned for every failure.

All byte output is XML-declared, UTF-8 encoded, and uses the S3 namespace as the
default namespace on the document root.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from xml.etree import ElementTree as ET

__all__ = [
    "S3_NAMESPACE",
    "Bucket",
    "ListBucketsResponse",
    "GetBucketLocationResponse",
    "ObjectEntry",
    "CommonPrefix",
    "ListObjectsV2Response",
    "ListObjectsResponse",
    "InitiateMultipartUploadResponse",
    "CompleteMultipartUploadResponse",
    "Part",
    "ListPartsResponse",
    "MultipartUpload",
    "ListMultipartUploadsResponse",
    "DeleteObjectRequestEntry",
    "DeleteObjectsRequest",
    "DeletedObject",
    "DeleteError",
    "DeleteObjectsResponse",
    "CompletePart",
    "Error",
    "serialise_list_buckets",
    "serialise_get_bucket_location",
    "serialise_list_objects_v2",
    "serialise_list_objects",
    "serialise_initiate_multipart_upload",
    "serialise_complete_multipart_upload",
    "serialise_list_parts",
    "serialise_list_multipart_uploads",
    "serialise_delete_objects_response",
    "serialise_error",
    "deserialise_delete_objects_request",
    "parse_complete_multipart_upload_request",
]

#: The one XML namespace S3 uses for all 2006-03-01 protocol bodies. Applied as
#: the default namespace on every document root this module emits, and stripped
#: when parsing request bodies so clients that omit it (as many do) still parse.
S3_NAMESPACE = "http://s3.amazonaws.com/doc/2006-03-01/"


class MalformedXMLError(ValueError):
    """Raised when a request body cannot be parsed as the expected S3 shape.

    Carries a human-readable reason so the caller can map it onto the S3
    ``MalformedXML`` error document without re-deriving the cause.
    """


# ---------------------------------------------------------------------------
# ElementTree helpers
#
# Everything funnels through these so namespace handling and serialisation are
# identical for every document, and so no code path reaches for string
# concatenation to build XML.
# ---------------------------------------------------------------------------


def _root(tag: str) -> ET.Element:
    """Create a document root element in the S3 default namespace."""
    return ET.Element(f"{{{S3_NAMESPACE}}}{tag}")


def _child(parent: ET.Element, tag: str, text: str | None = None) -> ET.Element:
    """Append a namespaced child element, optionally carrying text."""
    el = ET.SubElement(parent, f"{{{S3_NAMESPACE}}}{tag}")
    if text is not None:
        el.text = text
    return el


def _child_if(parent: ET.Element, tag: str, text: str | None) -> None:
    """Append a child only when ``text`` is not ``None`` (omit optional nodes)."""
    if text is not None:
        _child(parent, tag, text)


def _bool(value: bool) -> str:
    """Render a boolean the way S3 does in XML bodies (lowercase)."""
    return "true" if value else "false"


def _iso(value: datetime) -> str:
    """Render a timestamp in the millisecond-precision ISO 8601 form S3 uses."""
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


def _to_bytes(root: ET.Element) -> bytes:
    """Serialise a document root to a UTF-8, XML-declared byte string.

    ``ElementTree`` registers the S3 namespace with an empty prefix so it is
    emitted as the default namespace (``xmlns="..."``) rather than as ``ns0:``.
    """
    out = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    return bytes(out)


def _localname(tag: str) -> str:
    """Return the local name of a possibly-namespaced ``ElementTree`` tag."""
    return tag.rsplit("}", 1)[-1]


def _find_text(parent: ET.Element, local: str) -> str | None:
    """Find the first direct child with local name ``local`` and return its text.

    Namespace-agnostic: matches whether or not the client declared the S3
    namespace, which real-world clients inconsistently do.
    """
    for child in parent:
        if _localname(child.tag) == local:
            return child.text
    return None


# Register the empty prefix once at import so the default namespace is emitted
# without an ``ns0`` alias on every serialised document.
ET.register_namespace("", S3_NAMESPACE)


# ---------------------------------------------------------------------------
# Bucket operations (requirement 4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Bucket:
    """One entry in a :class:`ListBucketsResponse` (requirement 4.1)."""

    name: str
    creation_date: datetime


@dataclass(frozen=True)
class ListBucketsResponse:
    """Response body for ListBuckets (requirement 4.1).

    Attributes:
        buckets: The caller's buckets, in the order they should be listed.
        owner_id: The canonical owner id reported in the ``Owner`` block.
        owner_display_name: The owner display name reported in the ``Owner``
            block.
    """

    buckets: tuple[Bucket, ...] = ()
    owner_id: str = ""
    owner_display_name: str = ""


def serialise_list_buckets(response: ListBucketsResponse) -> bytes:
    """Serialise a :class:`ListBucketsResponse` to its S3 XML body."""
    root = _root("ListAllMyBucketsResult")
    owner = _child(root, "Owner")
    _child(owner, "ID", response.owner_id)
    _child(owner, "DisplayName", response.owner_display_name)
    buckets = _child(root, "Buckets")
    for bucket in response.buckets:
        el = _child(buckets, "Bucket")
        _child(el, "Name", bucket.name)
        _child(el, "CreationDate", _iso(bucket.creation_date))
    return _to_bytes(root)


@dataclass(frozen=True)
class GetBucketLocationResponse:
    """Response body for GetBucketLocation (requirements 4.1, 4.5).

    The gateway returns the configured ``S3_REGION`` here. S3 historically emits
    an empty ``LocationConstraint`` for ``us-east-1``; this codec writes the
    region text verbatim and leaves that policy decision to the caller, which
    may pass an empty string.
    """

    location_constraint: str


def serialise_get_bucket_location(response: GetBucketLocationResponse) -> bytes:
    """Serialise a :class:`GetBucketLocationResponse` to its S3 XML body."""
    root = _root("LocationConstraint")
    root.text = response.location_constraint
    return _to_bytes(root)


# ---------------------------------------------------------------------------
# Object listing (requirement 5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ObjectEntry:
    """A single ``Contents`` entry in a listing response (requirement 5.2)."""

    key: str
    last_modified: datetime
    etag: str
    size: int
    storage_class: str = "STANDARD"


@dataclass(frozen=True)
class CommonPrefix:
    """A single ``CommonPrefixes`` entry in a listing response (requirement 5.2)."""

    prefix: str


def _append_contents(root: ET.Element, entry: ObjectEntry) -> None:
    """Append one ``Contents`` block shared by the v1 and v2 listing shapes."""
    el = _child(root, "Contents")
    _child(el, "Key", entry.key)
    _child(el, "LastModified", _iso(entry.last_modified))
    _child(el, "ETag", entry.etag)
    _child(el, "Size", str(entry.size))
    _child(el, "StorageClass", entry.storage_class)


def _append_common_prefixes(root: ET.Element, prefixes: tuple[CommonPrefix, ...]) -> None:
    """Append the ``CommonPrefixes`` blocks shared by the v1 and v2 shapes."""
    for cp in prefixes:
        el = _child(root, "CommonPrefixes")
        _child(el, "Prefix", cp.prefix)


@dataclass(frozen=True)
class ListObjectsV2Response:
    """Response body for ListObjectsV2 (requirements 5.1, 5.2, 5.3).

    ``contents`` and ``common_prefixes`` are emitted in the order supplied; the
    handler is responsible for the lexicographic ordering and disjointness the
    specification requires. When ``is_truncated`` is true,
    ``next_continuation_token`` carries the token for the next page (5.3).
    """

    name: str
    prefix: str = ""
    key_count: int = 0
    max_keys: int = 1000
    is_truncated: bool = False
    delimiter: str | None = None
    encoding_type: str | None = None
    continuation_token: str | None = None
    next_continuation_token: str | None = None
    start_after: str | None = None
    contents: tuple[ObjectEntry, ...] = ()
    common_prefixes: tuple[CommonPrefix, ...] = ()


def serialise_list_objects_v2(response: ListObjectsV2Response) -> bytes:
    """Serialise a :class:`ListObjectsV2Response` to its S3 XML body."""
    root = _root("ListBucketResult")
    _child(root, "Name", response.name)
    _child(root, "Prefix", response.prefix)
    _child(root, "KeyCount", str(response.key_count))
    _child(root, "MaxKeys", str(response.max_keys))
    _child_if(root, "Delimiter", response.delimiter)
    _child_if(root, "EncodingType", response.encoding_type)
    _child(root, "IsTruncated", _bool(response.is_truncated))
    _child_if(root, "ContinuationToken", response.continuation_token)
    _child_if(root, "NextContinuationToken", response.next_continuation_token)
    _child_if(root, "StartAfter", response.start_after)
    for entry in response.contents:
        _append_contents(root, entry)
    _append_common_prefixes(root, response.common_prefixes)
    return _to_bytes(root)


@dataclass(frozen=True)
class ListObjectsResponse:
    """Response body for ListObjects v1 (requirements 5.1, 5.2, 5.3).

    The v1 shape paginates with ``marker`` / ``next_marker`` rather than
    continuation tokens. ``next_marker`` is only meaningful when ``is_truncated``
    is true (5.3).
    """

    name: str
    prefix: str = ""
    marker: str = ""
    max_keys: int = 1000
    is_truncated: bool = False
    delimiter: str | None = None
    encoding_type: str | None = None
    next_marker: str | None = None
    contents: tuple[ObjectEntry, ...] = ()
    common_prefixes: tuple[CommonPrefix, ...] = ()


def serialise_list_objects(response: ListObjectsResponse) -> bytes:
    """Serialise a :class:`ListObjectsResponse` (v1) to its S3 XML body."""
    root = _root("ListBucketResult")
    _child(root, "Name", response.name)
    _child(root, "Prefix", response.prefix)
    _child(root, "Marker", response.marker)
    _child_if(root, "NextMarker", response.next_marker)
    _child(root, "MaxKeys", str(response.max_keys))
    _child_if(root, "Delimiter", response.delimiter)
    _child_if(root, "EncodingType", response.encoding_type)
    _child(root, "IsTruncated", _bool(response.is_truncated))
    for entry in response.contents:
        _append_contents(root, entry)
    _append_common_prefixes(root, response.common_prefixes)
    return _to_bytes(root)


# ---------------------------------------------------------------------------
# Multipart upload (requirement 10)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InitiateMultipartUploadResponse:
    """Response body for CreateMultipartUpload (requirement 10.1)."""

    bucket: str
    key: str
    upload_id: str


def serialise_initiate_multipart_upload(response: InitiateMultipartUploadResponse) -> bytes:
    """Serialise an :class:`InitiateMultipartUploadResponse` to its S3 XML body."""
    root = _root("InitiateMultipartUploadResult")
    _child(root, "Bucket", response.bucket)
    _child(root, "Key", response.key)
    _child(root, "UploadId", response.upload_id)
    return _to_bytes(root)


@dataclass(frozen=True)
class CompleteMultipartUploadResponse:
    """Response body for CompleteMultipartUpload (requirement 10.9).

    ``etag`` is the composite multipart ETag of the form
    ``<md5-of-concatenated-part-md5s>-<part-count>``; this codec writes it
    verbatim and leaves its computation to the multipart handler.
    """

    location: str
    bucket: str
    key: str
    etag: str


def serialise_complete_multipart_upload(response: CompleteMultipartUploadResponse) -> bytes:
    """Serialise a :class:`CompleteMultipartUploadResponse` to its S3 XML body."""
    root = _root("CompleteMultipartUploadResult")
    _child(root, "Location", response.location)
    _child(root, "Bucket", response.bucket)
    _child(root, "Key", response.key)
    _child(root, "ETag", response.etag)
    return _to_bytes(root)


@dataclass(frozen=True)
class Part:
    """One staged part reported by ListParts (requirement 10.7)."""

    part_number: int
    last_modified: datetime
    etag: str
    size: int


@dataclass(frozen=True)
class ListPartsResponse:
    """Response body for ListParts (requirement 10.7)."""

    bucket: str
    key: str
    upload_id: str
    parts: tuple[Part, ...] = ()
    part_number_marker: int = 0
    next_part_number_marker: int = 0
    max_parts: int = 1000
    is_truncated: bool = False
    storage_class: str = "STANDARD"


def serialise_list_parts(response: ListPartsResponse) -> bytes:
    """Serialise a :class:`ListPartsResponse` to its S3 XML body."""
    root = _root("ListPartsResult")
    _child(root, "Bucket", response.bucket)
    _child(root, "Key", response.key)
    _child(root, "UploadId", response.upload_id)
    _child(root, "StorageClass", response.storage_class)
    _child(root, "PartNumberMarker", str(response.part_number_marker))
    _child(root, "NextPartNumberMarker", str(response.next_part_number_marker))
    _child(root, "MaxParts", str(response.max_parts))
    _child(root, "IsTruncated", _bool(response.is_truncated))
    for part in response.parts:
        el = _child(root, "Part")
        _child(el, "PartNumber", str(part.part_number))
        _child(el, "LastModified", _iso(part.last_modified))
        _child(el, "ETag", part.etag)
        _child(el, "Size", str(part.size))
    return _to_bytes(root)


@dataclass(frozen=True)
class MultipartUpload:
    """One in-progress upload reported by ListMultipartUploads (requirement 10.8)."""

    key: str
    upload_id: str
    initiated: datetime
    storage_class: str = "STANDARD"


@dataclass(frozen=True)
class ListMultipartUploadsResponse:
    """Response body for ListMultipartUploads (requirement 10.8)."""

    bucket: str
    uploads: tuple[MultipartUpload, ...] = ()
    key_marker: str = ""
    upload_id_marker: str = ""
    next_key_marker: str = ""
    next_upload_id_marker: str = ""
    prefix: str = ""
    delimiter: str | None = None
    max_uploads: int = 1000
    is_truncated: bool = False


def serialise_list_multipart_uploads(response: ListMultipartUploadsResponse) -> bytes:
    """Serialise a :class:`ListMultipartUploadsResponse` to its S3 XML body."""
    root = _root("ListMultipartUploadsResult")
    _child(root, "Bucket", response.bucket)
    _child(root, "KeyMarker", response.key_marker)
    _child(root, "UploadIdMarker", response.upload_id_marker)
    _child(root, "NextKeyMarker", response.next_key_marker)
    _child(root, "NextUploadIdMarker", response.next_upload_id_marker)
    _child(root, "Prefix", response.prefix)
    _child_if(root, "Delimiter", response.delimiter)
    _child(root, "MaxUploads", str(response.max_uploads))
    _child(root, "IsTruncated", _bool(response.is_truncated))
    for upload in response.uploads:
        el = _child(root, "Upload")
        _child(el, "Key", upload.key)
        _child(el, "UploadId", upload.upload_id)
        _child(el, "StorageClass", upload.storage_class)
        _child(el, "Initiated", _iso(upload.initiated))
    return _to_bytes(root)


# ---------------------------------------------------------------------------
# Batch delete (requirement 9.6)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeleteObjectRequestEntry:
    """One ``Object`` entry parsed from a DeleteObjects request (requirement 9.6)."""

    key: str
    version_id: str | None = None


@dataclass(frozen=True)
class DeleteObjectsRequest:
    """Parsed body of a DeleteObjects (batch delete) request (requirement 9.6).

    ``quiet`` mirrors the request's ``<Quiet>`` flag: when true, the matching
    response omits successfully deleted keys and reports only errors.
    """

    objects: tuple[DeleteObjectRequestEntry, ...] = ()
    quiet: bool = False


def deserialise_delete_objects_request(data: bytes) -> DeleteObjectsRequest:
    """Parse a DeleteObjects request body into a :class:`DeleteObjectsRequest`.

    Accepts the body whether or not the client declared the S3 namespace. Raises
    :class:`MalformedXMLError` when the body is not well-formed XML, is not a
    ``Delete`` document, or carries an ``Object`` entry without a ``Key``.
    """
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise MalformedXMLError(f"request body is not well-formed XML: {exc}") from exc

    if _localname(root.tag) != "Delete":
        raise MalformedXMLError(
            f"expected a <Delete> document, found <{_localname(root.tag)}>"
        )

    quiet = (_find_text(root, "Quiet") or "false").strip().lower() == "true"

    entries: list[DeleteObjectRequestEntry] = []
    for child in root:
        if _localname(child.tag) != "Object":
            continue
        key = _find_text(child, "Key")
        if key is None:
            raise MalformedXMLError("<Object> entry is missing a <Key>")
        entries.append(
            DeleteObjectRequestEntry(key=key, version_id=_find_text(child, "VersionId"))
        )

    return DeleteObjectsRequest(objects=tuple(entries), quiet=quiet)


@dataclass(frozen=True)
class DeletedObject:
    """A successfully deleted key reported in a DeleteObjects response (9.6)."""

    key: str
    version_id: str | None = None
    delete_marker: bool = False
    delete_marker_version_id: str | None = None


@dataclass(frozen=True)
class DeleteError:
    """A per-key failure reported in a DeleteObjects response (requirement 9.6)."""

    key: str
    code: str
    message: str
    version_id: str | None = None


@dataclass(frozen=True)
class DeleteObjectsResponse:
    """Response body for DeleteObjects (requirement 9.6).

    Carries per-key success (``deleted``) and failure (``errors``) status for
    every requested key. In quiet mode the handler supplies an empty ``deleted``
    tuple so only errors are reported.
    """

    deleted: tuple[DeletedObject, ...] = ()
    errors: tuple[DeleteError, ...] = ()


def serialise_delete_objects_response(response: DeleteObjectsResponse) -> bytes:
    """Serialise a :class:`DeleteObjectsResponse` to its S3 XML body."""
    root = _root("DeleteResult")
    for deleted in response.deleted:
        el = _child(root, "Deleted")
        _child(el, "Key", deleted.key)
        _child_if(el, "VersionId", deleted.version_id)
        if deleted.delete_marker:
            _child(el, "DeleteMarker", _bool(True))
        _child_if(el, "DeleteMarkerVersionId", deleted.delete_marker_version_id)
    for err in response.errors:
        el = _child(root, "Error")
        _child(el, "Key", err.key)
        _child_if(el, "VersionId", err.version_id)
        _child(el, "Code", err.code)
        _child(el, "Message", err.message)
    return _to_bytes(root)


# ---------------------------------------------------------------------------
# CompleteMultipartUpload request parsing (requirement 10.4, 10.5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompletePart:
    """One ``Part`` entry parsed from a CompleteMultipartUpload request body."""

    part_number: int
    etag: str


def parse_complete_multipart_upload_request(data: bytes) -> tuple[CompletePart, ...]:
    """Parse the ``Part`` list from a CompleteMultipartUpload request body.

    Returns the parts in the order they appear in the body; the multipart
    handler validates ascending order and completeness (requirements 10.4,
    10.5). Raises :class:`MalformedXMLError` when the body is not well-formed, is
    not a ``CompleteMultipartUpload`` document, or carries a ``Part`` without a
    valid integer ``PartNumber`` or an ``ETag``.
    """
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise MalformedXMLError(f"request body is not well-formed XML: {exc}") from exc

    if _localname(root.tag) != "CompleteMultipartUpload":
        raise MalformedXMLError(
            f"expected a <CompleteMultipartUpload> document, found <{_localname(root.tag)}>"
        )

    parts: list[CompletePart] = []
    for child in root:
        if _localname(child.tag) != "Part":
            continue
        number_text = _find_text(child, "PartNumber")
        etag = _find_text(child, "ETag")
        if number_text is None or etag is None:
            raise MalformedXMLError("<Part> entry is missing <PartNumber> or <ETag>")
        try:
            part_number = int(number_text)
        except ValueError as exc:
            raise MalformedXMLError(f"invalid <PartNumber> value {number_text!r}") from exc
        parts.append(CompletePart(part_number=part_number, etag=etag))

    return tuple(parts)


# ---------------------------------------------------------------------------
# Error document (returned for every failure)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Error:
    """The standard S3 error XML document.

    Attributes:
        code: The S3 error code (e.g. ``NoSuchBucket``), mapped to an HTTP
            status by the caller.
        message: A human-readable description of the error.
        resource: The bucket/key the request targeted, when known.
        request_id: The gateway request id, echoed for client-side correlation.
        extra: Additional error-specific elements (e.g. ``BucketName``,
            ``MaxSizeAllowed``) appended as ``<name>value</name>`` children.
    """

    code: str
    message: str
    resource: str | None = None
    request_id: str | None = None
    extra: dict[str, str] = field(default_factory=dict)


def serialise_error(error: Error) -> bytes:
    """Serialise an :class:`Error` to the standard S3 error XML body."""
    root = _root("Error")
    _child(root, "Code", error.code)
    _child(root, "Message", error.message)
    for name, value in error.extra.items():
        _child(root, name, value)
    _child_if(root, "Resource", error.resource)
    _child_if(root, "RequestId", error.request_id)
    return _to_bytes(root)

"""msgpack codec for :class:`~gateway.app.format.types.CommitRecord`.

Serialises the commit record written last for each generation (caption
``tgo3-c-<generation_id>``) — the message whose presence makes a generation
recoverable — and deserialises it back on recovery. The pair is round-trip
correct field by field (requirements 22.3, 22.4) and rejects any payload whose
``format_version`` is not 1 before constructing the model (requirements 16.1,
16.2).

``last_modified`` is carried as an ISO 8601 string (``datetime.isoformat`` /
``datetime.fromisoformat``) so microseconds and timezone offset survive the
round-trip exactly.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from gateway.app.format._msgpack_codec import (
    as_bytes,
    as_int,
    as_str,
    as_str_dict,
    pack,
    unpack,
)
from gateway.app.format.types import CommitRecord, validate_format_version

__all__ = ["deserialise", "serialise"]


def serialise(obj: CommitRecord) -> bytes:
    """Serialise a :class:`CommitRecord` to a msgpack payload."""
    return pack(
        {
            "format_version": obj.format_version,
            "generation_id": obj.generation_id.bytes,
            "bucket_id": obj.bucket_id.bytes,
            "key": obj.key,
            "generation_number": obj.generation_number,
            "blob_manifest_message_id": obj.blob_manifest_message_id,
            "etag": obj.etag,
            "checksums": obj.checksums,
            "content_type": obj.content_type,
            "user_meta": obj.user_meta,
            "last_modified": obj.last_modified.isoformat(),
        }
    )


def deserialise(data: bytes) -> CommitRecord:
    """Deserialise a msgpack payload to a :class:`CommitRecord`.

    Raises:
        FormatVersionError: If the payload's ``format_version`` is not 1.
    """
    fields = unpack(data)
    validate_format_version(as_int(fields["format_version"], "format_version"))
    return CommitRecord(
        format_version=as_int(fields["format_version"], "format_version"),
        generation_id=UUID(bytes=as_bytes(fields["generation_id"], "generation_id")),
        bucket_id=UUID(bytes=as_bytes(fields["bucket_id"], "bucket_id")),
        key=as_str(fields["key"], "key"),
        generation_number=as_int(fields["generation_number"], "generation_number"),
        blob_manifest_message_id=as_int(
            fields["blob_manifest_message_id"], "blob_manifest_message_id"
        ),
        etag=as_str(fields["etag"], "etag"),
        checksums=as_str_dict(fields["checksums"], "checksums"),
        content_type=as_str(fields["content_type"], "content_type"),
        user_meta=as_str_dict(fields["user_meta"], "user_meta"),
        last_modified=datetime.fromisoformat(as_str(fields["last_modified"], "last_modified")),
    )

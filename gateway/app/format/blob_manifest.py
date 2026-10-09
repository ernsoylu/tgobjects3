"""msgpack codec for :class:`~gateway.app.format.types.BlobManifest`.

Serialises the manifest written to the bucket channel (caption
``tgo3-b-<blob_id>``) after every data chunk is confirmed, and deserialises it
back on recovery. The pair is round-trip correct field by field (requirements
22.5, 22.6) and rejects any payload whose ``format_version`` is not 1 before
constructing the model (requirements 16.1, 16.2).
"""

from __future__ import annotations

from uuid import UUID

from gateway.app.format._msgpack_codec import (
    as_bytes,
    as_int,
    as_int_list,
    pack,
    unpack,
)
from gateway.app.format.types import BlobManifest, validate_format_version

__all__ = ["deserialise", "serialise"]


def serialise(obj: BlobManifest) -> bytes:
    """Serialise a :class:`BlobManifest` to a msgpack payload."""
    return pack(
        {
            "format_version": obj.format_version,
            "blob_id": obj.blob_id,
            "bucket_id": obj.bucket_id.bytes,
            "sha256": obj.sha256,
            "size": obj.size,
            "chunk_records": obj.chunk_records,
            "chunk_message_ids": obj.chunk_message_ids,
            "key_wrapped": obj.key_wrapped,
        }
    )


def deserialise(data: bytes) -> BlobManifest:
    """Deserialise a msgpack payload to a :class:`BlobManifest`.

    Raises:
        FormatVersionError: If the payload's ``format_version`` is not 1.
    """
    fields = unpack(data)
    validate_format_version(as_int(fields["format_version"], "format_version"))
    return BlobManifest(
        format_version=as_int(fields["format_version"], "format_version"),
        blob_id=as_bytes(fields["blob_id"], "blob_id"),
        bucket_id=UUID(bytes=as_bytes(fields["bucket_id"], "bucket_id")),
        sha256=as_bytes(fields["sha256"], "sha256"),
        size=as_int(fields["size"], "size"),
        chunk_records=as_int(fields["chunk_records"], "chunk_records"),
        chunk_message_ids=as_int_list(fields["chunk_message_ids"], "chunk_message_ids"),
        key_wrapped=as_bytes(fields["key_wrapped"], "key_wrapped"),
    )

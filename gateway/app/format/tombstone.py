"""msgpack codec for :class:`~gateway.app.format.types.Tombstone`.

Serialises the tombstone written on DeleteObject (caption
``tgo3-t-<generation_id>``) and deserialises it back on recovery, where it
marks the referenced generation DELETED. The pair is round-trip correct field
by field and rejects any payload whose ``format_version`` is not 1 before
constructing the model (requirements 16.1, 16.2).
"""

from __future__ import annotations

from uuid import UUID

from gateway.app.format._msgpack_codec import as_bytes, as_int, as_str, pack, unpack
from gateway.app.format.types import Tombstone, validate_format_version

__all__ = ["deserialise", "serialise"]


def serialise(obj: Tombstone) -> bytes:
    """Serialise a :class:`Tombstone` to a msgpack payload."""
    return pack(
        {
            "format_version": obj.format_version,
            "generation_id": obj.generation_id.bytes,
            "bucket_id": obj.bucket_id.bytes,
            "key": obj.key,
            "generation_number": obj.generation_number,
        }
    )


def deserialise(data: bytes) -> Tombstone:
    """Deserialise a msgpack payload to a :class:`Tombstone`.

    Raises:
        FormatVersionError: If the payload's ``format_version`` is not 1.
    """
    fields = unpack(data)
    validate_format_version(as_int(fields["format_version"], "format_version"))
    return Tombstone(
        format_version=as_int(fields["format_version"], "format_version"),
        generation_id=UUID(bytes=as_bytes(fields["generation_id"], "generation_id")),
        bucket_id=UUID(bytes=as_bytes(fields["bucket_id"], "bucket_id")),
        key=as_str(fields["key"], "key"),
        generation_number=as_int(fields["generation_number"], "generation_number"),
    )

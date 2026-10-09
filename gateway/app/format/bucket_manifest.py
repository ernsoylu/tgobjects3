"""msgpack codec for :class:`~gateway.app.format.types.BucketManifest`.

Serialises the manifest written on bucket creation and key rotation (caption
``tgo3-k-<version>``) — carrying the bucket KEK wrapped by the root key — and
deserialises it back on recovery. The pair is round-trip correct field by field
and rejects any payload whose ``format_version`` is not 1 before constructing
the model (requirements 16.1, 16.2).
"""

from __future__ import annotations

from gateway.app.format._msgpack_codec import as_bytes, as_int, as_str, pack, unpack
from gateway.app.format.types import BucketManifest, validate_format_version

__all__ = ["deserialise", "serialise"]


def serialise(obj: BucketManifest) -> bytes:
    """Serialise a :class:`BucketManifest` to a msgpack payload."""
    return pack(
        {
            "format_version": obj.format_version,
            "bucket_name": obj.bucket_name,
            "kek_wrapped": obj.kek_wrapped,
            "kek_version": obj.kek_version,
        }
    )


def deserialise(data: bytes) -> BucketManifest:
    """Deserialise a msgpack payload to a :class:`BucketManifest`.

    Raises:
        FormatVersionError: If the payload's ``format_version`` is not 1.
    """
    fields = unpack(data)
    validate_format_version(as_int(fields["format_version"], "format_version"))
    return BucketManifest(
        format_version=as_int(fields["format_version"], "format_version"),
        bucket_name=as_str(fields["bucket_name"], "bucket_name"),
        kek_wrapped=as_bytes(fields["kek_wrapped"], "kek_wrapped"),
        kek_version=as_int(fields["kek_version"], "kek_version"),
    )

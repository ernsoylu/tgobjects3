"""msgpack codec for :class:`~gateway.app.format.types.Checkpoint`.

Serialises the index snapshot written periodically to the system channel
(caption ``tgo3-p-<timestamp>``) — the encrypted PostgreSQL dump envelope plus
the per-channel scan high-water marks the Recovery CLI starts from — and
deserialises it back. The pair is round-trip correct field by field and rejects
any payload whose ``format_version`` is not 1 before constructing the model
(requirements 16.1, 16.2).
"""

from __future__ import annotations

from gateway.app.format._msgpack_codec import as_bytes, as_int, as_int_dict, pack, unpack
from gateway.app.format.types import Checkpoint, validate_format_version

__all__ = ["deserialise", "serialise"]


def serialise(obj: Checkpoint) -> bytes:
    """Serialise a :class:`Checkpoint` to a msgpack payload."""
    return pack(
        {
            "format_version": obj.format_version,
            "pg_dump_enc": obj.pg_dump_enc,
            "high_water": obj.high_water,
        }
    )


def deserialise(data: bytes) -> Checkpoint:
    """Deserialise a msgpack payload to a :class:`Checkpoint`.

    Raises:
        FormatVersionError: If the payload's ``format_version`` is not 1.
    """
    fields = unpack(data)
    validate_format_version(as_int(fields["format_version"], "format_version"))
    return Checkpoint(
        format_version=as_int(fields["format_version"], "format_version"),
        pg_dump_enc=as_bytes(fields["pg_dump_enc"], "pg_dump_enc"),
        high_water=as_int_dict(fields["high_water"], "high_water"),
    )

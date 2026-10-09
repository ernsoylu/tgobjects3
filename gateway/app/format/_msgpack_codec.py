"""Shared msgpack encode/decode helpers for the format-layer codecs.

The five message codecs (``blob_manifest``, ``commit_record``, ``tombstone``,
``bucket_manifest``, ``checkpoint`` — task 7.2) all follow the same shape:

    serialise(model)   → dump the model to a plain dict, lower the non-trivial
                         field types (UUID, datetime) to msgpack-encodable
                         forms, then pack to bytes.
    deserialise(data)  → unpack to a dict, validate the raw ``format_version``
                         *before* building the model (so an unsupported version
                         raises :class:`FormatVersionError` reporting the exact
                         value found — requirement 16.2), lift the lowered field
                         types back to their native Python types, then construct
                         and return the validated model.

msgpack natively round-trips ``int``, ``str``, ``bytes``, ``list`` and ``dict``
with string keys, which covers most fields. The two field types that need
explicit handling are:

    - :class:`uuid.UUID` → its 16 raw bytes (``UUID.bytes`` /
      ``UUID(bytes=...)``). Packed with ``bytes`` so it is distinct from any
      ``str`` field.
    - :class:`datetime.datetime` → an ISO 8601 string (``isoformat`` /
      ``fromisoformat``), which preserves microseconds and timezone offset
      exactly so the round-trip compares equal by value (requirements 22.3,
      22.4).

Keeping this logic in one place guarantees every codec encodes a given type
identically, so the per-field round-trip contract holds uniformly.
"""

from __future__ import annotations

import msgpack

__all__ = [
    "as_bytes",
    "as_int",
    "as_int_dict",
    "as_int_list",
    "as_str",
    "as_str_dict",
    "pack",
    "unpack",
]


def pack(payload: dict[str, object]) -> bytes:
    """Pack a plain dict of msgpack-encodable values to bytes.

    ``use_bin_type`` keeps ``bytes`` and ``str`` distinct on the wire so a
    ``bytes`` field never decodes back as ``str`` (and vice versa), which the
    field-by-field round-trip relies on.
    """
    return bytes(msgpack.packb(payload, use_bin_type=True))


def unpack(data: bytes) -> dict[str, object]:
    """Unpack msgpack bytes to a dict with ``str`` keys and ``bytes`` values.

    ``raw=False`` decodes msgpack strings to ``str`` while leaving binary
    payloads as ``bytes``; ``strict_map_key=False`` is unnecessary because every
    map key in the format layer is a string.
    """
    result = msgpack.unpackb(data, raw=False)
    if not isinstance(result, dict):
        raise ValueError(f"expected a msgpack map at the top level, got {type(result).__name__}")
    return result


# ---------------------------------------------------------------------------
# Field extractors
#
# ``unpack`` yields values typed ``object``; these narrow a value to the type a
# model field declares, raising a clear ``ValueError`` when a payload carries
# the wrong type for a field rather than letting a mis-typed value reach model
# construction. They also satisfy strict typing at each codec's call site.
# ---------------------------------------------------------------------------


def _require(value: object, expected: type, field: str) -> None:
    """Raise :class:`ValueError` if ``value`` is not an instance of ``expected``.

    ``bool`` is a subclass of ``int`` but is never a valid value for the
    integer fields in the format layer, so it is rejected explicitly.
    """
    if expected is int and isinstance(value, bool):
        raise ValueError(f"field {field!r} must be int, got bool")
    if not isinstance(value, expected):
        raise ValueError(f"field {field!r} must be {expected.__name__}, got {type(value).__name__}")


def as_int(value: object, field: str = "value") -> int:
    """Narrow a decoded value to ``int``."""
    _require(value, int, field)
    assert isinstance(value, int)
    return value


def as_bytes(value: object, field: str = "value") -> bytes:
    """Narrow a decoded value to ``bytes``."""
    _require(value, bytes, field)
    assert isinstance(value, bytes)
    return value


def as_str(value: object, field: str = "value") -> str:
    """Narrow a decoded value to ``str``."""
    _require(value, str, field)
    assert isinstance(value, str)
    return value


def as_int_list(value: object, field: str = "value") -> list[int]:
    """Narrow a decoded value to ``list[int]``."""
    _require(value, list, field)
    assert isinstance(value, list)
    return [as_int(item, f"{field}[{i}]") for i, item in enumerate(value)]


def as_str_dict(value: object, field: str = "value") -> dict[str, str]:
    """Narrow a decoded value to ``dict[str, str]``."""
    _require(value, dict, field)
    assert isinstance(value, dict)
    return {
        as_str(key, f"{field} key"): as_str(val, f"{field}[{key!r}]")
        for key, val in value.items()
    }


def as_int_dict(value: object, field: str = "value") -> dict[str, int]:
    """Narrow a decoded value to ``dict[str, int]``."""
    _require(value, dict, field)
    assert isinstance(value, dict)
    return {
        as_str(key, f"{field} key"): as_int(val, f"{field}[{key!r}]")
        for key, val in value.items()
    }

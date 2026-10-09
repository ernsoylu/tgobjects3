"""Pure offset-to-record mapping for ranged reads.

Translates a requested byte range within a blob into the record indices that
cover it, and locates each record within its Telegram chunk document. These are
the arithmetic helpers behind GetObject's ranged reads: the gateway maps a
``Range`` header to the *minimal* set of records that covers the requested bytes
and downloads only those records (requirement 8.2).

The blob layout, per the design's "Offset Mapping" section, is::

    record r             = floor(o / R)
    chunk  c             = floor(r / n)
    intra-chunk offset d = (r mod n) * 2^20

where ``o`` is a byte offset within the blob, ``R`` is the plaintext bytes per
record (``MAX_PLAINTEXT`` = 1,048,560), and ``n`` is ``CHUNK_RECORDS`` — the
number of records packed into a single Telegram document.

Every function here is a pure, side-effect-free integer computation.
"""

from __future__ import annotations

from gateway.app.format.record_codec import MAX_PLAINTEXT

__all__ = [
    "chunk_for_record",
    "intra_chunk_offset",
    "records_for_range",
]

# Plaintext-record stride within a Telegram document. Records are packed back to
# back as full 2^20-byte ciphertext blocks, so a record's position inside its
# chunk document is a multiple of this stride (design: "Offset Mapping").
_RECORD_STRIDE = 1 << 20  # 2^20 == 1 MiB


def records_for_range(start: int, end: int, record_size: int = MAX_PLAINTEXT) -> list[int]:
    """Return the minimal, contiguous record indices covering ``[start, end]``.

    Both bounds are inclusive byte offsets within the blob. The returned list is
    the smallest contiguous run of record indices such that every byte in the
    requested range lives in one of them: removing any single record would leave
    at least one requested byte uncovered. This backs the requirement that a
    ``Range`` header downloads only the records it needs (requirement 8.2).

    Args:
        start: Inclusive start byte offset within the blob (``>= 0``).
        end: Inclusive end byte offset within the blob (``>= start``).
        record_size: Plaintext bytes per record (``R``); defaults to
            ``MAX_PLAINTEXT`` (1,048,560). Must be positive.

    Returns:
        The contiguous record indices ``[floor(start / R) .. floor(end / R)]``.

    Raises:
        ValueError: If ``record_size`` is not positive, ``start`` is negative,
            or ``end`` is less than ``start``.
    """
    if record_size <= 0:
        raise ValueError("record_size must be positive")
    if start < 0:
        raise ValueError("start must be non-negative")
    if end < start:
        raise ValueError("end must be >= start")

    first = start // record_size
    last = end // record_size
    return list(range(first, last + 1))


def chunk_for_record(record_index: int, chunk_records: int) -> int:
    """Return the chunk index ``c = floor(r / n)`` holding ``record_index``.

    Records are grouped ``chunk_records`` at a time into one Telegram document,
    so record ``r`` lives in chunk ``floor(r / n)`` (design: "Offset Mapping").

    Args:
        record_index: Zero-based record index ``r`` (``>= 0``).
        chunk_records: Records packed per chunk document ``n`` (``>= 1``).

    Returns:
        The zero-based chunk index containing ``record_index``.

    Raises:
        ValueError: If ``record_index`` is negative or ``chunk_records < 1``.
    """
    if record_index < 0:
        raise ValueError("record_index must be non-negative")
    if chunk_records < 1:
        raise ValueError("chunk_records must be >= 1")

    return record_index // chunk_records


def intra_chunk_offset(record_index: int, chunk_records: int) -> int:
    """Return the byte offset ``d = (r mod n) * 2^20`` of a record in its chunk.

    Within a chunk document each record occupies a full ``2^20``-byte ciphertext
    block, so record ``r`` starts at ``(r mod n) * 2^20`` bytes into the document
    that holds it (design: "Offset Mapping").

    Args:
        record_index: Zero-based record index ``r`` (``>= 0``).
        chunk_records: Records packed per chunk document ``n`` (``>= 1``).

    Returns:
        The byte offset of ``record_index`` within its chunk document.

    Raises:
        ValueError: If ``record_index`` is negative or ``chunk_records < 1``.
    """
    if record_index < 0:
        raise ValueError("record_index must be non-negative")
    if chunk_records < 1:
        raise ValueError("chunk_records must be >= 1")

    return (record_index % chunk_records) * _RECORD_STRIDE

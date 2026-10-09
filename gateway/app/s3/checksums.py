"""Checksum computation for S3 ``x-amz-checksum-*`` support.

The S3 API lets clients request integrity protection by declaring a checksum
algorithm (via the ``x-amz-checksum-algorithm`` header or a trailing header in
``aws-chunked`` uploads) and supplying the expected value base64-encoded. The
gateway must compute the same value over the received body bytes so it can
compare the two and reject mismatches (requirements 6.6, 7.10). AWS supports a
fixed set of algorithms — CRC32, CRC32C, CRC64NVME, SHA-1, SHA-256 — and any
other algorithm must be rejected before the payload is processed
(requirement 7.11).

This module is the single place that knows how to turn ``(algorithm, bytes)``
into the base64 string an S3 client expects. The CRC family is computed with
``awscrt`` (the same AWS Common Runtime implementation the official SDKs use,
which is important for CRC32C and CRC64NVME where the polynomial/seed details
must match AWS exactly); SHA-1 and SHA-256 come from the standard library
``hashlib``.

Encoding: for every algorithm the result is the base64 encoding of the raw
checksum bytes in network (big-endian) byte order — a 4-byte value for CRC32
and CRC32C, 8 bytes for CRC64NVME, and the raw digest for the SHA family. This
matches the wire format S3 clients place in ``x-amz-checksum-<alg>`` headers.

Computing a checksum is CPU-bound; callers in the request path should dispatch
:func:`compute` to a thread pool rather than running it on the event loop (see
the concurrency notes in the design document).
"""

from __future__ import annotations

import base64
import hashlib
from typing import Final

from awscrt import checksums as _crt_checksums


class UnsupportedChecksumAlgorithm(Exception):
    """Raised when a checksum algorithm outside the supported set is requested.

    S3 clients may name only CRC32, CRC32C, CRC64NVME, SHA-1, or SHA-256
    (requirement 7.10). Any other value — including a misspelling or a casing
    the normaliser does not recognise — raises this error so the request can be
    rejected before the payload is processed (requirement 7.11). The offending
    algorithm name is preserved on :attr:`algorithm` for the caller to surface
    in its error response.
    """

    def __init__(self, algorithm: str) -> None:
        self.algorithm = algorithm
        super().__init__(f"Unsupported checksum algorithm: {algorithm!r}")


#: Byte width used when encoding each CRC algorithm's integer result, in
#: network (big-endian) byte order. SHA algorithms are not listed here because
#: :mod:`hashlib` already yields raw digest bytes.
_CRC_WIDTHS: Final[dict[str, int]] = {
    "CRC32": 4,
    "CRC32C": 4,
    "CRC64NVME": 8,
}


def compute(algorithm: str, data: bytes) -> str:
    """Compute the base64-encoded checksum of ``data`` for ``algorithm``.

    Args:
        algorithm: The checksum algorithm name as declared by the client, e.g.
            ``"CRC32"``, ``"CRC32C"``, ``"CRC64NVME"``, ``"SHA-1"``, or
            ``"SHA-256"``. Matching is case-insensitive and tolerant of the
            ``SHA1``/``SHA-1`` and ``SHA256``/``SHA-256`` spellings S3 clients
            use interchangeably.
        data: The exact bytes to checksum — in the request path, the fully
            received object body (requirement 6.6).

    Returns:
        The checksum as a base64 string: for the CRC family, the big-endian
        bytes of the integer result (4 bytes for CRC32/CRC32C, 8 for
        CRC64NVME); for the SHA family, the raw digest. This is the value an S3
        client places in the corresponding ``x-amz-checksum-*`` header, so the
        caller can compare it directly for validation (requirement 7.10).

    Raises:
        UnsupportedChecksumAlgorithm: If ``algorithm`` is not one of the five
            supported algorithms (requirement 7.11).
    """
    normalized = _normalize(algorithm)

    if normalized in _CRC_WIDTHS:
        value = _crc(normalized, data)
        raw = value.to_bytes(_CRC_WIDTHS[normalized], "big")
    elif normalized == "SHA-1":
        raw = hashlib.sha1(data).digest()
    elif normalized == "SHA-256":
        raw = hashlib.sha256(data).digest()
    else:  # pragma: no cover - defensive; _normalize already filtered.
        raise UnsupportedChecksumAlgorithm(algorithm)

    return base64.b64encode(raw).decode("ascii")


def _normalize(algorithm: str) -> str:
    """Map a client-supplied algorithm name to its canonical form.

    Normalises case and the hyphen-optional SHA spellings. Raises
    :class:`UnsupportedChecksumAlgorithm` (preserving the original spelling)
    for anything outside the supported set (requirement 7.11).
    """
    canonical = algorithm.strip().upper()
    if canonical in {"SHA1", "SHA-1"}:
        return "SHA-1"
    if canonical in {"SHA256", "SHA-256"}:
        return "SHA-256"
    if canonical in _CRC_WIDTHS:
        return canonical
    raise UnsupportedChecksumAlgorithm(algorithm)


def _crc(algorithm: str, data: bytes) -> int:
    """Return the integer CRC of ``data`` using the AWS Common Runtime.

    ``awscrt`` is used rather than a hand-rolled or ``zlib`` implementation so
    the polynomial and seed exactly match what AWS SDKs produce — this matters
    for CRC32C and CRC64NVME, which have no stdlib equivalent.
    """
    # ``awscrt`` ships no type stubs, so each call returns ``Any``; wrap in
    # ``int()`` to satisfy mypy strict and guarantee the declared return type.
    if algorithm == "CRC32":
        return int(_crt_checksums.crc32(data))
    if algorithm == "CRC32C":
        return int(_crt_checksums.crc32c(data))
    # CRC64NVME — the only remaining CRC entry in _CRC_WIDTHS.
    return int(_crt_checksums.crc64nvme(data))

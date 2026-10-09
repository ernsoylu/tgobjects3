"""Unit and property tests for :mod:`gateway.app.s3.checksums`.

Covers the base64 ``x-amz-checksum-*`` encoding for every supported algorithm
(requirements 6.6, 7.10) and rejection of unsupported algorithms
(requirement 7.11).
"""

from __future__ import annotations

import base64
import hashlib
import zlib

import pytest
from gateway.app.s3.checksums import UnsupportedChecksumAlgorithm, compute
from hypothesis import given
from hypothesis import strategies as st

# Known-answer vectors: algorithm -> {input bytes: expected base64 checksum}.
# Values independently cross-checked against standard tools (e.g. the empty and
# "hello world" SHA-256 digests are the canonical published vectors).
_VECTORS: dict[str, dict[bytes, str]] = {
    "CRC32": {b"": "AAAAAA==", b"hello world": "DUoRhQ=="},
    "CRC32C": {b"": "AAAAAA==", b"hello world": "yZRlqg=="},
    "CRC64NVME": {b"": "AAAAAAAAAAA=", b"hello world": "jSnVw/bqjr4="},
    "SHA-1": {
        b"": "2jmj7l5rSw0yVb/vlWAYkK/YBwk=",
        b"hello world": "Kq5sNclPz7QV2+lfQIuc6R7oRu0=",
    },
    "SHA-256": {
        b"": "47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU=",
        b"hello world": "uU0nuZNNPgilLlLX2n2r+sSE7+N6U4DukIj3rOLvzek=",
    },
}


@pytest.mark.parametrize(
    ("algorithm", "data", "expected"),
    [
        (algo, data, expected)
        for algo, cases in _VECTORS.items()
        for data, expected in cases.items()
    ],
)
def test_compute_known_vectors(algorithm: str, data: bytes, expected: str) -> None:
    """compute() returns the canonical base64 value for each algorithm."""
    assert compute(algorithm, data) == expected


@pytest.mark.parametrize(
    ("alias", "canonical"),
    [
        ("sha1", "SHA-1"),
        ("SHA1", "SHA-1"),
        ("sha-1", "SHA-1"),
        ("sha256", "SHA-256"),
        ("SHA256", "SHA-256"),
        ("sha-256", "SHA-256"),
        ("crc32", "CRC32"),
        ("Crc32c", "CRC32C"),
        ("  crc64nvme  ", "CRC64NVME"),
    ],
)
def test_compute_name_normalization(alias: str, canonical: str) -> None:
    """Algorithm names are matched case-insensitively and alias-tolerant."""
    data = b"normalize me"
    assert compute(alias, data) == compute(canonical, data)


@pytest.mark.parametrize("algorithm", ["", "MD5", "SHA-512", "crc", "sha3-256", "unknown"])
def test_unsupported_algorithm_raises(algorithm: str) -> None:
    """Unknown algorithms raise UnsupportedChecksumAlgorithm (req 7.11)."""
    with pytest.raises(UnsupportedChecksumAlgorithm) as excinfo:
        compute(algorithm, b"data")
    assert excinfo.value.algorithm == algorithm


# --- Property-based tests ---------------------------------------------------


@given(data=st.binary(max_size=4096))
def test_sha256_matches_hashlib(data: bytes) -> None:
    """CRC-free property: SHA-256 output equals base64(hashlib digest)."""
    expected = base64.b64encode(hashlib.sha256(data).digest()).decode("ascii")
    assert compute("SHA-256", data) == expected


@given(data=st.binary(max_size=4096))
def test_sha1_matches_hashlib(data: bytes) -> None:
    """SHA-1 output equals base64 of the hashlib SHA-1 digest."""
    expected = base64.b64encode(hashlib.sha1(data).digest()).decode("ascii")
    assert compute("SHA-1", data) == expected


@given(data=st.binary(max_size=4096))
def test_crc32_matches_zlib(data: bytes) -> None:
    """CRC32 output equals base64 of zlib's big-endian 4-byte CRC32."""
    expected = base64.b64encode(zlib.crc32(data).to_bytes(4, "big")).decode("ascii")
    assert compute("CRC32", data) == expected


@given(
    algorithm=st.sampled_from(["CRC32", "CRC32C", "CRC64NVME", "SHA-1", "SHA-256"]),
    data=st.binary(max_size=4096),
)
def test_output_is_valid_base64_with_expected_width(algorithm: str, data: bytes) -> None:
    """Every result decodes as base64 to the algorithm's fixed raw width."""
    widths = {"CRC32": 4, "CRC32C": 4, "CRC64NVME": 8, "SHA-1": 20, "SHA-256": 32}
    raw = base64.b64decode(compute(algorithm, data), validate=True)
    assert len(raw) == widths[algorithm]

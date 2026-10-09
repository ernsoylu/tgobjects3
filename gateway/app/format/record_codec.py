"""AES-256-GCM seal/unseal for 1 MiB records.

This module is a pure (no I/O) codec for the record wire format described in
the design document's "Record Wire Format" section. Each record encrypts up to
``MAX_PLAINTEXT`` (1,048,560) plaintext bytes with AES-256-GCM, producing a
stored record laid out as::

    ┌───────────────────────────────────────────────┐
    │  12 bytes   nonce (record_index, big-endian)    │
    ├───────────────────────────────────────────────┤
    │  len(plaintext) bytes   AES-256-GCM ciphertext  │
    ├───────────────────────────────────────────────┤
    │  16 bytes   GCM authentication tag              │
    └───────────────────────────────────────────────┘

The GCM tag covers both the ciphertext and the additional authenticated data
(AAD). The AAD binds every record to its blob, position, total plaintext
length and last-record flag, so a record cannot be silently substituted,
reordered or truncated without failing authentication.

Requirements covered: 13.5, 13.6, 13.7, 22.1, 22.2.
"""

from __future__ import annotations

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

__all__ = [
    "AAD_FORMAT_VERSION",
    "CIPHERTEXT_BLOCK_SIZE",
    "KEY_SIZE",
    "MAX_PLAINTEXT",
    "NONCE_SIZE",
    "TAG_SIZE",
    "RecordAuthError",
    "seal",
    "unseal",
]

# Size of the AES-256 key in bytes.
KEY_SIZE = 32
# Size of the big-endian nonce prefix in bytes (96-bit GCM nonce).
NONCE_SIZE = 12
# Size of the GCM authentication tag in bytes.
TAG_SIZE = 16
# Maximum plaintext per record. A full ciphertext block (ciphertext + tag,
# excluding the nonce prefix) is then exactly 1,048,576 bytes (1 MiB).
MAX_PLAINTEXT = 1_048_560
# Full ciphertext block size excluding the nonce prefix: MAX_PLAINTEXT + TAG_SIZE.
CIPHERTEXT_BLOCK_SIZE = MAX_PLAINTEXT + TAG_SIZE
# First AAD byte: the record format version.
AAD_FORMAT_VERSION = b"\x01"

# blob_id is a fixed 16-byte identifier in the AAD layout.
_BLOB_ID_SIZE = 16
# record_index occupies 4 big-endian bytes in the AAD.
_RECORD_INDEX_AAD_BYTES = 4
# total_plaintext occupies 8 big-endian bytes in the AAD.
_TOTAL_PLAINTEXT_AAD_BYTES = 8


class RecordAuthError(Exception):
    """Raised when a record fails AES-256-GCM authentication on unseal.

    A failure means the ciphertext, tag or AAD did not match. No decrypted
    plaintext is ever returned when this is raised.
    """


def _build_aad(
    blob_id: bytes,
    record_index: int,
    total_plaintext: int,
    is_last: bool,
) -> bytes:
    """Construct the additional authenticated data for a record.

    Layout: ``format_version(1) || blob_id(16) || record_index(4, big-endian)
    || total_plaintext(8, big-endian) || is_last(1)``.
    """
    return (
        AAD_FORMAT_VERSION
        + blob_id
        + record_index.to_bytes(_RECORD_INDEX_AAD_BYTES, "big")
        + total_plaintext.to_bytes(_TOTAL_PLAINTEXT_AAD_BYTES, "big")
        + (b"\x01" if is_last else b"\x00")
    )


def _nonce(record_index: int) -> bytes:
    """Derive the 12-byte big-endian, zero-padded nonce from the record index."""
    return record_index.to_bytes(NONCE_SIZE, "big")


def _validate_common(
    key: bytes,
    blob_id: bytes,
    record_index: int,
    total_plaintext: int,
) -> None:
    """Validate inputs shared by :func:`seal` and :func:`unseal`."""
    if len(key) != KEY_SIZE:
        raise ValueError(f"key must be {KEY_SIZE} bytes, got {len(key)}")
    if len(blob_id) != _BLOB_ID_SIZE:
        raise ValueError(f"blob_id must be {_BLOB_ID_SIZE} bytes, got {len(blob_id)}")
    if record_index < 0 or record_index >= (1 << (_RECORD_INDEX_AAD_BYTES * 8)):
        raise ValueError(f"record_index out of range: {record_index}")
    if total_plaintext < 0 or total_plaintext >= (1 << (_TOTAL_PLAINTEXT_AAD_BYTES * 8)):
        raise ValueError(f"total_plaintext out of range: {total_plaintext}")


def seal(
    plaintext: bytes,
    key: bytes,
    blob_id: bytes,
    record_index: int,
    total_plaintext: int,
    is_last: bool,
) -> bytes:
    """Seal one record with AES-256-GCM.

    Args:
        plaintext: The record payload, at most :data:`MAX_PLAINTEXT` bytes.
        key: The 32-byte AES-256 blob key.
        blob_id: The 16-byte blob identifier bound into the AAD.
        record_index: Zero-based record position; also the GCM nonce.
        total_plaintext: Total plaintext length of the whole blob.
        is_last: Whether this is the final record of the blob.

    Returns:
        ``nonce(12) || ciphertext(len(plaintext)) || tag(16)``.

    Raises:
        ValueError: If any input is outside its permitted range.
    """
    _validate_common(key, blob_id, record_index, total_plaintext)
    if len(plaintext) > MAX_PLAINTEXT:
        raise ValueError(
            f"plaintext exceeds maximum record size {MAX_PLAINTEXT}: {len(plaintext)}"
        )

    nonce = _nonce(record_index)
    aad = _build_aad(blob_id, record_index, total_plaintext, is_last)
    # AESGCM.encrypt returns ciphertext with the 16-byte tag appended.
    sealed = AESGCM(key).encrypt(nonce, plaintext, aad)
    return nonce + sealed


def unseal(
    ciphertext: bytes,
    key: bytes,
    blob_id: bytes,
    record_index: int,
    total_plaintext: int,
    is_last: bool,
) -> bytes:
    """Unseal one record, verifying its AES-256-GCM tag before returning bytes.

    Args:
        ciphertext: A record produced by :func:`seal`:
            ``nonce(12) || ciphertext || tag(16)``.
        key: The 32-byte AES-256 blob key.
        blob_id: The 16-byte blob identifier bound into the AAD.
        record_index: Zero-based record position; also the GCM nonce.
        total_plaintext: Total plaintext length of the whole blob.
        is_last: Whether this is the final record of the blob.

    Returns:
        The original plaintext, byte-for-byte.

    Raises:
        ValueError: If any input is outside its permitted range or the record
            is too short to contain a nonce and tag.
        RecordAuthError: If GCM authentication fails. No partial plaintext is
            returned in this case.
    """
    _validate_common(key, blob_id, record_index, total_plaintext)
    if len(ciphertext) < NONCE_SIZE + TAG_SIZE:
        raise ValueError(
            f"record too short: need at least {NONCE_SIZE + TAG_SIZE} bytes, "
            f"got {len(ciphertext)}"
        )

    prefix_nonce = ciphertext[:NONCE_SIZE]
    expected_nonce = _nonce(record_index)
    if prefix_nonce != expected_nonce:
        # The prepended nonce must match the record_index-derived nonce;
        # a mismatch means the record does not belong at this position.
        raise RecordAuthError("record nonce does not match record_index")

    body = ciphertext[NONCE_SIZE:]
    aad = _build_aad(blob_id, record_index, total_plaintext, is_last)
    try:
        return AESGCM(key).decrypt(expected_nonce, body, aad)
    except InvalidTag as exc:
        raise RecordAuthError("record authentication failed") from exc

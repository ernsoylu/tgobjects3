"""Key hierarchy derivation and AES Key Wrap (RFC 3394) primitives.

This module implements the root of the TGOBJECTS3 key hierarchy:

    TGOS3_MASTER_KEY (env, min 16 chars)
        └─ HKDF-SHA256 → 32-byte root AES key
                ├─ bucket KEK (random 32 bytes, per bucket)
                │      wrapped by root key → AES Key Wrap (RFC 3394)
                └─ blob key (random 32 bytes, per blob)
                       wrapped by bucket KEK → AES Key Wrap (RFC 3394)

The HKDF domain-separation info string is fixed at ``b"tgobjects3-v1-root"``.
"""

from __future__ import annotations

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.keywrap import (
    InvalidUnwrap,
    aes_key_unwrap,
    aes_key_wrap,
)

#: HKDF domain-separation info string for the root key derivation.
ROOT_KEY_INFO = b"tgobjects3-v1-root"

#: Length in bytes of the derived root AES key.
ROOT_KEY_LENGTH = 32


class KeyUnwrapError(Exception):
    """Raised when an AES Key Wrap unwrap operation fails integrity checks."""


def derive_root_key(master_key: str) -> bytes:
    """Derive the 32-byte root AES key from the master key via HKDF-SHA256.

    Args:
        master_key: The master key supplied via ``TGOS3_MASTER_KEY``.

    Returns:
        A 32-byte root key suitable for use as the top-level key-encryption key.
    """
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=ROOT_KEY_LENGTH,
        salt=None,
        info=ROOT_KEY_INFO,
    )
    return hkdf.derive(master_key.encode("utf-8"))


def wrap(kek: bytes, plaintext_key: bytes) -> bytes:
    """Wrap ``plaintext_key`` under ``kek`` using AES Key Wrap (RFC 3394).

    Args:
        kek: The key-encryption key (16, 24, or 32 bytes).
        plaintext_key: The key material to wrap; length must be a multiple of
            8 bytes and at least 16 bytes.

    Returns:
        The wrapped key, 8 bytes longer than ``plaintext_key``.
    """
    return aes_key_wrap(kek, plaintext_key)


def unwrap(kek: bytes, wrapped_key: bytes) -> bytes:
    """Unwrap ``wrapped_key`` under ``kek`` using AES Key Wrap (RFC 3394).

    Args:
        kek: The key-encryption key (16, 24, or 32 bytes).
        wrapped_key: The wrapped key material produced by :func:`wrap`.

    Returns:
        The recovered plaintext key.

    Raises:
        KeyUnwrapError: If the integrity check fails (wrong KEK or corrupted
            ciphertext).
    """
    try:
        return aes_key_unwrap(kek, wrapped_key)
    except InvalidUnwrap as exc:
        raise KeyUnwrapError("AES Key Wrap integrity check failed") from exc

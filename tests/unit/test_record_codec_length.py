"""Property test for the record ciphertext length invariant.

Covers requirement 22.1: a 1,048,560-byte plaintext sealed with AES-256-GCM
must produce exactly 1,048,576 ciphertext bytes (ciphertext + 16-byte tag),
excluding the 12-byte nonce that :func:`seal` prepends to the stored record.
"""

from __future__ import annotations

from gateway.app.format.record_codec import (
    CIPHERTEXT_BLOCK_SIZE,
    MAX_PLAINTEXT,
    NONCE_SIZE,
    seal,
)
from hypothesis import given, settings
from hypothesis import strategies as st

# A 16-byte blob identifier and a 32-byte AES-256 key, held fixed across
# examples so that only the plaintext content varies: the invariant concerns
# the output *length*, which must not depend on the key or blob binding.
_BLOB_ID = bytes(range(16))
_KEY = bytes(range(32))


def _full_plaintext(seed: bytes) -> bytes:
    """Expand a small seed into exactly ``MAX_PLAINTEXT`` plaintext bytes.

    Drawing a full 1 MiB value directly through ``st.binary`` overruns
    Hypothesis' per-example draw budget. Instead we draw a small seed (so
    Hypothesis still varies the content and can shrink it) and tile it to the
    exact 1,048,560-byte boundary the invariant is defined at. The content is
    irrelevant to the length invariant; only the size must be exact.
    """
    if not seed:
        seed = b"\x00"
    repeats = MAX_PLAINTEXT // len(seed) + 1
    return (seed * repeats)[:MAX_PLAINTEXT]


# Feature: tgobjects3, Property 2: Record ciphertext length invariant
# Validates: Requirements 22.1
@settings(max_examples=200)
@given(seed=st.binary(min_size=1, max_size=256))
def test_record_ciphertext_length_invariant(seed: bytes) -> None:
    """A full 1,048,560-byte plaintext seals to exactly 1,048,576 ciphertext bytes.

    The stored record is ``nonce(12) || ciphertext || tag(16)``; subtracting the
    12-byte nonce leaves the ciphertext block (ciphertext + GCM tag), which must
    equal ``CIPHERTEXT_BLOCK_SIZE`` == 1,048,576.
    """
    plaintext = _full_plaintext(seed)
    assert len(plaintext) == MAX_PLAINTEXT

    sealed = seal(
        plaintext,
        _KEY,
        _BLOB_ID,
        record_index=0,
        total_plaintext=MAX_PLAINTEXT,
        is_last=True,
    )
    assert len(sealed) - NONCE_SIZE == 1_048_576
    assert len(sealed) - NONCE_SIZE == CIPHERTEXT_BLOCK_SIZE

"""Property tests for :mod:`gateway.app.format.record_codec`.

Covers the record codec round-trip guarantee: for any valid plaintext, key,
and blob identifier, unsealing a sealed record reproduces the original
plaintext byte-for-byte (requirement 22.2).
"""

from __future__ import annotations

from gateway.app.format.record_codec import MAX_PLAINTEXT, seal, unseal
from hypothesis import given, settings
from hypothesis import strategies as st


# Feature: tgobjects3, Property 1: Record codec round-trip
# Validates: Requirements 22.2
@settings(max_examples=500)
@given(
    plaintext=st.binary(min_size=1, max_size=MAX_PLAINTEXT),
    key=st.binary(min_size=32, max_size=32),
    blob_id=st.binary(min_size=16, max_size=16),
)
def test_record_codec_round_trip(plaintext: bytes, key: bytes, blob_id: bytes) -> None:
    """unseal(seal(pt, ...)) == pt for all valid inputs. Validates: Requirements 22.2."""
    sealed = seal(
        plaintext,
        key,
        blob_id,
        record_index=0,
        total_plaintext=len(plaintext),
        is_last=True,
    )
    recovered = unseal(
        sealed,
        key,
        blob_id,
        record_index=0,
        total_plaintext=len(plaintext),
        is_last=True,
    )
    assert recovered == plaintext

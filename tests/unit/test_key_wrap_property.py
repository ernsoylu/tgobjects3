"""Property tests for :mod:`gateway.app.format.key_wrap`.

Feature: tgobjects3, Property 10: AES Key Wrap round-trip.

Exercises the AES Key Wrap (RFC 3394) primitives that protect the key
hierarchy: per-bucket KEKs are wrapped under the root/master key and blob keys
are wrapped under the bucket KEK (requirements 13.3, 13.4). The defining
property of a wrap/unwrap pair is that unwrapping a wrapped key under the same
KEK recovers the original key material exactly.
"""

from __future__ import annotations

from gateway.app.format.key_wrap import unwrap, wrap
from hypothesis import given
from hypothesis import strategies as st


# Feature: tgobjects3, Property 10: AES Key Wrap round-trip
# Validates: Requirements 13.3, 13.4
@given(
    kek=st.binary(min_size=32, max_size=32),
    key=st.binary(min_size=32, max_size=32),
)
def test_aes_key_wrap_round_trip(kek: bytes, key: bytes) -> None:
    """``unwrap(kek, wrap(kek, key)) == key`` for 32-byte KEK and key.

    Both per-bucket KEKs and blob keys are random 256-bit (32-byte) keys, so
    the round-trip must preserve the exact key material for any such pair.
    """
    assert unwrap(kek, wrap(kek, key)) == key

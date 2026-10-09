"""Property tests for :mod:`gateway.app.format.blob_manifest`.

Feature: tgobjects3, Property 4: BlobManifest serialisation round-trip.

Exercises the BlobManifest msgpack codec round-trip guarantee: for any valid
manifest, deserialising its serialised form reproduces every field exactly
(requirements 22.5, 22.6). The strategy covers all BlobManifest field types —
the 16-byte ``blob_id``, the ``bucket_id`` UUID, the ``sha256`` digest, the
non-negative ``size``, the positive ``chunk_records`` count, the
``chunk_message_ids`` integer list, and the wrapped ``key_wrapped`` material —
and the assertion compares the deserialised model field by field against the
original.
"""

from __future__ import annotations

import uuid

from gateway.app.format.blob_manifest import deserialise, serialise
from gateway.app.format.types import FORMAT_VERSION, BlobManifest
from hypothesis import given
from hypothesis import strategies as st

# msgpack encodes integers within the signed/unsigned 64-bit range. The model's
# integer fields are non-negative, so constrain generated values to the uint64
# range to stay within what the wire codec can represent.
_UINT64_MAX = 2**64 - 1


@st.composite
def blob_manifests(draw: st.DrawFn) -> BlobManifest:
    """Build a :class:`BlobManifest` covering every field type.

    Each field is drawn independently across its full valid domain so the
    round-trip is exercised against varied blob ids, UUIDs, digests, sizes,
    chunk counts, message-id lists, and wrapped key material.
    """
    return BlobManifest(
        format_version=FORMAT_VERSION,
        blob_id=draw(st.binary(min_size=16, max_size=16)),
        bucket_id=draw(st.uuids()),
        sha256=draw(st.binary(min_size=32, max_size=32)),
        size=draw(st.integers(min_value=0, max_value=_UINT64_MAX)),
        chunk_records=draw(st.integers(min_value=1, max_value=_UINT64_MAX)),
        chunk_message_ids=draw(
            st.lists(st.integers(min_value=0, max_value=_UINT64_MAX), max_size=16)
        ),
        key_wrapped=draw(st.binary(min_size=0, max_size=256)),
    )


# Feature: tgobjects3, Property 4: BlobManifest serialisation round-trip
# Validates: Requirements 22.5, 22.6
@given(manifest=blob_manifests())
def test_blob_manifest_round_trip(manifest: BlobManifest) -> None:
    """``deserialise(serialise(m)) == m`` field by field. Validates: Requirements 22.5, 22.6."""
    recovered = deserialise(serialise(manifest))

    assert recovered.format_version == manifest.format_version
    assert recovered.blob_id == manifest.blob_id
    assert recovered.bucket_id == manifest.bucket_id
    assert isinstance(recovered.bucket_id, uuid.UUID)
    assert recovered.sha256 == manifest.sha256
    assert recovered.size == manifest.size
    assert recovered.chunk_records == manifest.chunk_records
    assert recovered.chunk_message_ids == manifest.chunk_message_ids
    assert recovered.key_wrapped == manifest.key_wrapped
    # Whole-model equality subsumes the per-field checks above and guards any
    # field added to the model later.
    assert recovered == manifest

"""Property tests for :mod:`gateway.app.format.commit_record`.

Feature: tgobjects3, Property 3: CommitRecord serialisation round-trip.

The commit record is the durability boundary written last for every object
generation; its presence in the channel is what makes a generation recoverable
(requirements 22.3, 22.4). The defining property of its codec is that any
well-formed :class:`CommitRecord` survives a ``serialise`` -> ``deserialise``
round-trip with every field equal by value to the original — including the
``last_modified`` timestamp, which is carried as an ISO 8601 string so
microseconds and timezone offset are preserved exactly.
"""

from __future__ import annotations

from datetime import UTC, datetime

from gateway.app.format.commit_record import deserialise, serialise
from gateway.app.format.types import CommitRecord
from hypothesis import given
from hypothesis import strategies as st


def _str_dicts() -> st.SearchStrategy[dict[str, str]]:
    """String-keyed, string-valued maps covering ``checksums`` / ``user_meta``."""
    return st.dictionaries(keys=st.text(), values=st.text(), max_size=8)


# The msgpack wire codec carries integers in the 64-bit domain, which matches
# how these fields are used in practice: ``generation_number`` is a
# monotonically increasing unsigned counter and ``blob_manifest_message_id`` is
# a signed Telegram message id. Constraining the generators to those ranges
# keeps the strategy inside the format's actual input space rather than probing
# arbitrary-precision Python ints the wire format cannot represent.
_U64_MAX = 2**64 - 1
_I64_MIN = -(2**63)
_I64_MAX = 2**63 - 1


@st.composite
def commit_records(draw: st.DrawFn) -> CommitRecord:
    """Build a :class:`CommitRecord` covering all declared field types.

    Spans the full field set: two UUIDs, a key string, a non-negative
    generation number, a signed message id, free-form string fields, two
    string->string maps, and a timestamp. ``last_modified`` draws both naive
    and UTC-aware datetimes (both round-trip exactly through ISO 8601).
    """
    last_modified: datetime = draw(
        st.datetimes(timezones=st.sampled_from([None, UTC]))
    )
    return CommitRecord(
        generation_id=draw(st.uuids()),
        bucket_id=draw(st.uuids()),
        key=draw(st.text()),
        generation_number=draw(st.integers(min_value=0, max_value=_U64_MAX)),
        blob_manifest_message_id=draw(st.integers(min_value=_I64_MIN, max_value=_I64_MAX)),
        etag=draw(st.text()),
        checksums=draw(_str_dicts()),
        content_type=draw(st.text()),
        user_meta=draw(_str_dicts()),
        last_modified=last_modified,
    )


# Feature: tgobjects3, Property 3: CommitRecord serialisation round-trip
# Validates: Requirements 22.3, 22.4
@given(record=commit_records())
def test_commit_record_round_trip(record: CommitRecord) -> None:
    """``deserialise(serialise(record))`` equals ``record`` field by field.

    Pydantic model equality compares every field by value, so a single
    equality assertion covers the whole field set; individual fields are also
    asserted to make a counter-example pinpoint the offending field.
    """
    restored = deserialise(serialise(record))

    assert restored == record
    assert restored.format_version == record.format_version
    assert restored.generation_id == record.generation_id
    assert restored.bucket_id == record.bucket_id
    assert restored.key == record.key
    assert restored.generation_number == record.generation_number
    assert restored.blob_manifest_message_id == record.blob_manifest_message_id
    assert restored.etag == record.etag
    assert restored.checksums == record.checksums
    assert restored.content_type == record.content_type
    assert restored.user_meta == record.user_meta
    assert restored.last_modified == record.last_modified

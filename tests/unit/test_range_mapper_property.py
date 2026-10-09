"""Property tests for :mod:`gateway.app.format.range_mapper`.

Feature: tgobjects3, Property 11: Range request maps to minimal record set.

A ``Range`` header on GetObject must download only the records it needs: the
gateway maps the requested byte range to the *minimal* set of records that
covers it and fetches only those (requirement 8.2). The defining property of
:func:`records_for_range` is therefore twofold — the returned record indices
form a contiguous run (no gaps, so a single sequential read suffices), and the
run is minimal (every record in it holds at least one requested byte, so
removing any one record would leave a requested byte uncovered).
"""

from __future__ import annotations

from gateway.app.format.range_mapper import records_for_range
from gateway.app.format.record_codec import MAX_PLAINTEXT
from hypothesis import given
from hypothesis import strategies as st

# Upper bound on how many records a drawn range may span. The property is
# verified with O(1) arithmetic regardless of record count, but the function
# materialises ``range(first, last + 1)`` as a list, so this caps the per-example
# allocation cost and keeps examples well inside hypothesis's deadline. A few
# thousand records still exercises genuinely multi-record ranges.
_MAX_RECORD_SPAN = 4096


@st.composite
def _ranges(draw: st.DrawFn) -> tuple[int, int, int, int]:
    """Draw ``(object_size, start, end, record_size)`` within the input space.

    Models the arithmetic contract of a ranged read: ``object_size`` is a
    positive blob length, ``0 <= start <= end < object_size`` are inclusive
    byte offsets, and ``record_size`` is the positive plaintext stride ``R``.

    ``record_size`` is drawn from a realistic band centred on the production
    default ``MAX_PLAINTEXT`` (1,048,560) — spanning small sub-record sizes up to
    a few multiples of the default — so that multi-record ranges, the
    interesting non-degenerate cases, occur frequently without collapsing to a
    single record. The requested span is then bounded to at most
    ``_MAX_RECORD_SPAN`` records so the returned list stays small and each
    example stays fast; tiny record sizes paired with blob-spanning ranges
    (e.g. ``R == 1`` over a 4 GiB object) are outside the realistic input space
    and are excluded by construction rather than by weakening the property.
    """
    object_size = draw(st.integers(min_value=1, max_value=1 << 32))
    record_size = draw(
        st.integers(min_value=1, max_value=4 * MAX_PLAINTEXT),
    )
    start = draw(st.integers(min_value=0, max_value=object_size - 1))

    # Cap the end so the range spans at most ``_MAX_RECORD_SPAN`` records: the
    # last record index may exceed the first by at most ``_MAX_RECORD_SPAN - 1``.
    first = start // record_size
    max_last = first + (_MAX_RECORD_SPAN - 1)
    end_ceiling = min(object_size - 1, max_last * record_size + (record_size - 1))
    end = draw(st.integers(min_value=start, max_value=end_ceiling))
    return object_size, start, end, record_size


# Feature: tgobjects3, Property 11: Range request maps to minimal record set
# Validates: Requirements 8.2
@given(params=_ranges())
def test_range_maps_to_minimal_contiguous_record_set(
    params: tuple[int, int, int, int],
) -> None:
    """``records_for_range`` returns a contiguous, minimal covering set.

    For every requested range ``[start, end]`` the result must:

    * be non-empty and contiguous — consecutive integer indices with no gaps,
      so the records can be fetched as one sequential run;
    * cover the range — every record overlapping ``[start, end]`` is present;
    * be minimal — removing any single record leaves at least one requested
      byte uncovered, i.e. every returned record holds a requested byte.

    Every check is O(1) arithmetic independent of the record count: contiguity
    is confirmed against ``range(first, last + 1)`` and minimality reduces to
    checking the two boundary records, since any interior record is wholly
    inside ``[start, end]`` and so trivially holds requested bytes.
    """
    object_size, start, end, record_size = params

    records = records_for_range(start, end, record_size)

    # Non-empty: an inclusive range always needs at least one record.
    assert records, "a non-empty range must map to at least one record"

    # The expected minimal set, derived independently from the floor-division
    # mapping, so the assertions do not merely restate the implementation's own
    # range() construction.
    first = start // record_size
    last = end // record_size

    # Contiguous with correct endpoints: strictly increasing by exactly one with
    # no gaps, spanning exactly [first .. last]. Comparing against the
    # independently derived endpoints verifies coverage and contiguity together.
    assert records == list(range(first, last + 1))

    # Minimal: every returned record must hold at least one requested byte, so
    # dropping any one of them would uncover a byte. Interior records lie wholly
    # within [start, end] by construction, so only the two boundary records can
    # fail this — checking them is sufficient and keeps the cost O(1).
    # Record r covers blob bytes [r * record_size, (r + 1) * record_size - 1].
    for r in (first, last):
        rec_lo = r * record_size
        rec_hi = rec_lo + record_size - 1
        covered_lo = max(start, rec_lo)
        covered_hi = min(end, rec_hi)
        assert covered_lo <= covered_hi, (
            f"record {r} covers no requested byte and is not minimal"
        )

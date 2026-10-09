"""Property test for ListObjectsV2 delimiter grouping.

Feature: tgobjects3, Property 8: ListObjectsV2 delimiter produces disjoint
lexicographically-ordered results.

With a ``delimiter``, ListObjectsV2 collapses keys that share a common prefix up
to and including the first delimiter *beyond* ``prefix`` into a single
``CommonPrefixes`` entry; every other matching key is reported in ``Contents``
(requirement 5.2). The guarantees the handler must uphold are:

* **disjointness** — no key is represented in both ``Contents`` and
  ``CommonPrefixes``;
* **ordering** — both ``Contents`` and ``CommonPrefixes`` come back in
  lexicographic order.

:class:`~gateway.app.s3.operations.list_objects.ListObjectsHandler` builds a page
by walking the committed keys of a bucket in ascending key order and routing each
one through the pure helper
:func:`~gateway.app.s3.operations.list_objects._delimiter_group`: a key with a
delimiter at or after ``prefix`` folds into its group (repeats of the same group
are skipped), otherwise it lands in ``Contents``. The DB supplies the ascending
order and the ``prefix`` filter; the grouping/disjointness logic the property
pins down lives entirely in that forward pass over sorted keys. This test drives
that same pass directly over a generated, prefix-filtered, lexicographically
sorted key set, so it exercises the real grouping logic (requirement 5.2) with no
database and bounded per-example work.
"""

from __future__ import annotations

from gateway.app.s3 import xml_codecs
from gateway.app.s3.operations.list_objects import _delimiter_group
from hypothesis import given
from hypothesis import strategies as st

# A small alphabet for key segments keeps generated keys short and readable
# while still producing plenty of shared-prefix collisions that drive genuine
# ``CommonPrefixes`` roll-ups rather than a flat all-``Contents`` listing.
_SEGMENT_ALPHABET = "abc"
#: Candidate single-character delimiters. Chosen distinct from the segment
#: alphabet so a delimiter never collides with ordinary key content, and small
#: so the generator reliably lands delimiters inside keys.
_DELIMITERS = "/-"
#: Bound on the number of keys per example so each example stays well inside
#: hypothesis's deadline; a few dozen keys already produces many groups.
_MAX_KEYS = 24


@st.composite
def object_key_set(draw: st.DrawFn) -> tuple[str, str, list[str]]:
    """Draw ``(prefix, delimiter, keys)`` for a delimited listing.

    Models the input space of a ListObjectsV2 request with a delimiter against a
    bucket of object keys:

    * ``prefix`` is a short run over :data:`_SEGMENT_ALPHABET` (possibly empty),
      the client-supplied prefix filter;
    * ``delimiter`` is a single character from :data:`_DELIMITERS`, disjoint from
      the segment alphabet;
    * ``keys`` is a de-duplicated, lexicographically sorted list of keys that all
      begin with ``prefix`` — the handler's prefix filter and ``ORDER BY key``
      guarantee the engine only ever sees prefix-matching keys in ascending
      order, so the generator reproduces that precondition directly.

    Each key body (the part after ``prefix``) is assembled from alphabet
    segments interleaved with the delimiter, so keys land on both sides of the
    grouping rule: some carry a delimiter at or after ``prefix`` (folding into a
    ``CommonPrefixes`` group, often sharing a group with siblings) and some carry
    none (staying in ``Contents``). Keys and segment counts are bounded so the
    per-example work stays small.
    """
    prefix = draw(
        st.text(alphabet=_SEGMENT_ALPHABET, min_size=0, max_size=3),
    )
    delimiter = draw(st.sampled_from(_DELIMITERS))

    def _body() -> st.SearchStrategy[str]:
        # A key body is 1–4 alphabet segments joined by zero or more delimiters,
        # so a body may contain no delimiter (lands in Contents) or one/several
        # (folds into a group). Segments are non-empty to avoid empty group
        # names; the join interleaves delimiters between segments.
        segments = st.lists(
            st.text(alphabet=_SEGMENT_ALPHABET, min_size=1, max_size=3),
            min_size=1,
            max_size=4,
        )
        separators = st.lists(st.sampled_from(["", delimiter]), min_size=4, max_size=4)
        return st.builds(
            lambda segs, seps: "".join(
                seg + (sep if i < len(segs) - 1 else "")
                for i, (seg, sep) in enumerate(zip(segs, seps, strict=False))
            ),
            segments,
            separators,
        )

    bodies = draw(st.lists(_body(), min_size=0, max_size=_MAX_KEYS, unique=True))
    keys = sorted({prefix + body for body in bodies})
    return prefix, delimiter, keys


def _build_page(
    prefix: str, delimiter: str, keys: list[str]
) -> tuple[list[str], list[str]]:
    """Replay the handler's single forward pass over sorted keys.

    Mirrors :meth:`ListObjectsHandler._collect` exactly for the delimiter case:
    walk ``keys`` in ascending order, routing each key through
    :func:`_delimiter_group`. A key with no group is appended to ``Contents``; a
    key whose group repeats the most recently emitted ``CommonPrefix`` is skipped
    (already represented); otherwise its group opens a new ``CommonPrefixes``
    entry. ``max-keys`` truncation is deliberately omitted — Property 8 is about
    the disjointness and ordering of what is emitted, which truncation only
    shortens, so an unbounded page is the strongest statement of the property.

    Returns the ``(contents_keys, common_prefix_values)`` pair.
    """
    contents: list[str] = []
    common_prefixes: list[str] = []
    current_prefix: str | None = None
    for key in keys:
        group = _delimiter_group(key, prefix, delimiter)
        if group is not None:
            if group == current_prefix:
                continue
            current_prefix = group
            common_prefixes.append(group)
            # Sanity-check that we route through the same value object the
            # handler emits, keeping this replay honest to the real serialiser.
            assert xml_codecs.CommonPrefix(prefix=group).prefix == group
            continue
        contents.append(key)
    return contents, common_prefixes


# Feature: tgobjects3, Property 8: ListObjectsV2 delimiter produces disjoint
# lexicographically-ordered results
# Validates: Requirements 5.2
@given(data=object_key_set())
def test_delimiter_listing_is_disjoint_and_ordered(
    data: tuple[str, str, list[str]],
) -> None:
    """Contents and CommonPrefixes are disjoint and each is lexicographically ordered.

    For any key set, prefix and single-character delimiter, the listing engine's
    forward pass must produce:

    * **disjoint** collections — the set of ``Contents`` keys and the set of
      ``CommonPrefixes`` values share no element, and no ``Contents`` key falls
      under any emitted common prefix;
    * **lexicographically ordered** ``Contents`` and ``CommonPrefixes``.
    """
    prefix, delimiter, keys = data

    contents, common_prefixes = _build_page(prefix, delimiter, keys)

    # Lexicographic order within each collection (requirement 5.2). The engine
    # relies on the DB's ascending key order; sorted() here is the independent
    # oracle rather than a restatement of the engine's own construction.
    assert contents == sorted(contents)
    assert common_prefixes == sorted(common_prefixes)

    # CommonPrefixes carries no duplicate group (the repeat-skip collapses runs
    # of a group into one entry).
    assert len(common_prefixes) == len(set(common_prefixes))

    # Disjoint as literal values: no key string is also a common-prefix string.
    assert set(contents).isdisjoint(common_prefixes)

    # Stronger disjointness: a key routed to Contents must not fall under any
    # emitted common prefix, otherwise it would be represented twice
    # (requirement 5.2). Equivalently, every Contents key has no delimiter at or
    # after the prefix — exactly the condition that kept it out of a group.
    for key in contents:
        assert _delimiter_group(key, prefix, delimiter) is None
        for cp in common_prefixes:
            assert not key.startswith(cp), (
                f"Contents key {key!r} falls under CommonPrefix {cp!r}"
            )

    # Every emitted common prefix genuinely ends with the delimiter at or beyond
    # the client prefix, i.e. it is a real roll-up boundary (requirement 5.2).
    for cp in common_prefixes:
        assert cp.startswith(prefix)
        assert cp.endswith(delimiter)

"""Property tests for :mod:`gateway.app.format.sigv4_parser`.

Feature: tgobjects3, Property 5: SigV4 Authorization header round-trip.

Exercises the SigV4 ``Authorization`` header parser/serialiser pair. The
parser extracts the Algorithm, Credential, SignedHeaders, and Signature field
values from a well-formed header without modification (requirement 22.7), and
re-formatting the parsed fields reproduces a header whose four field values are
identical to the originals (requirement 22.8).

The defining property exercised here is the round-trip: for any well-formed
header ``h``, ``parse(h)`` must succeed and ``format_header(parse(h))`` must
yield a header whose four field values equal those of ``h``.
"""

from __future__ import annotations

from gateway.app.format.sigv4_parser import ParseResult, format_header, parse
from hypothesis import given
from hypothesis import strategies as st

# Field values are extracted by splitting on "," then stripping surrounding
# whitespace, so for a value to survive the round-trip unmodified it must be
# non-empty and contain no commas or leading/trailing whitespace. We draw from
# printable characters, excluding those characters, which keeps generated
# headers conforming to the subset the parser accepts verbatim.
_FORBIDDEN_CHARS = {",", " ", "\t", "\n", "\r", "\x0b", "\x0c"}


def _field_text(min_size: int = 1) -> st.SearchStrategy[str]:
    """A strategy for a non-empty field value with no commas or whitespace."""
    return st.text(
        alphabet=st.characters(
            min_codepoint=0x21,
            max_codepoint=0x7E,
            exclude_characters="".join(_FORBIDDEN_CHARS),
        ),
        min_size=min_size,
    )


def _signed_headers() -> st.SearchStrategy[str]:
    """A strategy for a semicolon-delimited ``SignedHeaders`` list value."""
    return st.lists(
        _field_text().filter(lambda s: ";" not in s),
        min_size=1,
        max_size=6,
    ).map(";".join)


@st.composite
def sigv4_auth_header(draw: st.DrawFn) -> tuple[str, ParseResult]:
    """Generate a well-formed header and its expected parsed fields.

    Returns a ``(header, expected)`` pair where ``header`` is a canonical,
    well-formed SigV4 ``Authorization`` header and ``expected`` is the
    :class:`ParseResult` its four field values should round-trip to.
    """
    algorithm = draw(_field_text())
    credential = draw(_field_text())
    signed_headers = draw(_signed_headers())
    signature = draw(_field_text())

    header = (
        f"{algorithm} "
        f"Credential={credential}, "
        f"SignedHeaders={signed_headers}, "
        f"Signature={signature}"
    )
    expected = ParseResult(
        algorithm=algorithm,
        credential=credential,
        signed_headers=signed_headers,
        signature=signature,
    )
    return header, expected


# Feature: tgobjects3, Property 5: SigV4 Authorization header round-trip
# Validates: Requirements 22.7, 22.8
@given(sigv4_auth_header())
def test_sigv4_authorization_header_round_trip(
    case: tuple[str, ParseResult],
) -> None:
    """``format_header(parse(h))`` preserves all four field values.

    For any well-formed header, the parser extracts the four fields without
    modification (req 22.7), and re-formatting them yields a header whose
    Algorithm, Credential, SignedHeaders, and Signature values are identical
    to the originals (req 22.8). We assert this by re-parsing the formatted
    header and comparing field values structurally.
    """
    header, expected = case

    parsed = parse(header)
    assert isinstance(parsed, ParseResult)
    assert parsed == expected

    formatted = format_header(parsed)

    reparsed = parse(formatted)
    assert isinstance(reparsed, ParseResult)
    assert reparsed == expected

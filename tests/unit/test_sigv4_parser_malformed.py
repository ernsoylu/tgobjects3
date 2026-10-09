"""Property tests for :mod:`gateway.app.format.sigv4_parser` malformed input.

Feature: tgobjects3, Property 6: SigV4 parser rejects malformed headers.

Exercises the robustness contract of the SigV4 ``Authorization`` header parser
(requirement 22.9): for any header that is missing at least one of the four
required fields (algorithm, ``Credential``, ``SignedHeaders``, ``Signature``)
or that contains a field in a non-conforming format, :func:`parse` must return
a structured :class:`ParseError` carrying both an error ``kind`` indicator and
the ``position`` at which parsing failed, and must never raise an unhandled
exception.
"""

from __future__ import annotations

from gateway.app.format.sigv4_parser import ParseError, parse
from hypothesis import given
from hypothesis import strategies as st

# The ordered required field keys that a well-formed header must carry after
# the algorithm token.
_REQUIRED_KEYS = ("Credential", "SignedHeaders", "Signature")

# A token strategy that avoids the structural characters the parser treats as
# separators (space, comma, equals) so that generated field values do not
# accidentally reintroduce a well-formed field the corruption removed.
_token = st.text(
    alphabet=st.characters(
        min_codepoint=0x21,
        max_codepoint=0x7E,
        blacklist_characters=" ,=",
    ),
    min_size=1,
    max_size=12,
)


def _build_valid_fields() -> st.SearchStrategy[dict[str, str]]:
    """Generate the four well-formed components of a SigV4 header."""
    return st.fixed_dictionaries(
        {
            "algorithm": _token,
            "Credential": _token,
            "SignedHeaders": _token,
            "Signature": _token,
        }
    )


def _render(parts: dict[str, str]) -> str:
    """Render a header string from its four components (may be partial)."""
    fields = ", ".join(
        f"{key}={parts[key]}" for key in _REQUIRED_KEYS if key in parts
    )
    algorithm = parts.get("algorithm", "")
    if algorithm and fields:
        return f"{algorithm} {fields}"
    if algorithm:
        return algorithm
    return fields


@st.composite
def malformed_headers(draw: st.DrawFn) -> str:
    """Build an ``Authorization`` header with at least one defect.

    Starts from a well-formed set of components and applies one corruption:
    dropping the algorithm token, dropping a required field, blanking a field
    value, replacing a ``Key=Value`` field with a malformed fragment, or
    renaming a required key to an unknown one. Each defect maps to one of the
    parser's documented :class:`ParseError` kinds.
    """
    parts = draw(_build_valid_fields())
    strategy = draw(
        st.sampled_from(
            [
                "drop-algorithm",
                "drop-field",
                "empty-value",
                "malformed-field",
                "unknown-field",
            ]
        )
    )

    if strategy == "drop-algorithm":
        del parts["algorithm"]
        return _render(parts)

    if strategy == "drop-field":
        victim = draw(st.sampled_from(_REQUIRED_KEYS))
        del parts[victim]
        return _render(parts)

    if strategy == "empty-value":
        victim = draw(st.sampled_from(_REQUIRED_KEYS))
        parts[victim] = ""
        return _render(parts)

    if strategy == "unknown-field":
        victim = draw(st.sampled_from(_REQUIRED_KEYS))
        bogus = draw(_token.filter(lambda t: t not in _REQUIRED_KEYS))
        rendered = _render(parts)
        return rendered.replace(f"{victim}=", f"{bogus}=", 1)

    # malformed-field: inject a field fragment with no ``=`` separator.
    rendered = _render(parts)
    junk = draw(_token)
    algorithm = parts["algorithm"]
    body = rendered[len(algorithm) + 1 :]
    return f"{algorithm} {junk}, {body}"


# Feature: tgobjects3, Property 6: SigV4 parser rejects malformed headers
# Validates: Requirements 22.9
@given(header=malformed_headers())
def test_malformed_header_returns_structured_error(header: str) -> None:
    """Malformed headers yield a ``ParseError`` with ``kind`` and ``position``.

    The parser must degrade gracefully: never raising, always returning a
    structured error whose ``kind`` and ``position`` are non-empty.
    """
    result = parse(header)

    assert isinstance(result, ParseError), (
        f"expected ParseError for malformed header {header!r}, got {result!r}"
    )
    assert result.kind, "ParseError.kind must be set"
    assert result.position, "ParseError.position must be set"


# Feature: tgobjects3, Property 6: SigV4 parser rejects malformed headers
# Validates: Requirements 22.9
@given(
    header=st.text(
        alphabet=st.characters(min_codepoint=0x20, max_codepoint=0x7E),
        max_size=40,
    )
)
def test_arbitrary_text_never_raises(header: str) -> None:
    """Arbitrary printable input never raises; errors are structured.

    Fuzzing with unconstrained text guards the "never raises an unhandled
    exception" clause of requirement 22.9 even for inputs the targeted
    strategy would not construct.
    """
    result = parse(header)

    if isinstance(result, ParseError):
        assert result.kind
        assert result.position

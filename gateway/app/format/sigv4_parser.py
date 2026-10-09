"""AWS Signature Version 4 ``Authorization`` header parser.

This module parses the S3 ``Authorization`` header produced by AWS SigV4
clients and reconstructs it from parsed fields. A well-formed header has the
shape::

    AWS4-HMAC-SHA256 Credential=AKID/20130524/us-east-1/s3/aws4_request, \
SignedHeaders=host;range;x-amz-date, Signature=fe5f80f77d5fa3beca038a248ff027

That is, an algorithm token, a single space, and then three comma-separated
``Key=Value`` fields in the fixed order ``Credential``, ``SignedHeaders``,
``Signature``.

The parser never raises on malformed input: :func:`parse` returns either a
:class:`ParseResult` on success or a :class:`ParseError` describing the first
failure. :func:`format_header` reconstructs a canonical header from a
:class:`ParseResult`, round-tripping the four field values.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Separator between the algorithm token and the field list.
_ALGORITHM_SEP = " "

#: Separator between the three ``Key=Value`` fields.
_FIELD_SEP = ", "

#: The ordered field keys expected after the algorithm token.
_CREDENTIAL_KEY = "Credential"
_SIGNED_HEADERS_KEY = "SignedHeaders"
_SIGNATURE_KEY = "Signature"


@dataclass(frozen=True)
class ParseResult:
    """A successfully parsed SigV4 ``Authorization`` header.

    Attributes:
        algorithm: The signing algorithm token, e.g. ``AWS4-HMAC-SHA256``.
        credential: The ``Credential`` scope string, e.g.
            ``AKID/20130524/us-east-1/s3/aws4_request``.
        signed_headers: The semicolon-delimited list of signed header names,
            e.g. ``host;range;x-amz-date``.
        signature: The lowercase hex signature.
    """

    algorithm: str
    credential: str
    signed_headers: str
    signature: str


@dataclass(frozen=True)
class ParseError:
    """A structured parse failure.

    Attributes:
        kind: A short machine-readable error kind indicator, one of
            ``"empty"``, ``"missing-algorithm"``, ``"missing-field"``,
            ``"malformed-field"``, ``"unknown-field"``, ``"duplicate-field"``,
            ``"field-order"``, or ``"empty-value"``.
        position: The field name or textual position at which parsing failed.
    """

    kind: str
    position: str


def parse(authorization: str) -> ParseResult | ParseError:
    """Parse a SigV4 ``Authorization`` header.

    Args:
        authorization: The raw ``Authorization`` header value.

    Returns:
        A :class:`ParseResult` when the header contains all four required
        fields in a conforming format, otherwise a :class:`ParseError`
        describing the first problem encountered. This function never raises
        on malformed input.
    """
    if not authorization or not authorization.strip():
        return ParseError(kind="empty", position="authorization")

    stripped = authorization.strip()

    # Split the algorithm token from the comma-separated field list on the
    # first space. SigV4 algorithm tokens contain no spaces.
    algo_sep = stripped.find(_ALGORITHM_SEP)
    if algo_sep == -1:
        return ParseError(kind="missing-algorithm", position="authorization")

    algorithm = stripped[:algo_sep]
    if not algorithm:
        return ParseError(kind="missing-algorithm", position="authorization")

    remainder = stripped[algo_sep + 1 :].strip()
    if not remainder:
        return ParseError(kind="missing-field", position=_CREDENTIAL_KEY)

    # Parse the comma-separated ``Key=Value`` fields into an ordered list.
    parsed_fields: list[tuple[str, str]] = []
    for raw_field in remainder.split(","):
        field = raw_field.strip()
        if not field:
            return ParseError(kind="malformed-field", position=field)

        eq = field.find("=")
        if eq <= 0:
            return ParseError(kind="malformed-field", position=field)

        key = field[:eq].strip()
        value = field[eq + 1 :].strip()
        if not key:
            return ParseError(kind="malformed-field", position=field)
        if not value:
            return ParseError(kind="empty-value", position=key)
        parsed_fields.append((key, value))

    expected_order = (_CREDENTIAL_KEY, _SIGNED_HEADERS_KEY, _SIGNATURE_KEY)
    values: dict[str, str] = {}
    for index, (key, value) in enumerate(parsed_fields):
        if key not in expected_order:
            return ParseError(kind="unknown-field", position=key)
        if key in values:
            return ParseError(kind="duplicate-field", position=key)
        if index < len(expected_order) and key != expected_order[index]:
            return ParseError(kind="field-order", position=key)
        values[key] = value

    for key in expected_order:
        if key not in values:
            return ParseError(kind="missing-field", position=key)

    return ParseResult(
        algorithm=algorithm,
        credential=values[_CREDENTIAL_KEY],
        signed_headers=values[_SIGNED_HEADERS_KEY],
        signature=values[_SIGNATURE_KEY],
    )


def format_header(result: ParseResult) -> str:
    """Reconstruct an ``Authorization`` header from parsed fields.

    The output is canonical: the algorithm token followed by a single space
    and the three ``Key=Value`` fields joined by ``", "`` in the order
    ``Credential``, ``SignedHeaders``, ``Signature``. For any header accepted
    by :func:`parse`, ``format_header(parse(h))`` yields a header whose four
    field values are identical to the originals.

    Args:
        result: The parsed fields to serialise.

    Returns:
        A canonical SigV4 ``Authorization`` header string.
    """
    fields = _FIELD_SEP.join(
        (
            f"{_CREDENTIAL_KEY}={result.credential}",
            f"{_SIGNED_HEADERS_KEY}={result.signed_headers}",
            f"{_SIGNATURE_KEY}={result.signature}",
        )
    )
    return f"{result.algorithm}{_ALGORITHM_SEP}{fields}"

"""AWS Signature Version 4 authentication for the S3 API surface.

This module verifies incoming S3 requests against the AWS SigV4 scheme, in both
of the two credential-presentation modes S3 clients use:

* **Header authentication** (:func:`verify_header_auth`) — the request carries
  an ``Authorization: AWS4-HMAC-SHA256 ...`` header plus an ``X-Amz-Date``
  timestamp (requirement 7.1).
* **Presigned query-string authentication** (:func:`verify_presigned_auth`) —
  the signature and its parameters travel in the query string under the
  ``X-Amz-*`` keys, with an ``X-Amz-Signature`` present (requirement 7.2).

Both paths enforce the same gateway-wide access-control contract: a request is
authenticated only when its computed signature matches the signature derived
from the stored secret for its access-key id, *and* its timestamp lies within
±15 minutes of the gateway's current clock (requirements 17.1, 17.3, 17.4). A
timestamp outside that window yields a ``RequestTimeTooSkewed`` outcome; an
unknown key or a signature mismatch yields a ``SignatureDoesNotMatch`` outcome
(design "Request Errors": both map to HTTP 403). Verification happens *before*
any request body is processed (requirements 6.1, 6.2, 7.3).

Streaming payloads
------------------
For ``x-amz-content-sha256`` values that select aws-chunked transfer encoding,
the seed signature established by header/presigned verification is extended per
chunk: :func:`verify_streaming_chunks` recomputes and checks each chunk's
``AWS4-HMAC-SHA256-PAYLOAD`` signature in sequence and, for the
``STREAMING-UNSIGNED-PAYLOAD-TRAILER`` variant, verifies the final trailer
signature before any trailer value may be trusted (requirements 7.5, 7.6, 7.7,
7.9). An ``UNSIGNED-PAYLOAD`` declaration is accepted with no body-hash check
(requirements 7.4, 17.6).

Secret storage and the bcrypt dependency
-----------------------------------------
Requirement 17.2 mandates that access-key secrets are stored as **bcrypt**
hashes, never in plaintext. Verifying a client signature, however, requires the
*plaintext* secret to re-derive the SigV4 signing key, so a bcrypt hash of the
secret cannot by itself re-sign a request. The gateway therefore stores the
secret in a form that both (a) lets the signing key be re-derived and (b)
satisfies the "no plaintext at rest" requirement; the lookup is abstracted
behind :class:`SecretProvider` so this module depends only on being handed the
material it needs — a :class:`StoredSecret` — rather than on any particular
storage encoding.

The bcrypt comparison used when a caller presents a secret for *equality*
checking (e.g. presign bootstrap / admin verification) is routed through
:class:`SecretVerifier`. **No bcrypt (or passlib) distribution is pinned in
``pyproject.toml`` and none is importable in the environment at the time this
module was written.** Rather than adding an unpinned dependency, the default
:class:`BcryptSecretVerifier` imports bcrypt lazily and raises
:class:`SecretVerificationUnavailableError` if it is absent, so the gap is
surfaced explicitly at the point of use instead of silently weakening auth. The
SigV4 signing-key derivation and canonicalisation below use only the standard
library (:mod:`hmac`, :mod:`hashlib`), which the task permits.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from gateway.app.format import sigv4_parser

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: The SigV4 signing algorithm token for request signatures.
ALGORITHM = "AWS4-HMAC-SHA256"

#: The service name component of the SigV4 credential scope for S3.
SERVICE = "s3"

#: The fixed terminator component of the SigV4 credential scope.
AWS4_REQUEST = "aws4_request"

#: Prefix prepended to the plaintext secret when deriving the signing key.
_SECRET_PREFIX = "AWS4"

#: Maximum allowed deviation between the request timestamp and the gateway
#: clock, in seconds (requirements 17.1, 17.4: "within 15 minutes").
MAX_CLOCK_SKEW_SECONDS = 15 * 60

#: ``x-amz-content-sha256`` sentinel: body hash is not signed (requirement 7.4).
UNSIGNED_PAYLOAD = "UNSIGNED-PAYLOAD"

#: ``x-amz-content-sha256`` sentinel: aws-chunked body, each chunk signed
#: (requirement 7.5).
STREAMING_SIGNED = "STREAMING-AWS4-HMAC-SHA256-PAYLOAD"

#: ``x-amz-content-sha256`` sentinel: aws-chunked body, unsigned chunks with a
#: signed trailer (requirement 7.7).
STREAMING_UNSIGNED_TRAILER = "STREAMING-UNSIGNED-PAYLOAD-TRAILER"

#: String-to-sign algorithm token for a signed streaming chunk.
_CHUNK_STRING_TO_SIGN_ALGO = "AWS4-HMAC-SHA256-PAYLOAD"

#: String-to-sign algorithm token for a streaming trailer signature.
_TRAILER_STRING_TO_SIGN_ALGO = "AWS4-HMAC-SHA256-TRAILER"

#: SHA-256 hex digest of the empty string, used as the "no payload" hash in the
#: per-chunk string-to-sign.
_EMPTY_SHA256_HEX = hashlib.sha256(b"").hexdigest()

#: Format of the ``X-Amz-Date`` / credential-scope date-time stamp.
_AMZ_DATETIME_FORMAT = "%Y%m%dT%H%M%SZ"

#: Format of the date component of the credential scope.
_AMZ_DATE_FORMAT = "%Y%m%d"


# ---------------------------------------------------------------------------
# Typed errors
# ---------------------------------------------------------------------------


class SigV4Error(Exception):
    """Base class for recoverable SigV4 authentication failures.

    These are *expected* request-level failures, not programming errors; the
    router translates them into the HTTP status / S3 error code recorded in the
    design's "Request Errors" table rather than letting them escape as 500s.
    """


class MalformedAuthError(SigV4Error):
    """The request's SigV4 material is structurally unparseable or incomplete.

    Raised when a required field (``Authorization`` header, ``X-Amz-Date``,
    ``X-Amz-Signature``, credential scope components, …) is missing or cannot be
    parsed. Maps to HTTP 400; distinct from a well-formed request whose
    signature simply does not match.
    """


class SecretVerificationUnavailableError(SigV4Error):
    """Bcrypt-based secret verification was requested but is not available.

    Requirement 17.2 requires bcrypt hashing of stored secrets, but no bcrypt
    (or passlib) distribution is pinned in ``pyproject.toml`` or importable in
    the current environment. The default :class:`BcryptSecretVerifier` raises
    this instead of silently skipping verification, so the missing dependency
    is surfaced loudly at the point of use.
    """


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SignedRequest:
    """The subset of an inbound HTTP request SigV4 verification depends on.

    A frozen, transport-agnostic value object (per the project's "typed value
    objects at boundaries" convention) assembled by the router from the FastAPI
    request before handing it to this module. Keeping verification decoupled
    from the ASGI request object makes the signing logic directly testable.

    Attributes:
        method: The HTTP method, upper-case (e.g. ``"GET"``, ``"PUT"``).
        path: The URL path, already percent-decoded exactly once by the server
            and *not* re-normalised, so canonicalisation can re-encode it.
        query: The decoded query-string parameters. For presigned requests this
            includes the ``X-Amz-*`` parameters; the ``X-Amz-Signature`` entry
            is excluded from the canonical query string during verification.
        headers: Request headers with lower-cased names. Only the headers named
            in ``SignedHeaders`` participate in the canonical request.
        content_sha256: The value of the ``x-amz-content-sha256`` header, or
            ``None`` if absent. Selects the payload-hashing mode (fixed hash,
            ``UNSIGNED-PAYLOAD`` or one of the streaming sentinels).
    """

    method: str
    path: str
    query: Mapping[str, str]
    headers: Mapping[str, str]
    content_sha256: str | None = None


@dataclass(frozen=True)
class CredentialScope:
    """The parsed components of a SigV4 credential scope.

    The credential string has the shape
    ``<access_key_id>/<date>/<region>/<service>/aws4_request``. This value
    object carries the five components after validation so callers never pick
    them apart by hand.

    Attributes:
        access_key_id: The client's access-key id (looked up via
            :class:`SecretProvider`).
        date: The ``YYYYMMDD`` scope date.
        region: The AWS region token, e.g. ``us-east-1``.
        service: The service token; must equal :data:`SERVICE` for S3.
        terminator: The fixed terminator; must equal :data:`AWS4_REQUEST`.
    """

    access_key_id: str
    date: str
    region: str
    service: str
    terminator: str

    @property
    def scope(self) -> str:
        """Return the ``date/region/service/aws4_request`` scope string.

        This is the trailing portion of the credential used verbatim as the
        third line of the SigV4 string-to-sign.
        """
        return f"{self.date}/{self.region}/{self.service}/{self.terminator}"


@dataclass(frozen=True)
class StoredSecret:
    """The stored material needed to re-derive a client's SigV4 signing key.

    Returned by :class:`SecretProvider` for a known access-key id. The gateway
    stores secrets per requirement 17.2; this value object is the handoff
    between storage and the signing logic. ``secret`` is the plaintext SigV4
    secret access key — required to re-derive the signing key — while
    ``secret_hash`` carries the bcrypt hash used for equality checks that do not
    involve re-signing (see :class:`SecretVerifier`).

    Exactly one of the two fields may be unset depending on the caller's needs,
    but signature verification requires ``secret`` to be present.

    Attributes:
        access_key_id: The access-key id this secret belongs to.
        secret: The plaintext SigV4 secret access key, or ``None`` if only the
            bcrypt hash is available.
        secret_hash: The bcrypt hash of the secret, or ``None`` if unavailable.
        disabled: ``True`` when the key exists but has been disabled; a disabled
            key never authenticates.
    """

    access_key_id: str
    secret: str | None = None
    secret_hash: str | None = None
    disabled: bool = False


@dataclass(frozen=True)
class AuthResult:
    """The outcome of a successful SigV4 verification.

    Only produced when authentication succeeds; failures raise a
    :class:`SigV4Error` subclass instead. Carries the authenticated identity and
    the seed signature streaming verification needs to continue the chunk
    signature chain.

    Attributes:
        access_key_id: The authenticated access-key id.
        scope: The validated credential scope.
        seed_signature: The request signature just verified, lower-case hex.
            For a signed streaming body this is the seed of the per-chunk
            signature chain consumed by :func:`verify_streaming_chunks`.
        signing_key: The derived SigV4 signing key for this request's
            date/region/service, reused to verify each streaming chunk without
            re-derivation.
        content_sha256: The declared payload mode copied from the request, so
            the router can decide whether to run streaming verification.
        signed_headers: The ordered, semicolon-joined list of signed header
            names taken from the request.
    """

    access_key_id: str
    scope: CredentialScope
    seed_signature: str
    signing_key: bytes
    content_sha256: str | None
    signed_headers: str


@dataclass(frozen=True)
class StreamingChunk:
    """One chunk of an aws-chunked streaming body.

    Attributes:
        data: The raw payload bytes of this chunk (excluding the chunk header
            and framing). A final zero-length chunk terminates the stream.
        signature: The chunk signature advertised in the chunk header's
            ``chunk-signature=`` field, lower-case hex. ``None`` for chunks of a
            ``STREAMING-UNSIGNED-PAYLOAD-TRAILER`` body, whose chunks are not
            individually signed.
    """

    data: bytes
    signature: str | None = None


@dataclass(frozen=True)
class StreamingTrailer:
    """The trailing header block of an aws-chunked streaming body.

    Attributes:
        canonical_bytes: The canonicalised trailer header bytes over which the
            trailer signature is computed (the trailer header lines in
            canonical form, excluding the ``x-amz-trailer-signature`` line).
        signature: The trailer signature advertised in the
            ``x-amz-trailer-signature`` trailing header, lower-case hex, or
            ``None`` when the body carried no signed trailer.
    """

    canonical_bytes: bytes
    signature: str | None = None


@dataclass(frozen=True)
class StreamingVerification:
    """The result of verifying a streaming body's chunk/trailer signatures.

    Attributes:
        chunk_count: Number of non-terminal data chunks verified.
        final_signature: The last chunk signature in the chain (or the seed
            signature if there were no chunks), lower-case hex.
        trailer_verified: ``True`` if a trailer signature was present and
            verified; ``False`` if no trailer signature was present.
    """

    chunk_count: int
    final_signature: str
    trailer_verified: bool = False


# ---------------------------------------------------------------------------
# Providers / protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class SecretProvider(Protocol):
    """Looks up the stored secret material for an access-key id.

    Injected into the verification functions so this module never touches the
    database directly. The production implementation reads the ``access_keys``
    table; tests supply an in-memory fake. The coroutine returns ``None`` for an
    unknown key, which the verifier treats as an authentication failure
    (requirement 17.3).
    """

    async def __call__(self, access_key_id: str) -> StoredSecret | None:
        """Return the stored secret for ``access_key_id`` or ``None``.

        Args:
            access_key_id: The access-key id extracted from the request's
                credential scope.

        Returns:
            The :class:`StoredSecret` for the key, or ``None`` if no such key
            exists.
        """
        ...


@runtime_checkable
class SecretVerifier(Protocol):
    """Checks a presented plaintext secret against a stored bcrypt hash.

    Separated from signature verification because the two are different
    operations: re-signing a request needs the plaintext secret, whereas
    confirming a secret the caller already holds is a hash comparison. Injected
    so the bcrypt backend can be swapped or faked in tests.
    """

    def verify(self, secret: str, secret_hash: str) -> bool:
        """Return whether ``secret`` matches ``secret_hash``.

        Args:
            secret: The candidate plaintext secret.
            secret_hash: The stored bcrypt hash to compare against.

        Returns:
            ``True`` if the secret matches the hash, ``False`` otherwise.
        """
        ...


class BcryptSecretVerifier:
    """Default :class:`SecretVerifier` backed by the ``bcrypt`` library.

    Imports ``bcrypt`` lazily on first use. Because no bcrypt distribution is
    pinned in ``pyproject.toml`` or importable in the current environment, a
    missing library raises :class:`SecretVerificationUnavailableError` rather
    than silently passing or failing — surfacing the unmet requirement 17.2
    dependency at the point of use. When a bcrypt distribution is later pinned,
    this class works unchanged.
    """

    def verify(self, secret: str, secret_hash: str) -> bool:
        """Compare ``secret`` to ``secret_hash`` using bcrypt.

        Raises:
            SecretVerificationUnavailableError: If ``bcrypt`` cannot be
                imported.
        """
        try:
            # bcrypt is intentionally not pinned in pyproject.toml yet
            # (requirement 17.2 dependency is unmet); the ignore self-corrects
            # once a bcrypt distribution is added, since unused ignores are
            # flagged under mypy strict.
            import bcrypt  # type: ignore[import-not-found,unused-ignore]
        except ImportError as exc:  # pragma: no cover - env-dependent
            raise SecretVerificationUnavailableError(
                "bcrypt is required for secret verification (requirement 17.2) "
                "but is not installed; no bcrypt dependency is pinned in "
                "pyproject.toml"
            ) from exc
        return bool(
            bcrypt.checkpw(secret.encode("utf-8"), secret_hash.encode("utf-8"))
        )


# ---------------------------------------------------------------------------
# Signing-key derivation and canonicalisation (stdlib hmac / hashlib)
# ---------------------------------------------------------------------------


def _hmac_sha256(key: bytes, msg: str) -> bytes:
    """Return ``HMAC-SHA256(key, msg)`` with ``msg`` UTF-8 encoded."""
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def derive_signing_key(secret: str, date: str, region: str, service: str) -> bytes:
    """Derive the SigV4 signing key for a given date/region/service.

    Implements the standard four-step HMAC-SHA256 chain:
    ``kDate = HMAC("AWS4"+secret, date)``, ``kRegion = HMAC(kDate, region)``,
    ``kService = HMAC(kRegion, service)``,
    ``kSigning = HMAC(kService, "aws4_request")`` (requirement 17.1 — the
    signing key must reproduce the client's signature from the stored secret).

    Args:
        secret: The plaintext SigV4 secret access key.
        date: The ``YYYYMMDD`` scope date.
        region: The region token.
        service: The service token (``s3``).

    Returns:
        The 32-byte signing key.
    """
    k_date = _hmac_sha256((_SECRET_PREFIX + secret).encode("utf-8"), date)
    k_region = _hmac_sha256(k_date, region)
    k_service = _hmac_sha256(k_region, service)
    return _hmac_sha256(k_service, AWS4_REQUEST)


def _uri_encode(value: str, *, encode_slash: bool) -> str:
    """Percent-encode ``value`` per the SigV4 canonicalisation rules.

    Every byte except the RFC 3986 unreserved set (``A-Za-z0-9-_.~``) is
    percent-encoded as upper-case hex. ``/`` is preserved in path segments
    (``encode_slash=False``) and encoded in query values
    (``encode_slash=True``).
    """
    unreserved = (
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.~"
    )
    out: list[str] = []
    for ch in value:
        if ch in unreserved or (ch == "/" and not encode_slash):
            out.append(ch)
        else:
            out.extend(f"%{byte:02X}" for byte in ch.encode("utf-8"))
    return "".join(out)


def _canonical_query_string(
    query: Mapping[str, str], *, exclude: frozenset[str]
) -> str:
    """Build the canonical query string, sorted and percent-encoded.

    Keys in ``exclude`` (notably ``X-Amz-Signature`` during presigned
    verification) are dropped. Remaining pairs are URI-encoded and sorted by
    encoded key then value.
    """
    pairs = [
        (_uri_encode(k, encode_slash=True), _uri_encode(v, encode_slash=True))
        for k, v in query.items()
        if k not in exclude
    ]
    pairs.sort()
    return "&".join(f"{k}={v}" for k, v in pairs)


def _canonical_headers(
    headers: Mapping[str, str], signed_headers: str
) -> str:
    """Build the canonical headers block for the signed header names.

    Each signed header contributes a ``name:trimmed-value\\n`` line, with the
    name lower-cased and the value's surrounding whitespace stripped and inner
    runs collapsed, in the order given by ``signed_headers``.

    Raises:
        MalformedAuthError: If a signed header is absent from ``headers``.
    """
    lower = {k.lower(): v for k, v in headers.items()}
    lines: list[str] = []
    for name in signed_headers.split(";"):
        if name not in lower:
            raise MalformedAuthError(f"signed header {name!r} missing from request")
        value = " ".join(lower[name].strip().split())
        lines.append(f"{name}:{value}\n")
    return "".join(lines)


def _canonical_request(
    request: SignedRequest,
    signed_headers: str,
    payload_hash: str,
    *,
    exclude_query: frozenset[str],
) -> str:
    """Assemble the SigV4 canonical request string.

    The canonical request is the newline-joined tuple of HTTP method, canonical
    URI, canonical query string, canonical headers, signed-headers list, and
    payload hash, exactly as defined by the SigV4 specification.
    """
    canonical_uri = _uri_encode(request.path, encode_slash=False)
    canonical_query = _canonical_query_string(request.query, exclude=exclude_query)
    canonical_headers = _canonical_headers(request.headers, signed_headers)
    return "\n".join(
        (
            request.method.upper(),
            canonical_uri,
            canonical_query,
            canonical_headers,
            signed_headers,
            payload_hash,
        )
    )


def _string_to_sign(amz_datetime: str, scope: str, canonical_request: str) -> str:
    """Build the SigV4 string-to-sign for a request signature.

    ``AWS4-HMAC-SHA256\\n{amz_datetime}\\n{scope}\\n{sha256hex(canonical_request)}``.
    """
    hashed = hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
    return "\n".join((ALGORITHM, amz_datetime, scope, hashed))


def _compute_signature(signing_key: bytes, string_to_sign: str) -> str:
    """Return the lower-case hex HMAC-SHA256 of ``string_to_sign``."""
    return hmac.new(
        signing_key, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()


# ---------------------------------------------------------------------------
# Scope / timestamp helpers
# ---------------------------------------------------------------------------


def _parse_credential(credential: str) -> CredentialScope:
    """Parse and validate a ``.../.../.../.../aws4_request`` credential.

    Raises:
        MalformedAuthError: If the credential does not have the five expected
            slash-separated components, or the service / terminator tokens are
            wrong for S3.
    """
    parts = credential.split("/")
    if len(parts) != 5:
        raise MalformedAuthError(
            "credential must have 5 slash-separated components"
        )
    access_key_id, date, region, service, terminator = parts
    if not access_key_id:
        raise MalformedAuthError("credential access-key id is empty")
    if service != SERVICE:
        raise MalformedAuthError(f"credential service must be {SERVICE!r}")
    if terminator != AWS4_REQUEST:
        raise MalformedAuthError(
            f"credential terminator must be {AWS4_REQUEST!r}"
        )
    return CredentialScope(
        access_key_id=access_key_id,
        date=date,
        region=region,
        service=service,
        terminator=terminator,
    )


def _parse_amz_datetime(value: str) -> datetime:
    """Parse an ``X-Amz-Date`` stamp (``YYYYMMDDTHHMMSSZ``) to an aware UTC datetime.

    Raises:
        MalformedAuthError: If the stamp does not match the expected format.
    """
    try:
        parsed = datetime.strptime(value, _AMZ_DATETIME_FORMAT)
    except ValueError as exc:
        raise MalformedAuthError(f"invalid X-Amz-Date {value!r}") from exc
    return parsed.replace(tzinfo=UTC)


def _check_clock_skew(request_time: datetime, now: datetime) -> None:
    """Enforce the ±15-minute timestamp window (requirements 17.1, 17.4).

    Raises:
        RequestTimeTooSkewed: If the request timestamp deviates from ``now`` by
            more than :data:`MAX_CLOCK_SKEW_SECONDS` seconds.
    """
    skew = abs((request_time - now).total_seconds())
    if skew > MAX_CLOCK_SKEW_SECONDS:
        raise RequestTimeTooSkewed(
            f"request time skew {skew:.0f}s exceeds the "
            f"{MAX_CLOCK_SKEW_SECONDS}s window"
        )


class SignatureDoesNotMatch(SigV4Error):
    """The computed signature does not match the client's, or the key is unknown.

    Covers both an unknown / disabled access-key id and a signature mismatch; by
    design these are indistinguishable to the client to avoid leaking which keys
    exist (requirement 17.3, design "Request Errors": HTTP 403
    ``SignatureDoesNotMatch``).
    """


class RequestTimeTooSkewed(SigV4Error):
    """The request timestamp lies outside the ±15-minute window.

    Maps to HTTP 403 ``RequestTimeTooSkewed`` (requirement 17.4, design
    "Request Errors").
    """


def _now_utc() -> datetime:
    """Return the current time as an aware UTC datetime (injection seam)."""
    return datetime.now(UTC)


async def _resolve_secret(
    provider: SecretProvider, access_key_id: str
) -> StoredSecret:
    """Look up and validate the stored secret for an access-key id.

    Raises:
        SignatureDoesNotMatch: If the key is unknown, disabled, or has no
            plaintext secret available to re-derive the signing key.
    """
    stored = await provider(access_key_id)
    if stored is None or stored.disabled:
        raise SignatureDoesNotMatch("unknown or disabled access-key id")
    if stored.secret is None:
        raise SignatureDoesNotMatch(
            "stored secret unavailable for signing-key derivation"
        )
    return stored


def _finish_verification(
    request: SignedRequest,
    scope: CredentialScope,
    stored: StoredSecret,
    amz_datetime: str,
    signed_headers: str,
    payload_hash: str,
    client_signature: str,
    *,
    exclude_query: frozenset[str],
) -> AuthResult:
    """Compute the expected signature and compare it to the client's.

    Shared by header and presigned verification once each has extracted the
    scope, signed headers, payload hash and client signature. Uses a
    constant-time comparison to avoid leaking timing information.

    Raises:
        SignatureDoesNotMatch: If the signatures differ.
    """
    assert stored.secret is not None  # guaranteed by _resolve_secret
    signing_key = derive_signing_key(
        stored.secret, scope.date, scope.region, scope.service
    )
    canonical = _canonical_request(
        request, signed_headers, payload_hash, exclude_query=exclude_query
    )
    sts = _string_to_sign(amz_datetime, scope.scope, canonical)
    expected = _compute_signature(signing_key, sts)
    if not hmac.compare_digest(expected, client_signature):
        raise SignatureDoesNotMatch("request signature mismatch")
    return AuthResult(
        access_key_id=scope.access_key_id,
        scope=scope,
        seed_signature=client_signature,
        signing_key=signing_key,
        content_sha256=request.content_sha256,
        signed_headers=signed_headers,
    )


def _payload_hash_for(request: SignedRequest) -> str:
    """Return the payload hash to use in the canonical request.

    For a fixed body this is the ``x-amz-content-sha256`` value; for the
    ``UNSIGNED-PAYLOAD`` and streaming sentinels the sentinel string itself is
    used verbatim, as the AWS SigV4 spec requires (requirements 7.4, 7.5, 7.7).
    When the header is absent the empty-body hash is used.
    """
    return request.content_sha256 or _EMPTY_SHA256_HEX


# ---------------------------------------------------------------------------
# Public verification entry points
# ---------------------------------------------------------------------------


async def verify_header_auth(
    request: SignedRequest, secret_provider: SecretProvider
) -> AuthResult:
    """Verify a header-authenticated SigV4 request (requirements 7.1, 6.1).

    Parses the ``Authorization`` header via :mod:`gateway.app.format.sigv4_parser`,
    validates the ``X-Amz-Date`` timestamp against the ±15-minute window
    (requirements 17.1, 17.4), re-derives the signing key from the stored secret
    for the credential's access-key id (requirement 17.1), and compares the
    recomputed signature with the client's in constant time (requirement 17.3).

    Args:
        request: The transport-agnostic request snapshot to verify.
        secret_provider: Callable resolving an access-key id to its stored
            secret material.

    Returns:
        An :class:`AuthResult` carrying the authenticated identity, seed
        signature and signing key for any subsequent streaming verification.

    Raises:
        MalformedAuthError: If the ``Authorization`` header or ``X-Amz-Date`` is
            missing or unparseable.
        RequestTimeTooSkewed: If the timestamp is outside the ±15-minute window.
        SignatureDoesNotMatch: If the key is unknown/disabled or the signature
            does not match.
    """
    headers = {k.lower(): v for k, v in request.headers.items()}
    authorization = headers.get("authorization")
    if not authorization:
        raise MalformedAuthError("missing Authorization header")

    parsed = sigv4_parser.parse(authorization)
    if isinstance(parsed, sigv4_parser.ParseError):
        raise MalformedAuthError(
            f"malformed Authorization header: {parsed.kind} at {parsed.position}"
        )
    if parsed.algorithm != ALGORITHM:
        raise MalformedAuthError(f"unsupported algorithm {parsed.algorithm!r}")

    amz_datetime = headers.get("x-amz-date")
    if not amz_datetime:
        raise MalformedAuthError("missing X-Amz-Date header")
    _check_clock_skew(_parse_amz_datetime(amz_datetime), _now_utc())

    scope = _parse_credential(parsed.credential)
    stored = await _resolve_secret(secret_provider, scope.access_key_id)
    return _finish_verification(
        request,
        scope,
        stored,
        amz_datetime,
        parsed.signed_headers,
        _payload_hash_for(request),
        parsed.signature,
        exclude_query=frozenset(),
    )


async def verify_presigned_auth(
    request: SignedRequest, secret_provider: SecretProvider
) -> AuthResult:
    """Verify a presigned (query-string) SigV4 request (requirement 7.2).

    Reads the ``X-Amz-*`` query parameters, validates the ``X-Amz-Date``
    timestamp against the ±15-minute window (requirements 17.1, 17.4), re-derives
    the signing key, and compares the recomputed signature with
    ``X-Amz-Signature`` in constant time (requirement 17.3). The
    ``X-Amz-Signature`` parameter itself is excluded from the canonical query
    string, and presigned requests use ``UNSIGNED-PAYLOAD`` as the payload hash
    (requirement 7.4).

    Args:
        request: The transport-agnostic request snapshot to verify.
        secret_provider: Callable resolving an access-key id to its stored
            secret material.

    Returns:
        An :class:`AuthResult` for the authenticated identity.

    Raises:
        MalformedAuthError: If a required ``X-Amz-*`` parameter is missing or
            unparseable.
        RequestTimeTooSkewed: If the timestamp is outside the ±15-minute window.
        SignatureDoesNotMatch: If the key is unknown/disabled or the signature
            does not match.
    """
    query = request.query
    algorithm = query.get("X-Amz-Algorithm")
    if algorithm != ALGORITHM:
        raise MalformedAuthError(
            f"missing or unsupported X-Amz-Algorithm {algorithm!r}"
        )
    credential = query.get("X-Amz-Credential")
    signed_headers = query.get("X-Amz-SignedHeaders")
    client_signature = query.get("X-Amz-Signature")
    amz_datetime = query.get("X-Amz-Date")
    if not credential:
        raise MalformedAuthError("missing X-Amz-Credential")
    if not signed_headers:
        raise MalformedAuthError("missing X-Amz-SignedHeaders")
    if not client_signature:
        raise MalformedAuthError("missing X-Amz-Signature")
    if not amz_datetime:
        raise MalformedAuthError("missing X-Amz-Date")

    _check_clock_skew(_parse_amz_datetime(amz_datetime), _now_utc())

    scope = _parse_credential(credential)
    stored = await _resolve_secret(secret_provider, scope.access_key_id)
    # Presigned requests sign against the UNSIGNED-PAYLOAD sentinel.
    payload_hash = request.content_sha256 or UNSIGNED_PAYLOAD
    return _finish_verification(
        request,
        scope,
        stored,
        amz_datetime,
        signed_headers,
        payload_hash,
        client_signature,
        exclude_query=frozenset({"X-Amz-Signature"}),
    )


# ---------------------------------------------------------------------------
# Streaming chunk / trailer verification
# ---------------------------------------------------------------------------


def _chunk_string_to_sign(
    amz_datetime: str, scope: str, prev_signature: str, chunk_data: bytes
) -> str:
    """Build the per-chunk string-to-sign for a signed streaming chunk.

    ``AWS4-HMAC-SHA256-PAYLOAD\\n{amz_datetime}\\n{scope}\\n{prev_signature}\\n
    {sha256hex("")}\\n{sha256hex(chunk_data)}`` (requirement 7.5).
    """
    chunk_hash = hashlib.sha256(chunk_data).hexdigest()
    return "\n".join(
        (
            _CHUNK_STRING_TO_SIGN_ALGO,
            amz_datetime,
            scope,
            prev_signature,
            _EMPTY_SHA256_HEX,
            chunk_hash,
        )
    )


def _trailer_string_to_sign(
    amz_datetime: str, scope: str, prev_signature: str, trailer_bytes: bytes
) -> str:
    """Build the string-to-sign for a streaming trailer signature.

    ``AWS4-HMAC-SHA256-TRAILER\\n{amz_datetime}\\n{scope}\\n{prev_signature}\\n
    {sha256hex(trailer_bytes)}`` (requirement 7.9).
    """
    trailer_hash = hashlib.sha256(trailer_bytes).hexdigest()
    return "\n".join(
        (
            _TRAILER_STRING_TO_SIGN_ALGO,
            amz_datetime,
            scope,
            prev_signature,
            trailer_hash,
        )
    )


async def verify_streaming_chunks(
    body_stream: AsyncIterator[StreamingChunk],
    seed_signature: str,
    signing_key: bytes,
    *,
    amz_datetime: str,
    scope: str,
    content_sha256: str,
    trailer: StreamingTrailer | None = None,
) -> StreamingVerification:
    """Verify an aws-chunked streaming body's chunk and trailer signatures.

    Handles the three ``x-amz-content-sha256`` streaming modes
    (requirements 7.4, 7.5, 7.6, 7.7, 7.9):

    * :data:`STREAMING_SIGNED` — each chunk advertises a ``chunk-signature``;
      this function recomputes it from the running signature chain seeded by
      ``seed_signature`` and the chunk payload, and raises on the first
      mismatch, naming the failing chunk (requirement 7.6).
    * :data:`STREAMING_UNSIGNED_TRAILER` — chunks are not individually signed;
      after the stream ends the trailer signature (if any) is verified before
      the trailer values may be trusted (requirement 7.9).
    * :data:`UNSIGNED_PAYLOAD` — the body is accepted without any per-chunk hash
      check; the stream is drained and reported (requirement 7.4).

    Args:
        body_stream: Async iterator yielding chunks in wire order. A final
            zero-length chunk, if present, terminates the chain and is not
            counted.
        seed_signature: The request's seed signature (``AuthResult.seed_signature``).
        signing_key: The derived signing key (``AuthResult.signing_key``).
        amz_datetime: The request's ``X-Amz-Date`` stamp.
        scope: The credential scope string (``CredentialScope.scope``).
        content_sha256: The declared streaming mode sentinel.
        trailer: The trailer block to verify for the trailer variant, or
            ``None``.

    Returns:
        A :class:`StreamingVerification` summarising the chunks verified, the
        final signature in the chain, and whether a trailer was verified.

    Raises:
        MalformedAuthError: If ``content_sha256`` is not a recognised streaming
            sentinel, or a signed chunk omits its signature.
        SignatureDoesNotMatch: If a chunk or trailer signature does not match.
    """
    if content_sha256 not in (
        STREAMING_SIGNED,
        STREAMING_UNSIGNED_TRAILER,
        UNSIGNED_PAYLOAD,
    ):
        raise MalformedAuthError(
            f"unsupported streaming payload mode {content_sha256!r}"
        )

    signed = content_sha256 == STREAMING_SIGNED
    prev_signature = seed_signature
    count = 0

    async for chunk in body_stream:
        if not chunk.data:
            # Terminal zero-length chunk ends the chain.
            if signed and chunk.signature is not None:
                prev_signature = _verify_one_chunk(
                    chunk, prev_signature, signing_key, amz_datetime, scope, count
                )
            break
        if signed:
            prev_signature = _verify_one_chunk(
                chunk, prev_signature, signing_key, amz_datetime, scope, count
            )
        count += 1

    trailer_verified = False
    if content_sha256 == STREAMING_UNSIGNED_TRAILER and trailer is not None:
        trailer_verified = _verify_trailer(
            trailer, prev_signature, signing_key, amz_datetime, scope
        )

    return StreamingVerification(
        chunk_count=count,
        final_signature=prev_signature,
        trailer_verified=trailer_verified,
    )


def _verify_one_chunk(
    chunk: StreamingChunk,
    prev_signature: str,
    signing_key: bytes,
    amz_datetime: str,
    scope: str,
    index: int,
) -> str:
    """Verify a single signed chunk and return its signature for the next link.

    Raises:
        MalformedAuthError: If the chunk omits its advertised signature.
        SignatureDoesNotMatch: If the recomputed signature does not match,
            naming ``index`` (requirement 7.6).
    """
    if chunk.signature is None:
        raise MalformedAuthError(f"chunk {index} missing chunk-signature")
    sts = _chunk_string_to_sign(amz_datetime, scope, prev_signature, chunk.data)
    expected = _compute_signature(signing_key, sts)
    if not hmac.compare_digest(expected, chunk.signature):
        raise SignatureDoesNotMatch(f"chunk {index} signature mismatch")
    return expected


def _verify_trailer(
    trailer: StreamingTrailer,
    prev_signature: str,
    signing_key: bytes,
    amz_datetime: str,
    scope: str,
) -> bool:
    """Verify a streaming trailer signature (requirement 7.9).

    Returns ``True`` when a trailer signature was present and verified, ``False``
    when the trailer carried no signature (an unsigned trailer).

    Raises:
        SignatureDoesNotMatch: If a trailer signature is present but does not
            match.
    """
    if trailer.signature is None:
        return False
    sts = _trailer_string_to_sign(
        amz_datetime, scope, prev_signature, trailer.canonical_bytes
    )
    expected = _compute_signature(signing_key, sts)
    if not hmac.compare_digest(expected, trailer.signature):
        raise SignatureDoesNotMatch("trailer signature mismatch")
    return True


#: Public type alias used by the router to inject the secret lookup as a plain
#: coroutine function in addition to the :class:`SecretProvider` protocol.
SecretProviderFn = Callable[[str], Awaitable[StoredSecret | None]]


__all__ = [
    "ALGORITHM",
    "SERVICE",
    "AWS4_REQUEST",
    "MAX_CLOCK_SKEW_SECONDS",
    "UNSIGNED_PAYLOAD",
    "STREAMING_SIGNED",
    "STREAMING_UNSIGNED_TRAILER",
    "SigV4Error",
    "MalformedAuthError",
    "SignatureDoesNotMatch",
    "RequestTimeTooSkewed",
    "SecretVerificationUnavailableError",
    "SignedRequest",
    "CredentialScope",
    "StoredSecret",
    "AuthResult",
    "StreamingChunk",
    "StreamingTrailer",
    "StreamingVerification",
    "SecretProvider",
    "SecretProviderFn",
    "SecretVerifier",
    "BcryptSecretVerifier",
    "derive_signing_key",
    "verify_header_auth",
    "verify_presigned_auth",
    "verify_streaming_chunks",
]

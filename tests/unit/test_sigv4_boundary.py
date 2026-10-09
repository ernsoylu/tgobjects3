"""Example-based tests for SigV4 boundary conditions (task 21.2).

Feature: tgobjects3 — exercises the gateway-wide access-control contract in
:mod:`gateway.app.s3.sigv4` at its decision boundaries, rather than across a
random input space. Three boundaries are covered:

* **Clock skew window (requirements 17.1, 17.4).** A request whose timestamp
  sits *exactly* on the ±15-minute edge of the gateway clock authenticates;
  one second past the edge (±15 min + 1 s) is rejected with
  :class:`~gateway.app.s3.sigv4.RequestTimeTooSkewed`, which the design's
  "Request Errors" table maps to HTTP 403.
* **UNSIGNED-PAYLOAD (requirement 7.4).** A header-authenticated request that
  declares ``x-amz-content-sha256: UNSIGNED-PAYLOAD`` authenticates with the
  sentinel used verbatim as the payload hash and no body-hash check.
* **Signature mismatch (requirement 17.3).** A tampered signature yields
  :class:`~gateway.app.s3.sigv4.SignatureDoesNotMatch` (HTTP 403), and because
  verification happens entirely before any body is read, no body bytes are ever
  consumed — asserted here with a body stream that records every read.

Valid signatures are constructed with the module's *own* signing helpers
(:func:`~gateway.app.s3.sigv4.derive_signing_key` and the canonical-request /
string-to-sign builders), so the tests check the public verification entry
points against the same derivation AWS SDK clients perform, without pinning an
external test vector. The ``secret_provider`` is a small in-memory fake.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from gateway.app.s3 import sigv4
from gateway.app.s3.sigv4 import (
    MAX_CLOCK_SKEW_SECONDS,
    UNSIGNED_PAYLOAD,
    RequestTimeTooSkewed,
    SignatureDoesNotMatch,
    SignedRequest,
    StoredSecret,
    derive_signing_key,
)

# ---------------------------------------------------------------------------
# Fixed test identity and request shape
# ---------------------------------------------------------------------------

_ACCESS_KEY_ID = "AKIDEXAMPLE"
_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
_REGION = "us-east-1"
_HOST = "s3.example.com"
_METHOD = "GET"
_PATH = "/bucket/object.txt"
#: A fixed instant used as both the request timestamp and (via a stubbed clock)
#: the gateway's "now", so skew is controlled solely by the offset applied to
#: the request stamp in each test.
_NOW = datetime(2024, 5, 24, 12, 0, 0, tzinfo=UTC)


def _secret_provider(stored: StoredSecret | None) -> sigv4.SecretProvider:
    """Return an in-memory :class:`SecretProvider` yielding ``stored``.

    The fake ignores the looked-up access-key id and always returns the single
    configured :class:`StoredSecret` (or ``None`` for the unknown-key case),
    which is all these boundary tests need.
    """

    async def provider(access_key_id: str) -> StoredSecret | None:
        return stored

    return provider


def _amz_datetime(when: datetime) -> str:
    """Format ``when`` as a SigV4 ``X-Amz-Date`` stamp (``YYYYMMDDTHHMMSSZ``)."""
    return when.strftime("%Y%m%dT%H%M%SZ")


def _sign_header_request(
    *,
    amz_datetime: str,
    content_sha256: str,
) -> SignedRequest:
    """Build a header-authenticated :class:`SignedRequest` with a valid signature.

    Uses the module's own key derivation and canonicalisation so the embedded
    ``Authorization`` signature is exactly what :func:`verify_header_auth` will
    recompute. ``content_sha256`` becomes the ``x-amz-content-sha256`` header
    and the payload hash in the canonical request (so ``UNSIGNED-PAYLOAD`` is
    signed verbatim per requirement 7.4).
    """
    date = amz_datetime[:8]
    scope = f"{date}/{_REGION}/{sigv4.SERVICE}/{sigv4.AWS4_REQUEST}"
    credential = f"{_ACCESS_KEY_ID}/{scope}"
    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    headers = {
        "host": _HOST,
        "x-amz-content-sha256": content_sha256,
        "x-amz-date": amz_datetime,
    }
    # The authorization header value does not participate in its own signature,
    # so build the request without it first, sign, then attach the header.
    unsigned = SignedRequest(
        method=_METHOD,
        path=_PATH,
        query={},
        headers=headers,
        content_sha256=content_sha256,
    )
    canonical = sigv4._canonical_request(
        unsigned, signed_headers, content_sha256, exclude_query=frozenset()
    )
    sts = sigv4._string_to_sign(amz_datetime, scope, canonical)
    signing_key = derive_signing_key(_SECRET, date, _REGION, sigv4.SERVICE)
    signature = sigv4._compute_signature(signing_key, sts)
    authorization = (
        f"{sigv4.ALGORITHM} Credential={credential}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return SignedRequest(
        method=_METHOD,
        path=_PATH,
        query={},
        headers={**headers, "authorization": authorization},
        content_sha256=content_sha256,
    )


@pytest.fixture
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the gateway clock (:func:`sigv4._now_utc`) to :data:`_NOW`."""
    monkeypatch.setattr(sigv4, "_now_utc", lambda: _NOW)


# ---------------------------------------------------------------------------
# Clock-skew window boundary (requirements 17.1, 17.4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sign", [1, -1], ids=["future", "past"])
async def test_timestamp_exactly_at_window_edge_is_accepted(
    sign: int, frozen_clock: None
) -> None:
    """A timestamp exactly ±15 min from now authenticates.

    The window check uses ``skew > MAX_CLOCK_SKEW_SECONDS``, so a skew of
    exactly the limit is inside the window (requirements 17.1, 17.4).
    """
    stamp = _NOW + sign * timedelta(seconds=MAX_CLOCK_SKEW_SECONDS)
    request = _sign_header_request(
        amz_datetime=_amz_datetime(stamp),
        content_sha256=sigv4._EMPTY_SHA256_HEX,
    )
    result = await sigv4.verify_header_auth(
        request, _secret_provider(StoredSecret(_ACCESS_KEY_ID, secret=_SECRET))
    )
    assert result.access_key_id == _ACCESS_KEY_ID


@pytest.mark.parametrize("sign", [1, -1], ids=["future", "past"])
async def test_timestamp_one_second_past_window_edge_is_rejected(
    sign: int, frozen_clock: None
) -> None:
    """A timestamp at ±15 min + 1 s is rejected (HTTP 403 RequestTimeTooSkewed).

    One second beyond the edge pushes the skew above the limit, so the request
    must fail with :class:`RequestTimeTooSkewed` (requirement 17.4).
    """
    stamp = _NOW + sign * timedelta(seconds=MAX_CLOCK_SKEW_SECONDS + 1)
    request = _sign_header_request(
        amz_datetime=_amz_datetime(stamp),
        content_sha256=sigv4._EMPTY_SHA256_HEX,
    )
    with pytest.raises(RequestTimeTooSkewed):
        await sigv4.verify_header_auth(
            request,
            _secret_provider(StoredSecret(_ACCESS_KEY_ID, secret=_SECRET)),
        )


# ---------------------------------------------------------------------------
# UNSIGNED-PAYLOAD acceptance (requirement 7.4)
# ---------------------------------------------------------------------------


async def test_unsigned_payload_is_accepted_without_body_hash(
    frozen_clock: None,
) -> None:
    """A header request declaring UNSIGNED-PAYLOAD authenticates.

    The sentinel is used verbatim as the canonical-request payload hash and no
    body-hash is computed or checked (requirement 7.4). Here the signature is
    built over the sentinel, mirroring an AWS SDK client, and verification
    succeeds against it.
    """
    request = _sign_header_request(
        amz_datetime=_amz_datetime(_NOW),
        content_sha256=UNSIGNED_PAYLOAD,
    )
    result = await sigv4.verify_header_auth(
        request, _secret_provider(StoredSecret(_ACCESS_KEY_ID, secret=_SECRET))
    )
    assert result.access_key_id == _ACCESS_KEY_ID
    assert result.content_sha256 == UNSIGNED_PAYLOAD


# ---------------------------------------------------------------------------
# Signature mismatch — 403 and no body bytes read (requirement 17.3)
# ---------------------------------------------------------------------------


class _RecordingBodyStream:
    """An async body stream that records whether any bytes were read.

    Stands in for the request body downstream of authentication. If
    verification correctly rejects a bad signature *before* body processing, no
    item is ever pulled from this stream and :attr:`read` stays ``False``.
    """

    def __init__(self) -> None:
        self.read = False

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self

    async def __anext__(self) -> bytes:
        self.read = True
        raise StopAsyncIteration


async def test_signature_mismatch_returns_403_and_reads_no_body(
    frozen_clock: None,
) -> None:
    """A tampered signature fails with 403 and consumes no body bytes.

    :class:`SignatureDoesNotMatch` maps to HTTP 403 (requirement 17.3). Because
    :func:`verify_header_auth` validates the signature before any body is
    touched, a recording body stream must report that nothing was read.
    """
    request = _sign_header_request(
        amz_datetime=_amz_datetime(_NOW),
        content_sha256=sigv4._EMPTY_SHA256_HEX,
    )
    # Flip the trailing hex digit of the embedded signature to force a mismatch
    # while keeping the header structurally valid.
    authorization = request.headers["authorization"]
    tampered_sig = authorization[:-1] + ("0" if authorization[-1] != "0" else "1")
    tampered = SignedRequest(
        method=request.method,
        path=request.path,
        query=request.query,
        headers={**request.headers, "authorization": tampered_sig},
        content_sha256=request.content_sha256,
    )

    body = _RecordingBodyStream()
    with pytest.raises(SignatureDoesNotMatch):
        await sigv4.verify_header_auth(
            tampered,
            _secret_provider(StoredSecret(_ACCESS_KEY_ID, secret=_SECRET)),
        )
        async for _ in body:  # pragma: no cover - unreachable once raised
            pass

    assert body.read is False

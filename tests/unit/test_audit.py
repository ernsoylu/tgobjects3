"""Unit and property tests for :mod:`gateway.app.s3.audit`.

Covers the audit-log middleware's three guarantees:

* an entry is recorded per S3 request with all mandated fields
  (requirement 18.5);
* credential/signature/token material is redacted (requirement 18.6);
* an audit-write failure is contained — the response is unaffected and the
  ``audit_log_write_failures_total`` counter is incremented (requirement 18.8).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from gateway.app.s3.audit import (
    REDACTED_PLACEHOLDER,
    AuditEntry,
    AuditLogMiddleware,
    AuditSink,
    InProcessFailureCounter,
    LoggingAuditSink,
    redact,
    redact_headers,
)
from hypothesis import given
from hypothesis import strategies as st
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class _RecordingSink:
    """An :class:`AuditSink` that stores every entry it is given."""

    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []

    async def write(self, entry: AuditEntry) -> None:
        self.entries.append(entry)


class _FailingSink:
    """An :class:`AuditSink` whose write always raises."""

    async def write(self, entry: AuditEntry) -> None:
        raise RuntimeError("audit backend is down")


def _http_scope(method: str = "GET", path: str = "/bucket/key") -> Scope:
    """Build a minimal ASGI HTTP scope for the middleware."""
    return {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [],
        "query_string": b"",
    }


def _app_returning(status: int) -> ASGIApp:
    """Return a trivial ASGI app that emits a response with ``status``."""

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [],
            }
        )
        await send({"type": "http.response.body", "body": b"ok"})

    return app


async def _drive(middleware: AuditLogMiddleware, scope: Scope) -> list[Message]:
    """Run the middleware once, collecting the messages it sends downstream."""
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    await middleware(scope, receive, send)
    return sent


# ---------------------------------------------------------------------------
# Redaction (requirement 18.6)
# ---------------------------------------------------------------------------


def test_redact_masks_sigv4_signature() -> None:
    authz = (
        "AWS4-HMAC-SHA256 Credential=AKID/20240101/us-east-1/s3/aws4_request, "
        "SignedHeaders=host, Signature=deadbeefcafef00d"
    )
    out = redact(authz)
    assert "deadbeefcafef00d" not in out
    assert REDACTED_PLACEHOLDER in out
    # Non-secret parts survive.
    assert "Credential=AKID/20240101/us-east-1/s3/aws4_request" in out


def test_redact_masks_presigned_query_signature_and_token() -> None:
    qs = "X-Amz-Signature=abc123&X-Amz-Security-Token=sekrit&foo=bar"
    out = redact(qs)
    assert "abc123" not in out
    assert "sekrit" not in out
    assert "foo=bar" in out


def test_redact_leaves_plain_text_unchanged() -> None:
    assert redact("PutObject") == "PutObject"


def test_redact_headers_masks_secret_headers_fully() -> None:
    headers = {
        "Authorization": "AWS4-HMAC-SHA256 Credential=AKID, Signature=xyz",
        "X-Amz-Security-Token": "token-value",
        "Host": "example.com",
    }
    masked = redact_headers(headers)
    assert masked["Authorization"] == REDACTED_PLACEHOLDER
    assert masked["X-Amz-Security-Token"] == REDACTED_PLACEHOLDER
    assert masked["Host"] == "example.com"


@given(st.text(min_size=0, max_size=64))
def test_redact_never_emits_a_raw_signature_value(secret: str) -> None:
    """**Validates: Requirement 18.6** — a signature value is always masked.

    For any token value, embedding it after ``Signature=`` and redacting must
    remove the raw value (unless the value itself contains a delimiter, which
    terminates the token and is therefore not part of the secret run).
    """
    probe = f"Signature={secret}&tail=1"
    out = redact(probe)
    # The raw secret run (up to the first delimiter) must be replaced by the
    # placeholder; the token now reads ``Signature=[REDACTED]``.
    run = secret.split("&")[0].split(",")[0].split(" ")[0].split("\t")[0]
    assert f"Signature={REDACTED_PLACEHOLDER}" in out
    # The original value never survives verbatim as the token's value.
    assert f"Signature={run}&" not in out


# ---------------------------------------------------------------------------
# Entry rendering (requirement 18.5)
# ---------------------------------------------------------------------------


def test_entry_to_log_fields_has_all_required_fields_iso_timestamp() -> None:
    entry = AuditEntry(
        timestamp=datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC),
        access_key_id="AKID",
        operation="PutObject",
        bucket="b",
        key="k",
        status=200,
        latency_ms=12,
    )
    fields = entry.to_log_fields()
    assert set(fields) == {
        "timestamp",
        "access_key_id",
        "operation",
        "bucket",
        "key",
        "status",
        "latency_ms",
    }
    assert fields["timestamp"] == "2024-01-02T03:04:05+00:00"
    assert fields["access_key_id"] == "AKID"
    assert fields["status"] == 200
    assert fields["latency_ms"] == 12


def test_entry_to_log_fields_redacts_string_fields() -> None:
    entry = AuditEntry(
        timestamp=datetime.now(UTC),
        access_key_id="AKID",
        operation="Signature=leak",
        bucket=None,
        key=None,
        status=200,
        latency_ms=1,
    )
    fields = entry.to_log_fields()
    assert "leak" not in str(fields["operation"])
    assert REDACTED_PLACEHOLDER in str(fields["operation"])


# ---------------------------------------------------------------------------
# Middleware behaviour (requirements 18.5, 18.8)
# ---------------------------------------------------------------------------


async def test_middleware_records_entry_per_request() -> None:
    sink = _RecordingSink()
    middleware = AuditLogMiddleware(
        _app_returning(204),
        sink=sink,
        extractor=lambda scope: ("AKID", "DeleteObject", "mybucket", "mykey"),
    )
    sent = await _drive(middleware, _http_scope("DELETE", "/mybucket/mykey"))

    # Downstream response passes through unchanged.
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 204

    assert len(sink.entries) == 1
    entry = sink.entries[0]
    assert entry.access_key_id == "AKID"
    assert entry.operation == "DeleteObject"
    assert entry.bucket == "mybucket"
    assert entry.key == "mykey"
    assert entry.status == 204
    assert entry.latency_ms >= 0


async def test_middleware_defaults_identity_fields_to_none() -> None:
    sink = _RecordingSink()
    middleware = AuditLogMiddleware(_app_returning(200), sink=sink)
    await _drive(middleware, _http_scope())
    entry = sink.entries[0]
    assert entry.access_key_id is None
    assert entry.operation is None
    assert entry.bucket is None
    assert entry.key is None
    assert entry.status == 200


async def test_middleware_passes_through_non_http_scopes() -> None:
    sink = _RecordingSink()
    seen: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])

    middleware = AuditLogMiddleware(app, sink=sink)

    async def receive() -> Message:
        return {"type": "lifespan.startup"}

    async def send(message: Message) -> None:
        pass

    await middleware({"type": "lifespan"}, receive, send)
    assert seen == ["lifespan"]
    assert sink.entries == []


async def test_audit_write_failure_does_not_fail_request_and_counts() -> None:
    counter = InProcessFailureCounter()
    middleware = AuditLogMiddleware(
        _app_returning(200),
        sink=_FailingSink(),
        failure_counter=counter,
    )
    # The request completes normally despite the failing sink.
    sent = await _drive(middleware, _http_scope())
    assert sent[0]["status"] == 200
    assert any(m["type"] == "http.response.body" for m in sent)
    # The failure is counted (requirement 18.8).
    assert counter.value == 1.0


async def test_logging_sink_emits_record(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sink = LoggingAuditSink()
    entry = AuditEntry(
        timestamp=datetime.now(UTC),
        access_key_id="AKID",
        operation="GetObject",
        bucket="b",
        key="k",
        status=200,
        latency_ms=3,
    )
    with caplog.at_level("INFO", logger="gateway.audit"):
        await sink.write(entry)
    assert any(r.message == "s3.audit" for r in caplog.records)


def test_in_process_counter_accumulates() -> None:
    counter = InProcessFailureCounter()
    assert counter.value == 0.0
    counter.inc()
    counter.inc(2.0)
    assert counter.value == 3.0


def test_recording_sink_satisfies_audit_sink_protocol() -> None:
    assert isinstance(_RecordingSink(), AuditSink)

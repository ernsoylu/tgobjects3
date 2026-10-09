"""Audit-log middleware for the S3-compatible HTTP surface.

Every S3 API request is recorded to an audit log, exactly once, after the
response status is known (requirement 18.5). Each entry carries the fields the
design mandates:

* ``timestamp`` — ISO 8601 UTC, when the request was received;
* ``access_key_id`` — the authenticated client's access-key id, or ``None``
  when the request failed authentication before an identity was established;
* ``operation`` — the S3 operation name (e.g. ``PutObject``);
* ``bucket`` / ``key`` — the resolved target, each ``None`` when the request
  does not address one;
* ``status`` — the HTTP response status code;
* ``latency_ms`` — wall-clock request latency in milliseconds.

Credential redaction (requirement 18.6)
---------------------------------------
No entry may leak secret material. :func:`redact` replaces every credential
value, encryption key and session token with the fixed placeholder
:data:`REDACTED_PLACEHOLDER`. The access-key *id* is not a secret and is kept
verbatim (it is the whole point of the audit trail); the SigV4 *signature*,
``X-Amz-Security-Token`` and any ``Authorization`` material are redacted. The
entry this middleware builds deliberately carries only the id and never the
signature, so redaction is applied defensively to the operation/bucket/key
string fields as well, in case a sibling task later threads request headers
through the sink.

Write-failure containment (requirement 18.8, design "Audit Log Write Failure")
------------------------------------------------------------------------------
An audit write must never affect the S3 request. The middleware runs the sink
*after* the downstream response has been produced, and wraps the write in a
guard that swallows every exception, logs it, and increments the
``audit_log_write_failures_total`` counter. The client always receives the
unmodified downstream response; audit failures are invisible to S3 clients.

Metrics seam
------------
A Prometheus counter named ``audit_log_write_failures_total`` is the contract
with the sibling ``/metrics`` task (task 33.2). **No ``prometheus_client``
distribution is pinned in ``pyproject.toml`` or importable in the current
environment**, so — mirroring the approach taken in
:mod:`gateway.app.s3.sigv4` for the unmet bcrypt dependency — this module does
not add an unpinned dependency. Instead it depends on a small
:class:`FailureCounter` protocol and ships a dependency-free
:class:`InProcessFailureCounter` default. When task 33.2 lands a real
Prometheus counter it can be injected through :class:`AuditLogMiddleware`
without changing this module.

Audit sink seam
---------------
The destination of audit entries is abstracted behind the :class:`AuditSink`
protocol. There is **no ``audit_log`` table in**
:mod:`gateway.app.db.models` **at the time of writing**, so the default
:class:`LoggingAuditSink` emits each entry through the standard
:mod:`logging` module. A durable, DB-backed audit table is a follow-up: once an
``audit_log`` model exists, a sibling task supplies a database-backed
:class:`AuditSink` and injects it here, leaving the middleware unchanged.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from threading import Lock
from typing import Protocol, runtime_checkable

from starlette.types import ASGIApp, Message, Receive, Scope, Send

__all__ = [
    "REDACTED_PLACEHOLDER",
    "AuditEntry",
    "AuditSink",
    "LoggingAuditSink",
    "FailureCounter",
    "InProcessFailureCounter",
    "AuditLogMiddleware",
    "redact",
    "redact_headers",
]

#: Logger used by :class:`LoggingAuditSink` and for audit-write failures.
_LOGGER = logging.getLogger("gateway.audit")

#: Fixed placeholder substituted for any redacted credential value, encryption
#: key or session token (requirement 18.6).
REDACTED_PLACEHOLDER = "[REDACTED]"

#: Name of the Prometheus counter incremented on an audit-write failure
#: (requirement 18.8). This string is the contract with the ``/metrics`` task.
AUDIT_WRITE_FAILURES_METRIC = "audit_log_write_failures_total"

#: Lower-cased request header names whose values are secret material and must
#: never reach an audit entry (requirement 18.6).
_SECRET_HEADER_NAMES: frozenset[str] = frozenset(
    {
        "authorization",
        "x-amz-security-token",
        "x-amz-content-sha256",
        "cookie",
    }
)

#: Lower-cased query-parameter names carrying SigV4 secret material in presigned
#: requests; their values are redacted before an entry is built.
_SECRET_QUERY_NAMES: frozenset[str] = frozenset(
    {
        "x-amz-signature",
        "x-amz-credential",
        "x-amz-security-token",
    }
)


# ---------------------------------------------------------------------------
# Audit entry value object
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditEntry:
    """One audit-log record for a single S3 API request.

    A frozen value object (per the project's "typed value objects at
    boundaries" convention) carrying exactly the fields requirement 18.5
    mandates. Instances are produced by :class:`AuditLogMiddleware` once the
    downstream response status is known and handed to an :class:`AuditSink`.

    Attributes:
        timestamp: When the request was received, as an aware UTC datetime;
            rendered ISO 8601 by :meth:`to_log_fields`.
        access_key_id: The authenticated access-key id, or ``None`` when no
            identity was established (e.g. an authentication failure). Not a
            secret, so it is recorded verbatim.
        operation: The S3 operation name, or ``None`` if the request could not
            be classified. Never a secret.
        bucket: The target bucket name, or ``None`` for a service-root request.
        key: The target object key, or ``None`` for a bucket-level request.
        status: The HTTP response status code returned to the client.
        latency_ms: Wall-clock request latency in whole milliseconds.
    """

    timestamp: datetime
    access_key_id: str | None
    operation: str | None
    bucket: str | None
    key: str | None
    status: int
    latency_ms: int

    def to_log_fields(self) -> dict[str, object]:
        """Render the entry as a flat mapping of JSON-friendly audit fields.

        The timestamp is formatted ISO 8601 in UTC (requirement 18.5). String
        fields are defensively passed through :func:`redact` so no secret
        pattern can survive even if a caller populated them from request data.
        """
        return {
            "timestamp": self.timestamp.astimezone(UTC).isoformat(),
            "access_key_id": self.access_key_id,
            "operation": redact(self.operation) if self.operation else None,
            "bucket": redact(self.bucket) if self.bucket else None,
            "key": redact(self.key) if self.key else None,
            "status": self.status,
            "latency_ms": self.latency_ms,
        }


# ---------------------------------------------------------------------------
# Redaction (requirement 18.6)
# ---------------------------------------------------------------------------


def redact(value: str) -> str:
    """Return ``value`` with any embedded SigV4 secret material masked.

    Replaces the values of SigV4 credential/signature/security-token tokens
    with :data:`REDACTED_PLACEHOLDER` (requirement 18.6) while leaving the
    surrounding structure intact so the entry stays legible. A value with no
    secret token is returned unchanged. The access-key *id* is not a secret and
    is never matched here.
    """
    out = value
    for token in ("Signature=", "X-Amz-Signature=", "X-Amz-Security-Token="):
        out = _mask_token(out, token)
    return out


def _mask_token(value: str, token: str) -> str:
    """Mask the run of characters following ``token`` up to the next delimiter.

    Case-insensitive on ``token``. The masked run ends at the first ``&``,
    ``,``, whitespace, or end of string — the delimiters SigV4 uses between the
    ``Authorization`` header's comma-separated parts and a query string's
    ``&``-separated parameters.
    """
    lowered = value.lower()
    token_l = token.lower()
    result = value
    search_from = 0
    while True:
        idx = lowered.find(token_l, search_from)
        if idx == -1:
            return result
        start = idx + len(token)
        end = start
        while end < len(result) and result[end] not in "&, \t":
            end += 1
        result = result[:start] + REDACTED_PLACEHOLDER + result[end:]
        lowered = result.lower()
        search_from = start + len(REDACTED_PLACEHOLDER)


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Return a copy of ``headers`` with secret headers fully masked.

    Any header whose lower-cased name is in :data:`_SECRET_HEADER_NAMES` has its
    entire value replaced with :data:`REDACTED_PLACEHOLDER` (requirement 18.6).
    Other header values are passed through :func:`redact` so an embedded
    signature is masked even in an unexpected header. Provided for a future
    DB-backed sink that may wish to persist request headers; the middleware's
    default entry does not carry raw headers.
    """
    masked: dict[str, str] = {}
    for name, value in headers.items():
        if name.lower() in _SECRET_HEADER_NAMES:
            masked[name] = REDACTED_PLACEHOLDER
        else:
            masked[name] = redact(value)
    return masked


# ---------------------------------------------------------------------------
# Metrics seam
# ---------------------------------------------------------------------------


@runtime_checkable
class FailureCounter(Protocol):
    """A monotonic counter for audit-write failures (requirement 18.8).

    The seam over the ``audit_log_write_failures_total`` Prometheus counter.
    The sibling ``/metrics`` task (33.2) can supply a real
    ``prometheus_client.Counter`` — whose ``.inc()`` method satisfies this
    protocol structurally — and inject it into :class:`AuditLogMiddleware`
    without this module importing ``prometheus_client`` (which is not pinned in
    ``pyproject.toml``).
    """

    def inc(self, amount: float = 1.0) -> None:
        """Increment the counter by ``amount`` (defaulting to one)."""
        ...


class InProcessFailureCounter:
    """Thread-safe, dependency-free default :class:`FailureCounter`.

    Used when no Prometheus counter is injected, because no
    ``prometheus_client`` distribution is pinned in ``pyproject.toml`` or
    importable in the current environment. Keeps a simple in-process total so
    audit-write failures are observable (and unit-testable) without adding an
    unpinned dependency; the ``.inc()`` signature matches
    ``prometheus_client.Counter`` so the real counter is a drop-in replacement.
    """

    def __init__(self) -> None:
        """Start the counter at zero."""
        self._value = 0.0
        self._lock = Lock()

    def inc(self, amount: float = 1.0) -> None:
        """Atomically add ``amount`` to the running total."""
        with self._lock:
            self._value += amount

    @property
    def value(self) -> float:
        """Return the current total (primarily for tests and introspection)."""
        with self._lock:
            return self._value


# ---------------------------------------------------------------------------
# Audit sink seam
# ---------------------------------------------------------------------------


@runtime_checkable
class AuditSink(Protocol):
    """Destination for completed :class:`AuditEntry` records.

    Abstracts *where* audit entries go so the middleware does not depend on any
    particular backend. The default :class:`LoggingAuditSink` writes through the
    standard library logger; a future DB-backed sink (once an ``audit_log``
    table exists in :mod:`gateway.app.db.models`) implements the same protocol
    and is injected without changing the middleware. An implementation may raise
    on failure — the middleware contains the error per requirement 18.8.
    """

    async def write(self, entry: AuditEntry) -> None:
        """Persist ``entry``; may raise, which the middleware will contain."""
        ...


class LoggingAuditSink:
    """Default :class:`AuditSink` emitting entries via :mod:`logging`.

    Writes one structured ``INFO`` record per S3 request through the
    ``gateway.audit`` logger, with the mandated fields attached as ``extra``
    (requirement 18.5) and credential values redacted (requirement 18.6).

    A durable audit store is a follow-up: there is no ``audit_log`` table in
    :mod:`gateway.app.db.models` yet, so this logging sink is the interim
    destination. When that table lands, a database-backed sink replaces this one
    behind the :class:`AuditSink` seam.
    """

    def __init__(self, logger: logging.Logger | None = None) -> None:
        """Use ``logger`` for output, defaulting to the ``gateway.audit`` logger."""
        self._logger = logger or _LOGGER

    async def write(self, entry: AuditEntry) -> None:
        """Emit ``entry`` as a single structured log record."""
        self._logger.info("s3.audit", extra={"audit": entry.to_log_fields()})


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

#: Signature of the hook that extracts the audit identity/target for a request.
#: Returns ``(access_key_id, operation, bucket, key)``, any element ``None``
#: when unknown. Injected so the middleware stays decoupled from the router's
#: internal state objects; the application wiring supplies an implementation
#: that reads from the authenticated request scope.
RequestContextExtractor = Callable[
    [Scope], tuple[str | None, str | None, str | None, str | None]
]


def _now_utc() -> datetime:
    """Return the current time as an aware UTC datetime (injection seam)."""
    return datetime.now(UTC)


class AuditLogMiddleware:
    """ASGI middleware recording one audit entry per S3 request.

    Wraps the S3 ASGI application. For every HTTP request it captures the
    receive time, lets the downstream app produce its response while observing
    the final status code, then builds an :class:`AuditEntry` and hands it to
    the configured :class:`AuditSink` (requirement 18.5). Credential values are
    redacted before the entry is written (requirement 18.6).

    The audit write is strictly best-effort: it runs only after the response has
    started streaming to the client and is wrapped in a guard that swallows any
    exception, logs it, and increments the ``audit_log_write_failures_total``
    counter (requirement 18.8, design "Audit Log Write Failure"). A failing sink
    therefore never changes the response an S3 client receives.

    Args:
        app: The downstream ASGI application (the S3 router app).
        sink: Where to send audit entries. Defaults to :class:`LoggingAuditSink`.
        failure_counter: Counter incremented on an audit-write failure. Defaults
            to a process-local :class:`InProcessFailureCounter`; inject a
            Prometheus counter here once task 33.2 provides one.
        extractor: Optional hook returning ``(access_key_id, operation, bucket,
            key)`` for the request from its ASGI scope. When absent, those
            fields are recorded as ``None`` (the status/latency are always
            captured).
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        sink: AuditSink | None = None,
        failure_counter: FailureCounter | None = None,
        extractor: RequestContextExtractor | None = None,
    ) -> None:
        """Store the downstream app and the injected audit collaborators."""
        self._app = app
        self._sink: AuditSink = sink or LoggingAuditSink()
        self._failure_counter: FailureCounter = (
            failure_counter or InProcessFailureCounter()
        )
        self._extractor = extractor

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        """Dispatch one ASGI event, auditing HTTP requests after they complete.

        Non-HTTP scopes (lifespan, websocket) are passed straight through so the
        middleware only observes S3 API requests.
        """
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        started = time.perf_counter()
        received_at = _now_utc()
        status_holder: dict[str, int] = {}

        async def send_wrapper(message: Message) -> None:
            """Capture the response status, then forward the event unchanged."""
            if message["type"] == "http.response.start":
                status_holder["status"] = int(message["status"])
            await send(message)

        await self._app(scope, receive, send_wrapper)

        latency_ms = int((time.perf_counter() - started) * 1000)
        status = status_holder.get("status", 0)
        await self._record(scope, received_at, status, latency_ms)

    async def _record(
        self, scope: Scope, received_at: datetime, status: int, latency_ms: int
    ) -> None:
        """Build and write the audit entry, containing any failure.

        Any exception raised while building the entry *or* writing it is
        swallowed: it is logged and counted via
        ``audit_log_write_failures_total`` but never propagated, so the S3
        request's outcome is unaffected (requirement 18.8).
        """
        try:
            entry = self._build_entry(scope, received_at, status, latency_ms)
            await self._sink.write(entry)
        except Exception:  # noqa: BLE001 - audit failures must never surface
            self._on_write_failure()

    def _build_entry(
        self, scope: Scope, received_at: datetime, status: int, latency_ms: int
    ) -> AuditEntry:
        """Assemble the :class:`AuditEntry` for a completed request.

        Uses the injected extractor for the identity/target fields when present,
        falling back to ``None`` for each. The entry's own
        :meth:`AuditEntry.to_log_fields` applies redaction when serialised.
        """
        access_key_id: str | None = None
        operation: str | None = None
        bucket: str | None = None
        key: str | None = None
        if self._extractor is not None:
            access_key_id, operation, bucket, key = self._extractor(scope)
        return AuditEntry(
            timestamp=received_at,
            access_key_id=access_key_id,
            operation=operation,
            bucket=bucket,
            key=key,
            status=status,
            latency_ms=latency_ms,
        )

    def _on_write_failure(self) -> None:
        """Log and count an audit-write failure without re-raising.

        Increments ``audit_log_write_failures_total`` (requirement 18.8). The
        counter increment is itself guarded so a broken counter cannot turn an
        audit failure into a request failure.
        """
        with contextlib.suppress(Exception):
            # Never let a broken metrics backend turn an audit failure into a
            # request failure.
            self._failure_counter.inc()
        _LOGGER.warning("audit log write failed", exc_info=True)


def _coerce_entry_fields(entry: AuditEntry) -> AuditEntry:
    """Return ``entry`` with its string fields redacted in place.

    A convenience for a DB-backed sink that persists the structured fields
    directly rather than through :meth:`AuditEntry.to_log_fields`; the logging
    sink does not need it. Kept here so redaction has a single home.
    """
    return replace(
        entry,
        operation=redact(entry.operation) if entry.operation else None,
        bucket=redact(entry.bucket) if entry.bucket else None,
        key=redact(entry.key) if entry.key else None,
    )

"""Prometheus metrics endpoint on the admin port (requirement 18.1).

The gateway exposes a Prometheus scrape target at ``GET /metrics`` on the admin
port so operators can monitor the system (requirement 18.1; design "Admin API:
GET /metrics"). The design acceptance test requires only that the endpoint
return *valid Prometheus text exposition format*, so this module's job is twofold:

1. own a small, process-local **metrics registry** of the gateway's counters and
   gauges, and
2. render that registry into the Prometheus text format for the HTTP response.

Why a hand-rolled registry (unmet ``prometheus_client`` dependency)
-------------------------------------------------------------------
The idiomatic implementation would use ``prometheus_client`` (a
``CollectorRegistry`` plus ``Counter``/``Gauge`` and ``generate_latest``).
**No ``prometheus_client`` distribution is pinned in ``pyproject.toml`` or
importable in the current environment** — mirroring the unmet ``bcrypt``
dependency handled in :mod:`gateway.app.s3.sigv4` and the unmet counter
dependency noted in :mod:`gateway.app.s3.audit`. Rather than add an unpinned
dependency (which the tech conventions forbid — versions are pinned
deliberately), this module ships a dependency-free registry that renders the
text exposition format by hand. The format is small and stable, so this is a
faithful interim implementation; when ``prometheus_client`` is later pinned, the
registry can be swapped for a ``CollectorRegistry`` behind the same
:func:`create_metrics_router` seam without touching callers.

The hand-rolled :class:`Counter` deliberately exposes the same ``.inc()``
signature as ``prometheus_client.Counter`` and therefore satisfies the
:class:`gateway.app.s3.audit.FailureCounter` protocol structurally. This is the
contract the audit middleware (task 33.3) depends on: ``main.py`` constructs one
:class:`MetricsRegistry`, injects its ``audit_log_write_failures_total`` counter
into :class:`~gateway.app.s3.audit.AuditLogMiddleware`, and mounts
:func:`create_metrics_router` for the same registry — so the counter the audit
path increments is the exact instance ``/metrics`` reports.

Exposed metrics
---------------
The task names an initial set (requirement 18.1). This module seeds the registry
with them so the scrape target is populated from first boot:

* ``audit_log_write_failures_total`` — counter; audit-write failures
  (requirement 18.8), incremented by the audit middleware.
* ``record_auth_failure_total`` — counter; SigV4 authentication failures.
* ``upload_job_state`` — gauge, labelled by ``state``; number of upload jobs in
  each lifecycle state.
* ``staging_reserved_bytes`` — gauge; bytes currently reserved in the staging
  area.
* ``cache_size_bytes`` — gauge; on-disk record-cache size in bytes.

The owners of those subsystems (auth, jobs, staging, cache) hold the registry
and update their metrics; this module only defines, stores, and renders them.
"""

from __future__ import annotations

from threading import Lock

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

__all__ = [
    "CONTENT_TYPE_LATEST",
    "METRIC_AUDIT_LOG_WRITE_FAILURES",
    "METRIC_CACHE_SIZE_BYTES",
    "METRIC_RECORD_AUTH_FAILURE",
    "METRIC_STAGING_RESERVED_BYTES",
    "METRIC_UPLOAD_JOB_STATE",
    "Counter",
    "Gauge",
    "MetricsRegistry",
    "create_metrics_router",
]

#: Content type for the Prometheus text exposition format, version 0.0.4. This
#: is the exact media type ``prometheus_client.generate_latest`` emits, so a
#: later swap to the real client keeps the response header stable.
CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"

#: Canonical metric names (requirement 18.1). Centralised so the audit path and
#: other subsystems reference the same strings the registry renders, and so a
#: typo cannot silently create a second, unscraped metric.
METRIC_AUDIT_LOG_WRITE_FAILURES = "audit_log_write_failures_total"
METRIC_RECORD_AUTH_FAILURE = "record_auth_failure_total"
METRIC_UPLOAD_JOB_STATE = "upload_job_state"
METRIC_STAGING_RESERVED_BYTES = "staging_reserved_bytes"
METRIC_CACHE_SIZE_BYTES = "cache_size_bytes"


def _escape_label_value(value: str) -> str:
    """Escape a label value per the Prometheus text exposition format.

    Backslash, double-quote and newline are the three characters the format
    reserves inside a label value; each is backslash-escaped. See the exposition
    format spec that ``prometheus_client`` implements.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _format_number(value: float) -> str:
    """Render ``value`` as a Prometheus sample value.

    Integers are emitted without a trailing ``.0`` (matching the convention of
    ``prometheus_client`` for whole counts), while fractional values keep their
    ``repr`` so no precision is lost.
    """
    if value == int(value):
        return str(int(value))
    return repr(value)


class Counter:
    """A monotonically increasing metric value (Prometheus ``counter``).

    Thread-safe and dependency-free. The ``.inc()`` signature matches
    ``prometheus_client.Counter`` so an instance satisfies the
    :class:`gateway.app.s3.audit.FailureCounter` protocol structurally and can
    be injected into the audit middleware as the real
    ``audit_log_write_failures_total`` counter (requirement 18.8).
    """

    def __init__(self, name: str, documentation: str) -> None:
        """Create a counter named ``name`` with help text ``documentation``."""
        self._name = name
        self._documentation = documentation
        self._value = 0.0
        self._lock = Lock()

    @property
    def name(self) -> str:
        """Return the metric name."""
        return self._name

    def inc(self, amount: float = 1.0) -> None:
        """Atomically add ``amount`` to the counter.

        Args:
            amount: The non-negative increment; defaults to one. A negative
                ``amount`` is rejected because a counter must not decrease.

        Raises:
            ValueError: If ``amount`` is negative.
        """
        if amount < 0:
            raise ValueError("counter increments must be non-negative")
        with self._lock:
            self._value += amount

    @property
    def value(self) -> float:
        """Return the current total (for tests and rendering)."""
        with self._lock:
            return self._value

    def render(self) -> list[str]:
        """Render this counter as Prometheus text lines (``# HELP``/``# TYPE``/sample)."""
        return [
            f"# HELP {self._name} {self._documentation}",
            f"# TYPE {self._name} counter",
            f"{self._name} {_format_number(self.value)}",
        ]


class Gauge:
    """A metric value that can go up or down (Prometheus ``gauge``).

    Thread-safe and dependency-free. Supports an optional single label
    dimension (e.g. ``upload_job_state`` labelled by ``state``); when a label is
    configured, each distinct label value is tracked as its own time series,
    matching how ``prometheus_client`` renders a labelled gauge.
    """

    def __init__(
        self, name: str, documentation: str, *, labelname: str | None = None
    ) -> None:
        """Create a gauge; pass ``labelname`` to make it a single-label series."""
        self._name = name
        self._documentation = documentation
        self._labelname = labelname
        # Unlabelled gauges use the empty-string key; labelled gauges key by
        # the (escaped on render) label value.
        self._values: dict[str | None, float] = {}
        self._lock = Lock()

    @property
    def name(self) -> str:
        """Return the metric name."""
        return self._name

    def set(self, value: float, *, label: str | None = None) -> None:
        """Set the gauge (or the series for ``label``) to ``value``.

        Args:
            value: The new value for the series.
            label: The label value selecting the series. Required when the gauge
                was created with a ``labelname``; must be ``None`` otherwise.

        Raises:
            ValueError: If ``label`` is inconsistent with the gauge's label
                configuration.
        """
        self._check_label(label)
        with self._lock:
            self._values[label] = value

    def inc(self, amount: float = 1.0, *, label: str | None = None) -> None:
        """Add ``amount`` to the gauge (or the series for ``label``)."""
        self._check_label(label)
        with self._lock:
            self._values[label] = self._values.get(label, 0.0) + amount

    def dec(self, amount: float = 1.0, *, label: str | None = None) -> None:
        """Subtract ``amount`` from the gauge (or the series for ``label``)."""
        self.inc(-amount, label=label)

    def value(self, *, label: str | None = None) -> float:
        """Return the current value of the series selected by ``label``."""
        self._check_label(label)
        with self._lock:
            return self._values.get(label, 0.0)

    def _check_label(self, label: str | None) -> None:
        """Validate ``label`` against this gauge's label configuration."""
        if self._labelname is None and label is not None:
            raise ValueError(f"gauge {self._name!r} takes no label")
        if self._labelname is not None and label is None:
            raise ValueError(
                f"gauge {self._name!r} requires label {self._labelname!r}"
            )

    def render(self) -> list[str]:
        """Render this gauge as Prometheus text lines.

        An unlabelled gauge with no value set is rendered as ``0`` so the series
        is always present for scrapers. A labelled gauge renders one sample line
        per observed label value, in sorted order for deterministic output.
        """
        lines = [
            f"# HELP {self._name} {self._documentation}",
            f"# TYPE {self._name} gauge",
        ]
        with self._lock:
            snapshot = dict(self._values)
        if self._labelname is None:
            value = snapshot.get(None, 0.0)
            lines.append(f"{self._name} {_format_number(value)}")
            return lines
        for label_value in sorted(k for k in snapshot if k is not None):
            escaped = _escape_label_value(label_value)
            sample = _format_number(snapshot[label_value])
            lines.append(
                f'{self._name}{{{self._labelname}="{escaped}"}} {sample}'
            )
        return lines


class MetricsRegistry:
    """The gateway's process-local collection of counters and gauges.

    One registry instance is created in ``main.py`` and shared across the
    subsystems that own metrics. It seeds the metric set named by the task
    (requirement 18.1) so the ``/metrics`` scrape target is populated from first
    boot, and exposes typed accessors for the collaborators that update them —
    most importantly :attr:`audit_log_write_failures`, which is injected into the
    audit middleware so the increment path and the ``/metrics`` read path share
    one counter instance.

    This stands in for a ``prometheus_client.CollectorRegistry``; the rendering
    contract (:meth:`render`) produces the same text exposition format
    ``generate_latest`` would, so a later migration is transparent to callers.
    """

    def __init__(self) -> None:
        """Create a registry pre-populated with the requirement 18.1 metrics."""
        self.audit_log_write_failures = Counter(
            METRIC_AUDIT_LOG_WRITE_FAILURES,
            "Total audit log write failures.",
        )
        self.record_auth_failure = Counter(
            METRIC_RECORD_AUTH_FAILURE,
            "Total SigV4 authentication failures.",
        )
        self.upload_job_state = Gauge(
            METRIC_UPLOAD_JOB_STATE,
            "Number of upload jobs in each lifecycle state.",
            labelname="state",
        )
        self.staging_reserved_bytes = Gauge(
            METRIC_STAGING_RESERVED_BYTES,
            "Bytes currently reserved in the staging area.",
        )
        self.cache_size_bytes = Gauge(
            METRIC_CACHE_SIZE_BYTES,
            "On-disk record cache size in bytes.",
        )
        # Registration order is also the render order, giving deterministic
        # output for tests and diffs.
        self._collectors: tuple[Counter | Gauge, ...] = (
            self.audit_log_write_failures,
            self.record_auth_failure,
            self.upload_job_state,
            self.staging_reserved_bytes,
            self.cache_size_bytes,
        )

    def render(self) -> str:
        """Render the whole registry as a Prometheus text exposition document.

        Each collector contributes its ``# HELP``/``# TYPE``/sample block; blocks
        are joined with newlines and the document ends with a trailing newline,
        as the exposition format requires (and as ``generate_latest`` emits).
        """
        lines: list[str] = []
        for collector in self._collectors:
            lines.extend(collector.render())
        return "\n".join(lines) + "\n"


def create_metrics_router(registry: MetricsRegistry) -> APIRouter:
    """Build the ``GET /metrics`` :class:`fastapi.APIRouter` for the admin port.

    Follows the factory-plus-injected-dependency style of
    :func:`gateway.app.admin.health.create_health_router`: the shared
    :class:`MetricsRegistry` is injected so the router reports the very counters
    the gateway's subsystems increment, and the surface is test-driveable with a
    purpose-built registry. The application wiring (task 32.1) mounts the
    returned router on the admin port (requirement 18.1).

    Args:
        registry: The shared metrics registry whose state is rendered on scrape.

    Returns:
        The configured router, ready to mount.
    """
    router = APIRouter()

    @router.get("/metrics", include_in_schema=False)
    async def metrics() -> PlainTextResponse:
        """Return the registry in Prometheus text exposition format (18.1)."""
        return PlainTextResponse(
            content=registry.render(), media_type=CONTENT_TYPE_LATEST
        )

    return router

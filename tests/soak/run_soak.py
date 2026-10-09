#!/usr/bin/env python3
"""M3 soak-test driver for the tgobjects3 S3 gateway.

This is a standalone, operator/CI script -- **not** a pytest test. It is meant
to be invoked by hand or from a long-running CI job to drive sustained,
mixed-workload load against a *running* gateway endpoint and then emit a
pass/fail verdict as structured JSON.

Context (task 39; design "Soak Test (M3)"; Requirement 18.1)
------------------------------------------------------------
The design's M3 pass criteria are "500 GB upload/download soak over 24 hours
... zero data integrity errors, no unrecovered FAILED generations, Prometheus
``record_auth_failure_total`` counter remains at zero" and the milestone goal
"500 GB soak, every crash point explainable". The real M3 run uses a staging
deployment and moves 500 GB; this driver is parameterised so the *same* code
path also runs as a tiny smoke check (a few MiB) in CI or on a laptop.

What it does
------------
Over a configurable total volume (``--total-bytes``, default a small smoke
value) and/or wall-clock budget (``--duration``), with ``--concurrency`` worker
tasks, it performs a realistic mix of S3 operations against one bucket:

* ``PutObject`` for small/medium objects and a multipart upload for objects at
  or above ``--multipart-threshold`` (varied sizes drawn from a seeded
  distribution).
* ``GetObject`` -- both full-object reads and ranged reads -- verifying the
  bytes (and, for full reads, the SHA-256) round-trip exactly.
* ``ListObjectsV2`` over the bucket's prefix.
* ``CopyObject`` (server-side copy) followed by an integrity-checked read of
  the copy.
* ``DeleteObject`` to keep the working set bounded and exercise tombstones.

Every object's payload is generated deterministically from the ``--seed`` and
the object key, so integrity verification needs no local copy of the data and
the whole run is reproducible.

Verdict
-------
At the end it reports throughput, per-operation counts, error counts and
latency percentiles, and (when ``--metrics-url`` is reachable) reads the
gateway's ``record_auth_failure_total`` counter from ``/metrics`` on the admin
port. It then emits a single JSON object to stdout containing a ``passed``
boolean derived from the design's pass criteria:

* zero data-integrity errors,
* ``record_auth_failure_total`` == 0 (when observable),
* (optionally, via ``--max-error-rate``) a tolerable operation error rate.

Process exit code is 0 on pass, 1 on fail, 2 on misconfiguration / unavailable
endpoint or client.

Dependencies
------------
Object traffic uses **boto3** (the pinned S3 client; see the workflow-matrix /
mypy config in ``pyproject.toml``). boto3 is imported lazily so that importing
this module, running ``--help``, and the pure aggregation layer never require
it. If boto3 is absent at run time the driver prints a clear message naming the
missing dependency and exits ``2`` -- it does not traceback. Reading the
Prometheus counter uses only the standard library (``urllib``).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - import only for type checking
    from concurrent.futures import Future

# ---------------------------------------------------------------------------
# Constants / defaults
# ---------------------------------------------------------------------------

#: Design pass target for the real M3 run (500 GB). Not the default: the real
#: run passes ``--total-bytes`` explicitly. Kept here as documentation of the
#: milestone criterion ("500 GB soak").
DESIGN_TARGET_BYTES: int = 500 * 1024 * 1024 * 1024

#: Default total volume for a *smoke* run -- deliberately tiny so the same code
#: path is safe to run on a laptop or in CI. Override with ``--total-bytes``.
DEFAULT_TOTAL_BYTES: int = 16 * 1024 * 1024

#: Default per-worker concurrency.
DEFAULT_CONCURRENCY: int = 4

#: Objects at or above this size use a multipart upload. 5 MiB is the S3
#: minimum part size, so this is also the smallest meaningful multipart object.
DEFAULT_MULTIPART_THRESHOLD: int = 5 * 1024 * 1024

#: Multipart part size used when an object exceeds the threshold.
DEFAULT_PART_SIZE: int = 5 * 1024 * 1024

#: Default object-size distribution (bytes). Keys are drawn uniformly at random
#: (seeded) from this set unless ``--object-sizes`` overrides it. The mix spans
#: tiny, small, medium and multipart-eligible sizes.
DEFAULT_OBJECT_SIZES: tuple[int, ...] = (
    4 * 1024,
    64 * 1024,
    512 * 1024,
    4 * 1024 * 1024,
    8 * 1024 * 1024,
)

#: Prometheus counter whose value must stay at zero for the soak to pass
#: (design "Soak Test (M3)"; Requirement 18.1).
AUTH_FAILURE_METRIC: str = "record_auth_failure_total"

#: Latency percentiles reported at the end of a run.
REPORTED_PERCENTILES: tuple[float, ...] = (50.0, 90.0, 99.0)

#: S3 operations the driver mixes.
OP_PUT = "put"
OP_GET = "get"
OP_GET_RANGE = "get_range"
OP_LIST = "list"
OP_COPY = "copy"
OP_DELETE = "delete"
ALL_OPS: tuple[str, ...] = (OP_PUT, OP_GET, OP_GET_RANGE, OP_LIST, OP_COPY, OP_DELETE)


# ---------------------------------------------------------------------------
# Pure aggregation layer (importable without boto3)
# ---------------------------------------------------------------------------


def percentile(samples: Sequence[float], pct: float) -> float:
    """Return the ``pct`` percentile of ``samples`` (0.0 for an empty input).

    Uses nearest-rank on a sorted copy; ``pct`` is in ``[0, 100]``. This is a
    pure, deterministic helper so it can be unit-tested without any I/O.
    """
    if not samples:
        return 0.0
    if pct <= 0:
        return min(samples)
    if pct >= 100:
        return max(samples)
    ordered = sorted(samples)
    rank = max(1, min(len(ordered), round(pct / 100.0 * len(ordered))))
    return ordered[rank - 1]


@dataclass
class OpStats:
    """Running counters and latency samples for a single operation kind."""

    count: int = 0
    errors: int = 0
    bytes_moved: int = 0
    latencies_ms: list[float] = field(default_factory=list)

    def record(self, *, latency_ms: float, nbytes: int = 0, error: bool = False) -> None:
        """Record one attempt of this operation."""
        self.count += 1
        self.bytes_moved += nbytes
        self.latencies_ms.append(latency_ms)
        if error:
            self.errors += 1

    def summary(self) -> dict[str, Any]:
        """Return a JSON-serialisable summary including latency percentiles."""
        return {
            "count": self.count,
            "errors": self.errors,
            "bytes_moved": self.bytes_moved,
            "latency_ms": {
                f"p{pct:g}": round(percentile(self.latencies_ms, pct), 3)
                for pct in REPORTED_PERCENTILES
            },
        }


@dataclass
class SoakAccumulator:
    """Thread-safe-ish accumulator of all per-operation stats and integrity errors.

    Updates are serialised by the caller holding ``lock`` (the worker pool uses
    a single lock), so the counters themselves need no additional guarding.
    """

    per_op: dict[str, OpStats] = field(default_factory=lambda: {op: OpStats() for op in ALL_OPS})
    integrity_errors: int = 0
    integrity_error_examples: list[str] = field(default_factory=list)

    def record(self, op: str, *, latency_ms: float, nbytes: int = 0, error: bool = False) -> None:
        """Record one operation attempt against ``op``'s stats."""
        self.per_op[op].record(latency_ms=latency_ms, nbytes=nbytes, error=error)

    def record_integrity_error(self, detail: str) -> None:
        """Record a byte/checksum mismatch; the first few details are kept."""
        self.integrity_errors += 1
        if len(self.integrity_error_examples) < 10:
            self.integrity_error_examples.append(detail)

    @property
    def total_errors(self) -> int:
        """Total operation-level errors across every operation kind."""
        return sum(s.errors for s in self.per_op.values())

    @property
    def total_ops(self) -> int:
        """Total operation attempts across every operation kind."""
        return sum(s.count for s in self.per_op.values())

    @property
    def total_bytes(self) -> int:
        """Total bytes moved (uploaded + downloaded) across all operations."""
        return sum(s.bytes_moved for s in self.per_op.values())


def build_verdict(
    *,
    acc: SoakAccumulator,
    elapsed_s: float,
    auth_failure_count: int | None,
    max_error_rate: float,
) -> dict[str, Any]:
    """Assemble the final pass/fail verdict as a JSON-serialisable dict.

    Pure function: it only reads the accumulated counters and derives the
    verdict per the design's M3 pass criteria (zero integrity errors, auth
    failure counter at zero when observable, error rate within tolerance).
    """
    total_ops = acc.total_ops
    error_rate = (acc.total_errors / total_ops) if total_ops else 0.0

    reasons: list[str] = []
    if acc.integrity_errors > 0:
        reasons.append(f"{acc.integrity_errors} data-integrity error(s)")
    if auth_failure_count is not None and auth_failure_count > 0:
        reasons.append(f"{AUTH_FAILURE_METRIC}={auth_failure_count} (expected 0)")
    if error_rate > max_error_rate:
        reasons.append(
            f"operation error rate {error_rate:.4f} exceeds max {max_error_rate:.4f}"
        )

    passed = not reasons
    throughput = (acc.total_bytes / elapsed_s) if elapsed_s > 0 else 0.0

    return {
        "passed": passed,
        "failure_reasons": reasons,
        "elapsed_seconds": round(elapsed_s, 3),
        "total_operations": total_ops,
        "total_errors": acc.total_errors,
        "error_rate": round(error_rate, 6),
        "integrity_errors": acc.integrity_errors,
        "integrity_error_examples": acc.integrity_error_examples,
        "auth_failure_total": auth_failure_count,
        "total_bytes_moved": acc.total_bytes,
        "throughput_bytes_per_second": round(throughput, 2),
        "operations": {op: acc.per_op[op].summary() for op in ALL_OPS},
    }


# ---------------------------------------------------------------------------
# Deterministic payload generation + integrity
# ---------------------------------------------------------------------------


def payload_for(seed: int, key: str, size: int) -> bytes:
    """Deterministically generate ``size`` bytes for ``key`` under ``seed``.

    The same (seed, key, size) always yields identical bytes, so integrity can
    be re-verified on read without keeping a local copy of the uploaded data,
    and an entire run reproduces exactly from the seed.
    """
    rng = random.Random(f"{seed}:{key}:{size}")
    return rng.randbytes(size)


def sha256_hex(data: bytes) -> str:
    """Return the hex SHA-256 of ``data`` (integrity check helper)."""
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Prometheus /metrics scraping (stdlib only)
# ---------------------------------------------------------------------------


def parse_prometheus_counter(text: str, name: str) -> int | None:
    """Extract an integer counter named ``name`` from Prometheus text exposition.

    Returns the (summed, un-labelled or labelled) value as an int, or ``None``
    if the metric is not present. Comment lines (``# HELP`` / ``# TYPE``) and
    label sets are tolerated; a non-numeric value yields ``None``.
    """
    total = 0.0
    found = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # A sample line is: ``name{labels} value`` or ``name value``.
        metric_part, _, value_part = stripped.rpartition(" ")
        if not metric_part or not value_part:
            continue
        sample_name = metric_part.split("{", 1)[0]
        if sample_name != name:
            continue
        try:
            total += float(value_part)
            found = True
        except ValueError:
            return None
    return int(total) if found else None


def fetch_auth_failure_count(metrics_url: str, timeout: float = 10.0) -> int | None:
    """Fetch ``record_auth_failure_total`` from the gateway ``/metrics`` endpoint.

    Returns the counter value, or ``None`` if the endpoint is unreachable or the
    metric is absent. Never raises -- an unobservable counter is reported as
    ``None`` and handled by the verdict logic.
    """
    try:
        req = urllib.request.Request(metrics_url, headers={"Accept": "text/plain"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - operator-supplied admin URL
            body = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return parse_prometheus_counter(body, AUTH_FAILURE_METRIC)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SoakArgs:
    """Parsed command-line arguments for a soak run."""

    endpoint_url: str
    bucket: str
    region: str
    key_prefix: str
    total_bytes: int
    duration: float | None
    concurrency: int
    object_sizes: tuple[int, ...]
    multipart_threshold: int
    part_size: int
    seed: int
    metrics_url: str | None
    max_error_rate: float
    no_verify_ssl: bool


def _parse_size(text: str) -> int:
    """Parse a human size string (e.g. ``16MiB``, ``500GB``, ``1024``) to bytes."""
    units = {
        "b": 1,
        "kb": 1000,
        "mb": 1000**2,
        "gb": 1000**3,
        "tb": 1000**4,
        "kib": 1024,
        "mib": 1024**2,
        "gib": 1024**3,
        "tib": 1024**4,
    }
    cleaned = text.strip().lower()
    for suffix in sorted(units, key=len, reverse=True):
        if cleaned.endswith(suffix) and cleaned != suffix:
            number = cleaned[: -len(suffix)].strip()
            return int(float(number) * units[suffix])
    return int(cleaned)


def parse_args(argv: Sequence[str] | None = None) -> SoakArgs:
    """Parse command-line arguments for the soak driver."""
    parser = argparse.ArgumentParser(
        prog="run_soak",
        description=(
            "M3 soak-test driver: drive a mixed PutObject / GetObject (full + "
            "ranged) / ListObjects / CopyObject / DeleteObject workload against "
            "a running gateway, verify integrity throughout, and emit a "
            "pass/fail verdict as JSON. Credentials come from the environment "
            "(AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY)."
        ),
    )
    parser.add_argument(
        "--endpoint-url",
        required=True,
        help="S3 endpoint URL of the running gateway (e.g. http://localhost:9000).",
    )
    parser.add_argument(
        "--bucket",
        required=True,
        help="Target bucket name. Must already exist and be writable.",
    )
    parser.add_argument(
        "--region",
        default="us-east-1",
        help="S3 region to pass to the client (default: us-east-1).",
    )
    parser.add_argument(
        "--key-prefix",
        default="soak/",
        help="Key prefix for all objects created by this run (default: soak/).",
    )
    parser.add_argument(
        "--total-bytes",
        type=_parse_size,
        default=DEFAULT_TOTAL_BYTES,
        help=(
            "Total volume to upload before stopping, as bytes or a size string "
            f"(e.g. 16MiB, 500GB). Default: {DEFAULT_TOTAL_BYTES} (smoke). The "
            "design M3 target is 500GiB."
        ),
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help=(
            "Optional wall-clock budget in seconds. The run stops when either "
            "--total-bytes or --duration is reached, whichever comes first."
        ),
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help=f"Number of concurrent worker threads (default: {DEFAULT_CONCURRENCY}).",
    )
    parser.add_argument(
        "--object-sizes",
        default=None,
        help=(
            "Comma-separated object-size distribution (bytes or size strings), "
            "sampled uniformly. Default spans 4KiB..8MiB."
        ),
    )
    parser.add_argument(
        "--multipart-threshold",
        type=_parse_size,
        default=DEFAULT_MULTIPART_THRESHOLD,
        help="Objects at or above this size use multipart upload (default: 5MiB).",
    )
    parser.add_argument(
        "--part-size",
        type=_parse_size,
        default=DEFAULT_PART_SIZE,
        help="Multipart part size (default: 5MiB; S3 minimum).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=1,
        help="Seed for reproducible object keys, sizes and payloads (default: 1).",
    )
    parser.add_argument(
        "--metrics-url",
        default=None,
        help=(
            "Admin /metrics URL to read record_auth_failure_total "
            "(e.g. http://localhost:8080/metrics). If omitted, the counter is "
            "reported as null and not used in the verdict."
        ),
    )
    parser.add_argument(
        "--max-error-rate",
        type=float,
        default=0.0,
        help=(
            "Maximum tolerated operation error rate for a pass (default: 0.0 -- "
            "any operation error fails the soak)."
        ),
    )
    parser.add_argument(
        "--no-verify-ssl",
        action="store_true",
        help="Disable TLS certificate verification (for self-signed staging).",
    )

    ns = parser.parse_args(argv)

    if ns.object_sizes is not None:
        sizes = tuple(_parse_size(part) for part in ns.object_sizes.split(",") if part.strip())
    else:
        sizes = DEFAULT_OBJECT_SIZES

    if not sizes or any(s <= 0 for s in sizes):
        parser.error("--object-sizes must be a comma-separated list of positive sizes")
    if ns.total_bytes <= 0:
        parser.error("--total-bytes must be positive")
    if ns.duration is not None and ns.duration <= 0:
        parser.error("--duration must be positive when supplied")
    if ns.concurrency < 1:
        parser.error("--concurrency must be a positive integer")
    if ns.part_size < 5 * 1024 * 1024:
        parser.error("--part-size must be at least 5MiB (S3 minimum part size)")
    if not 0.0 <= ns.max_error_rate <= 1.0:
        parser.error("--max-error-rate must be between 0.0 and 1.0")

    return SoakArgs(
        endpoint_url=ns.endpoint_url,
        bucket=ns.bucket,
        region=ns.region,
        key_prefix=ns.key_prefix,
        total_bytes=ns.total_bytes,
        duration=ns.duration,
        concurrency=ns.concurrency,
        object_sizes=sizes,
        multipart_threshold=ns.multipart_threshold,
        part_size=ns.part_size,
        seed=ns.seed,
        metrics_url=ns.metrics_url,
        max_error_rate=ns.max_error_rate,
        no_verify_ssl=ns.no_verify_ssl,
    )


# ---------------------------------------------------------------------------
# boto3 I/O layer (lazily imported)
# ---------------------------------------------------------------------------


class DependencyError(RuntimeError):
    """Raised when a required runtime dependency (boto3) is unavailable."""


def _build_s3_client(args: SoakArgs) -> Any:
    """Construct a boto3 S3 client, importing boto3 lazily.

    Credentials are read by boto3 from the environment
    (``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY`` / optional
    ``AWS_SESSION_TOKEN``). boto3 is imported here so that importing this module
    and running ``--help`` never require it.

    Raises:
        DependencyError: If boto3 is not importable.
    """
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:  # pragma: no cover - exercised only without boto3
        raise DependencyError(
            "boto3 is required to run the soak test but is not installed. "
            "Install it with `pip install boto3` (it is the pinned S3 client "
            "used by the workflow-matrix tests)."
        ) from exc

    config = Config(
        region_name=args.region,
        retries={"max_attempts": 3, "mode": "standard"},
        signature_version="s3v4",
    )
    return boto3.client(
        "s3",
        endpoint_url=args.endpoint_url,
        config=config,
        verify=not args.no_verify_ssl,
    )


def _ensure_credentials() -> None:
    """Verify S3 credentials are present in the environment.

    Raises:
        DependencyError: If the required credential variables are absent.
    """
    missing = [
        name
        for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")
        if not os.environ.get(name)
    ]
    if missing:
        raise DependencyError(
            "missing S3 credential environment variable(s): " + ", ".join(missing)
        )


def _probe_endpoint(client: Any, bucket: str) -> None:
    """Confirm the gateway/bucket is reachable before starting the load.

    Raises:
        DependencyError: If the endpoint or bucket cannot be reached.
    """
    try:
        client.head_bucket(Bucket=bucket)
    except Exception as exc:  # noqa: BLE001 - surface any client/transport error cleanly
        raise DependencyError(
            f"endpoint {bucket!r} is not reachable or bucket is unavailable: {exc}"
        ) from exc


@dataclass(frozen=True)
class _WorkItem:
    """One planned object: its key and payload size."""

    index: int
    key: str
    size: int


def _plan_work(args: SoakArgs) -> list[_WorkItem]:
    """Plan the sequence of objects to create until ``total_bytes`` is reached.

    Deterministic given the seed: sizes are sampled from the configured
    distribution, keys are derived from the index.
    """
    rng = random.Random(args.seed)
    items: list[_WorkItem] = []
    planned = 0
    index = 0
    while planned < args.total_bytes:
        size = rng.choice(args.object_sizes)
        key = f"{args.key_prefix}obj-{index:08d}-{size}"
        items.append(_WorkItem(index=index, key=key, size=size))
        planned += size
        index += 1
    return items


def _put_object(client: Any, args: SoakArgs, item: _WorkItem, data: bytes) -> None:
    """Upload one object, choosing single-shot or multipart by size."""
    if item.size >= args.multipart_threshold:
        _multipart_put(client, args, item, data)
    else:
        client.put_object(Bucket=args.bucket, Key=item.key, Body=data)


def _multipart_put(client: Any, args: SoakArgs, item: _WorkItem, data: bytes) -> None:
    """Upload ``data`` for ``item`` as a multipart upload."""
    created = client.create_multipart_upload(Bucket=args.bucket, Key=item.key)
    upload_id = created["UploadId"]
    try:
        parts: list[dict[str, Any]] = []
        part_number = 1
        for offset in range(0, len(data), args.part_size):
            chunk = data[offset : offset + args.part_size]
            resp = client.upload_part(
                Bucket=args.bucket,
                Key=item.key,
                PartNumber=part_number,
                UploadId=upload_id,
                Body=chunk,
            )
            parts.append({"ETag": resp["ETag"], "PartNumber": part_number})
            part_number += 1
        client.complete_multipart_upload(
            Bucket=args.bucket,
            Key=item.key,
            UploadId=upload_id,
            MultipartUpload={"Parts": parts},
        )
    except Exception:
        client.abort_multipart_upload(Bucket=args.bucket, Key=item.key, UploadId=upload_id)
        raise


def _get_full(client: Any, bucket: str, key: str) -> bytes:
    """Read a whole object and return its bytes."""
    resp = client.get_object(Bucket=bucket, Key=key)
    body: bytes = resp["Body"].read()
    return body


def _get_range(client: Any, bucket: str, key: str, start: int, end: int) -> bytes:
    """Read bytes ``[start, end]`` (inclusive) of an object via a Range header."""
    resp = client.get_object(Bucket=bucket, Key=key, Range=f"bytes={start}-{end}")
    body: bytes = resp["Body"].read()
    return body


def run_soak(args: SoakArgs) -> dict[str, Any]:
    """Drive the soak workload and return the JSON-serialisable verdict.

    Performs the full mixed workload with integrity verification, then reads the
    auth-failure counter (if a metrics URL was given) and builds the verdict.

    Raises:
        DependencyError: If boto3, credentials or the endpoint are unavailable.
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor

    _ensure_credentials()
    client = _build_s3_client(args)
    _probe_endpoint(client, args.bucket)

    plan = _plan_work(args)
    acc = SoakAccumulator()
    lock = threading.Lock()
    deadline = (time.monotonic() + args.duration) if args.duration is not None else None
    rng = random.Random(args.seed ^ 0x5EED)

    def expired() -> bool:
        return deadline is not None and time.monotonic() >= deadline

    def _timed(op: str, fn: Any, *, nbytes: int = 0) -> tuple[Any, bool]:
        start = time.monotonic()
        try:
            result = fn()
        except Exception:  # noqa: BLE001 - any client error is a recorded error, not a crash
            with lock:
                acc.record(op, latency_ms=(time.monotonic() - start) * 1000.0, error=True)
            return None, True
        latency_ms = (time.monotonic() - start) * 1000.0
        with lock:
            acc.record(op, latency_ms=latency_ms, nbytes=nbytes, error=False)
        return result, False

    def handle(item: _WorkItem) -> None:
        if expired():
            return
        data = payload_for(args.seed, item.key, item.size)
        expected_digest = sha256_hex(data)

        # PUT (single-shot or multipart).
        _, put_err = _timed(OP_PUT, lambda: _put_object(client, args, item, data), nbytes=item.size)
        if put_err:
            return

        # Full GET + integrity check.
        got, get_err = _timed(
            OP_GET, lambda: _get_full(client, args.bucket, item.key), nbytes=item.size
        )
        if not get_err and got is not None and sha256_hex(got) != expected_digest:
            with lock:
                acc.record_integrity_error(f"full-get mismatch for {item.key}")

        # Ranged GET + integrity check on the slice.
        if item.size > 1:
            start = rng.randrange(0, item.size - 1)
            end = min(item.size - 1, start + rng.randrange(0, item.size - start))
            ranged, range_err = _timed(
                OP_GET_RANGE,
                lambda: _get_range(client, args.bucket, item.key, start, end),
                nbytes=(end - start + 1),
            )
            if not range_err and ranged is not None and ranged != data[start : end + 1]:
                with lock:
                    acc.record_integrity_error(
                        f"ranged-get mismatch for {item.key} bytes={start}-{end}"
                    )

        # LIST (prefix scan).
        _timed(OP_LIST, lambda: client.list_objects_v2(Bucket=args.bucket, Prefix=args.key_prefix))

        # COPY + integrity check of the copy.
        copy_key = f"{item.key}.copy"
        _, copy_err = _timed(
            OP_COPY,
            lambda: client.copy_object(
                Bucket=args.bucket,
                Key=copy_key,
                CopySource={"Bucket": args.bucket, "Key": item.key},
            ),
            nbytes=item.size,
        )
        if not copy_err:
            copied, copy_get_err = _timed(
                OP_GET, lambda: _get_full(client, args.bucket, copy_key), nbytes=item.size
            )
            if not copy_get_err and copied is not None and sha256_hex(copied) != expected_digest:
                with lock:
                    acc.record_integrity_error(f"copy mismatch for {copy_key}")
            # DELETE the copy to keep the working set bounded.
            _timed(OP_DELETE, lambda: client.delete_object(Bucket=args.bucket, Key=copy_key))

        # DELETE the original to exercise tombstones and bound storage growth.
        _timed(OP_DELETE, lambda: client.delete_object(Bucket=args.bucket, Key=item.key))

    start_wall = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures: list[Future[None]] = []
        for item in plan:
            if expired():
                break
            futures.append(pool.submit(handle, item))
        for fut in futures:
            fut.result()
    elapsed = time.monotonic() - start_wall

    auth_failures = (
        fetch_auth_failure_count(args.metrics_url) if args.metrics_url else None
    )

    verdict = build_verdict(
        acc=acc,
        elapsed_s=elapsed,
        auth_failure_count=auth_failures,
        max_error_rate=args.max_error_rate,
    )
    verdict["config"] = {
        "endpoint_url": args.endpoint_url,
        "bucket": args.bucket,
        "total_bytes_planned": args.total_bytes,
        "objects_planned": len(plan),
        "concurrency": args.concurrency,
        "seed": args.seed,
        "duration_budget_s": args.duration,
        "design_target_bytes": DESIGN_TARGET_BYTES,
    }
    return verdict


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point: run the soak and print the JSON verdict to stdout.

    Returns ``0`` on pass, ``1`` on a failed verdict, and ``2`` on
    misconfiguration or an unavailable client/endpoint (printed to stderr).
    """
    args = parse_args(argv)
    try:
        verdict = run_soak(args)
    except DependencyError as exc:
        print(f"soak unavailable: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(verdict, indent=2, sort_keys=True))
    return 0 if verdict["passed"] else 1


if __name__ == "__main__":  # pragma: no cover - module CLI entry point
    raise SystemExit(main())

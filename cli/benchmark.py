"""M0 feasibility benchmark harness.

This module measures the Telegram throughput baselines required before the
storage format is frozen at M1 (see Requirement 21, acceptance criteria 21.1
and 21.2). It talks to Telegram through Telethon *directly* -- the
``TelegramPort`` abstraction is deliberately not used yet, since it is
introduced later in M1.

The module is split into two clearly separated halves:

* **Pure aggregation layer** -- :func:`mean`, :func:`average_iterations`,
  :func:`detect_flood_onset`, :func:`build_report` and the surrounding
  dataclasses. These functions are deterministic, side-effect free and
  importable without Telethon installed. Task 2.2 unit-tests this layer
  (averaging over >= 10 iterations and flood-onset detection).

* **Telethon I/O layer** -- :func:`run_benchmark` and its helpers. These import
  Telethon lazily so that the aggregation layer (and its tests) never require
  Telegram credentials or network access.

The harness authenticates using Telegram API credentials read from the
environment (``TGOS3_API_ID``, ``TGOS3_API_HASH``, ``TGOS3_SESSION``); no
secrets are hardcoded. Results are emitted to stdout as a single
machine-readable JSON object.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - import only for type checking
    from telethon import TelegramClient

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: A single record is exactly 1 MiB on the wire.
RECORD_SIZE: int = 1_048_576

#: Default number of records packed into one Telegram document (chunk).
DEFAULT_CHUNK_RECORDS: int = 512

#: Default sustained-throughput measurement window, in seconds.
DEFAULT_DURATION_SECONDS: int = 60

#: Default upload concurrency for the sustained-rate measurement.
DEFAULT_CONCURRENCY: int = 2

#: Minimum iterations an averaged metric must be sampled over (Req 21.2).
MIN_ITERATIONS: int = 10

#: Upload concurrency levels probed, in ascending order, while searching for
#: the flood-onset point (the first level that trips a Telegram rate limit).
FLOOD_PROBE_CONCURRENCY: tuple[int, ...] = (1, 2, 4, 8, 16, 32, 64)


# ---------------------------------------------------------------------------
# Pure aggregation layer (importable without Telethon; unit-tested by 2.2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IterationSample:
    """A single timed iteration of a repeated measurement.

    ``latency_ms`` is the wall-clock round-trip time of the iteration in
    milliseconds; ``api_calls`` is the number of Telegram API calls the
    iteration issued; ``items`` is the count of work units processed (e.g.
    messages scanned), used to derive per-second rates.
    """

    latency_ms: float
    api_calls: int = 0
    items: int = 0


@dataclass(frozen=True)
class AggregatedMetric:
    """Averages derived from a run of >= :data:`MIN_ITERATIONS` iterations."""

    iterations: int
    avg_latency_ms: float
    avg_api_calls: float
    avg_items_per_second: float


@dataclass(frozen=True)
class FloodProbe:
    """Outcome of attempting uploads at a single concurrency level."""

    concurrency: int
    flooded: bool


@dataclass(frozen=True)
class FloodOnset:
    """Result of the flood-onset search.

    ``concurrency`` is the lowest probed concurrency at which Telegram returned
    a rate-limit (flood-wait) response, or ``None`` if no probe tripped a
    limit.
    """

    concurrency: int | None
    probed_levels: tuple[int, ...]


def mean(values: Sequence[float]) -> float:
    """Return the arithmetic mean of ``values`` (0.0 for an empty sequence)."""
    if not values:
        return 0.0
    return sum(values) / len(values)


def average_iterations(samples: Sequence[IterationSample]) -> AggregatedMetric:
    """Average a sequence of iteration samples.

    Satisfies Requirement 21.2: averaged metrics must be computed over at least
    :data:`MIN_ITERATIONS` consecutive iterations.

    Args:
        samples: Per-iteration measurements. Must contain at least
            :data:`MIN_ITERATIONS` entries.

    Returns:
        The averaged latency, API-call count and items-per-second rate.

    Raises:
        ValueError: If fewer than :data:`MIN_ITERATIONS` samples are supplied.
    """
    if len(samples) < MIN_ITERATIONS:
        raise ValueError(
            f"averaged metrics require at least {MIN_ITERATIONS} iterations, "
            f"got {len(samples)}"
        )

    avg_latency_ms = mean([s.latency_ms for s in samples])
    avg_api_calls = mean([float(s.api_calls) for s in samples])

    per_second_rates: list[float] = []
    for s in samples:
        if s.latency_ms > 0:
            per_second_rates.append(s.items / (s.latency_ms / 1000.0))
    avg_items_per_second = mean(per_second_rates)

    return AggregatedMetric(
        iterations=len(samples),
        avg_latency_ms=avg_latency_ms,
        avg_api_calls=avg_api_calls,
        avg_items_per_second=avg_items_per_second,
    )


def detect_flood_onset(probes: Sequence[FloodProbe]) -> FloodOnset:
    """Find the lowest concurrency level that triggered a Telegram rate limit.

    Satisfies the flood-onset portion of Requirement 21.1: flood onset is the
    upload concurrency level at which the first Telegram rate-limit response is
    received.

    The returned ``concurrency`` is the smallest ``concurrency`` among probes
    marked ``flooded``; if no probe flooded, it is ``None``. Probe ordering in
    the input does not affect the result.

    Args:
        probes: Per-level probe outcomes.

    Returns:
        A :class:`FloodOnset` describing the onset level and all probed levels.
    """
    probed_levels = tuple(p.concurrency for p in probes)
    flooded_levels = [p.concurrency for p in probes if p.flooded]
    onset = min(flooded_levels) if flooded_levels else None
    return FloodOnset(concurrency=onset, probed_levels=probed_levels)


@dataclass
class BenchmarkReport:
    """The full machine-readable benchmark report emitted to stdout."""

    channel_id: int
    chunk_records: int
    record_size_bytes: int
    duration_seconds: int
    concurrency: int
    sustained_upload_bytes_per_second: float = 0.0
    sustained_download_bytes_per_second: float = 0.0
    cold_ttfb_ms: float = 0.0
    flood_onset_concurrency: int | None = None
    flood_probed_levels: tuple[int, ...] = ()
    file_reference_refresh: AggregatedMetric | None = None
    channel_scan: AggregatedMetric | None = None
    warnings: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        """Serialise the report to a stable, machine-readable JSON string."""
        return json.dumps(_as_jsonable(self), indent=2, sort_keys=True)


def build_report(
    *,
    channel_id: int,
    chunk_records: int,
    duration_seconds: int,
    concurrency: int,
    upload_bytes_per_second: float,
    download_bytes_per_second: float,
    cold_ttfb_ms: float,
    flood_onset: FloodOnset,
    file_reference_refresh: AggregatedMetric,
    channel_scan: AggregatedMetric,
    warnings: Sequence[str] = (),
) -> BenchmarkReport:
    """Assemble a :class:`BenchmarkReport` from measured components.

    This is a pure function: it performs no I/O and simply packages measured
    values into the report structure.
    """
    return BenchmarkReport(
        channel_id=channel_id,
        chunk_records=chunk_records,
        record_size_bytes=RECORD_SIZE,
        duration_seconds=duration_seconds,
        concurrency=concurrency,
        sustained_upload_bytes_per_second=upload_bytes_per_second,
        sustained_download_bytes_per_second=download_bytes_per_second,
        cold_ttfb_ms=cold_ttfb_ms,
        flood_onset_concurrency=flood_onset.concurrency,
        flood_probed_levels=flood_onset.probed_levels,
        file_reference_refresh=file_reference_refresh,
        channel_scan=channel_scan,
        warnings=list(warnings),
    )


def _as_jsonable(value: Any) -> Any:
    """Recursively convert dataclasses/tuples into JSON-serialisable values."""
    if hasattr(value, "__dataclass_fields__"):
        return {k: _as_jsonable(v) for k, v in asdict(value).items()}
    if isinstance(value, tuple):
        return [_as_jsonable(v) for v in value]
    if isinstance(value, list):
        return [_as_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _as_jsonable(v) for k, v in value.items()}
    return value


# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BenchmarkArgs:
    """Parsed command-line arguments."""

    channel_id: int
    duration: int
    chunk_records: int
    concurrency: int


def parse_args(argv: Sequence[str] | None = None) -> BenchmarkArgs:
    """Parse command-line arguments for the benchmark harness."""
    parser = argparse.ArgumentParser(
        prog="tgobjects3-benchmark",
        description=(
            "M0 feasibility benchmark: measures Telegram upload/download "
            "throughput, cold TTFB, flood onset, file-reference refresh cost "
            "and channel scan rate, emitting a JSON report to stdout."
        ),
    )
    parser.add_argument(
        "--channel-id",
        type=int,
        required=True,
        help="Telegram channel ID to run the benchmark against.",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=DEFAULT_DURATION_SECONDS,
        help=(
            "Sustained-throughput measurement window in seconds "
            f"(default: {DEFAULT_DURATION_SECONDS})."
        ),
    )
    parser.add_argument(
        "--chunk-records",
        type=int,
        default=DEFAULT_CHUNK_RECORDS,
        help=(
            "Number of 1 MiB records per Telegram document "
            f"(default: {DEFAULT_CHUNK_RECORDS})."
        ),
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help=(
            "Upload concurrency for the sustained-rate measurement "
            f"(default: {DEFAULT_CONCURRENCY})."
        ),
    )

    ns = parser.parse_args(argv)

    if ns.duration < 1:
        parser.error("--duration must be a positive number of seconds")
    if ns.chunk_records < 1:
        parser.error("--chunk-records must be a positive integer")
    if ns.concurrency < 1:
        parser.error("--concurrency must be a positive integer")

    return BenchmarkArgs(
        channel_id=ns.channel_id,
        duration=ns.duration,
        chunk_records=ns.chunk_records,
        concurrency=ns.concurrency,
    )


# ---------------------------------------------------------------------------
# Telethon I/O layer (lazily imported; not required by the aggregation layer)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TelegramCredentials:
    """Telegram API credentials sourced from the environment."""

    api_id: int
    api_hash: str
    session: str


def load_credentials(env: dict[str, str] | None = None) -> TelegramCredentials:
    """Read Telegram API credentials from the environment.

    Reads ``TGOS3_API_ID``, ``TGOS3_API_HASH`` and ``TGOS3_SESSION`` (the
    latter may be a StringSession value or a path to a session file). No
    secrets are hardcoded.

    Raises:
        SystemExit: If any required variable is missing. Each missing variable
            is reported on its own line to stderr.
    """
    source = os.environ if env is None else env
    missing: list[str] = []

    raw_api_id = source.get("TGOS3_API_ID")
    api_hash = source.get("TGOS3_API_HASH")
    session = source.get("TGOS3_SESSION")

    if not raw_api_id:
        missing.append("TGOS3_API_ID")
    if not api_hash:
        missing.append("TGOS3_API_HASH")
    if not session:
        missing.append("TGOS3_SESSION")

    api_id = 0
    if raw_api_id:
        try:
            api_id = int(raw_api_id)
        except ValueError:
            print("TGOS3_API_ID must be an integer", file=sys.stderr)
            raise SystemExit(2) from None

    if missing:
        for name in missing:
            print(f"missing required environment variable: {name}", file=sys.stderr)
        raise SystemExit(2)

    assert api_hash is not None  # narrowed by the missing-vars check above
    assert session is not None
    return TelegramCredentials(api_id=api_id, api_hash=api_hash, session=session)


def _build_client(creds: TelegramCredentials) -> TelegramClient:
    """Construct an (unconnected) Telethon client from credentials.

    Telethon is imported here, lazily, so that importing this module (and
    unit-testing the aggregation layer) never requires Telethon or network
    access.
    """
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    session_path = creds.session
    if os.path.exists(session_path):
        # Treat as a session file on disk.
        return TelegramClient(session_path, creds.api_id, creds.api_hash)
    # Otherwise treat the value as an in-memory StringSession.
    return TelegramClient(StringSession(creds.session), creds.api_id, creds.api_hash)


def _is_flood_error(exc: BaseException) -> bool:
    """Return True if ``exc`` is a Telegram rate-limit (flood-wait) error."""
    return type(exc).__name__.startswith("FloodWait")


def _make_payload(chunk_records: int) -> bytes:
    """Build a single document payload of ``chunk_records`` x 1 MiB records."""
    return os.urandom(RECORD_SIZE * chunk_records)


async def _measure_sustained_upload(
    client: TelegramClient,
    channel_id: int,
    chunk_records: int,
    concurrency: int,
    duration: int,
) -> tuple[float, list[int]]:
    """Upload documents in parallel for ``duration`` seconds.

    Returns the sustained upload rate in bytes/second together with the list of
    uploaded message IDs (used for the subsequent download measurement).
    """
    payload = _make_payload(chunk_records)
    doc_size = len(payload)
    deadline = time.monotonic() + duration
    uploaded_bytes = 0
    message_ids: list[int] = []
    lock = asyncio.Lock()

    async def uploader() -> None:
        nonlocal uploaded_bytes
        while time.monotonic() < deadline:
            message = await client.send_file(channel_id, payload, force_document=True)
            async with lock:
                uploaded_bytes += doc_size
                message_ids.append(message.id)

    start = time.monotonic()
    await asyncio.gather(*(uploader() for _ in range(concurrency)))
    elapsed = max(time.monotonic() - start, 1e-9)
    return uploaded_bytes / elapsed, message_ids


async def _measure_sustained_download(
    client: TelegramClient,
    channel_id: int,
    message_ids: Sequence[int],
    concurrency: int,
    duration: int,
) -> float:
    """Download previously uploaded documents, returning bytes/second."""
    if not message_ids:
        return 0.0

    deadline = time.monotonic() + duration
    downloaded_bytes = 0
    lock = asyncio.Lock()
    index = 0

    async def next_message_id() -> int:
        nonlocal index
        async with lock:
            mid = message_ids[index % len(message_ids)]
            index += 1
            return mid

    async def downloader() -> None:
        nonlocal downloaded_bytes
        while time.monotonic() < deadline:
            mid = await next_message_id()
            messages = await client.get_messages(channel_id, ids=mid)
            data = await client.download_media(messages, file=bytes)
            if data is not None:
                async with lock:
                    downloaded_bytes += len(data)

    start = time.monotonic()
    await asyncio.gather(*(downloader() for _ in range(concurrency)))
    elapsed = max(time.monotonic() - start, 1e-9)
    return downloaded_bytes / elapsed


async def _measure_cold_ttfb(
    creds: TelegramCredentials,
    channel_id: int,
    message_id: int,
) -> float:
    """Measure cold time-to-first-byte in ms using a fresh client.

    A brand-new client is created with no cached file references, so the
    timing captures the full reference-resolution + first-byte path.
    """
    client = _build_client(creds)
    await client.connect()
    try:
        start = time.monotonic()
        message = await client.get_messages(channel_id, ids=message_id)
        await client.download_media(message, file=bytes)
        return (time.monotonic() - start) * 1000.0
    finally:
        await client.disconnect()


async def _probe_flood_onset(
    client: TelegramClient,
    channel_id: int,
    chunk_records: int,
) -> FloodOnset:
    """Probe ascending concurrency levels until a rate limit is observed."""
    payload = _make_payload(chunk_records)
    probes: list[FloodProbe] = []

    for level in FLOOD_PROBE_CONCURRENCY:

        async def one_upload() -> None:
            await client.send_file(channel_id, payload, force_document=True)

        results = await asyncio.gather(
            *(one_upload() for _ in range(level)),
            return_exceptions=True,
        )
        flooded = any(
            isinstance(r, BaseException) and _is_flood_error(r) for r in results
        )
        probes.append(FloodProbe(concurrency=level, flooded=flooded))
        if flooded:
            break

    return detect_flood_onset(probes)


async def _measure_file_reference_refresh(
    client: TelegramClient,
    channel_id: int,
    message_id: int,
    iterations: int,
) -> AggregatedMetric:
    """Measure file-reference refresh latency and API-call count per iteration."""
    samples: list[IterationSample] = []
    for _ in range(iterations):
        start = time.monotonic()
        # Re-fetching the message refreshes its file reference; the follow-up
        # metadata read models a second dependent API call.
        await client.get_messages(channel_id, ids=message_id)
        await client.get_messages(channel_id, ids=message_id)
        latency_ms = (time.monotonic() - start) * 1000.0
        samples.append(IterationSample(latency_ms=latency_ms, api_calls=2))
    return average_iterations(samples)


async def _measure_channel_scan(
    client: TelegramClient,
    channel_id: int,
    iterations: int,
    scan_limit: int = 100,
) -> AggregatedMetric:
    """Measure channel message scan rate (messages/s) averaged over iterations."""
    samples: list[IterationSample] = []
    for _ in range(iterations):
        start = time.monotonic()
        scanned = 0
        async for _message in client.iter_messages(channel_id, limit=scan_limit):
            scanned += 1
        latency_ms = (time.monotonic() - start) * 1000.0
        samples.append(
            IterationSample(latency_ms=latency_ms, api_calls=1, items=scanned)
        )
    return average_iterations(samples)


async def run_benchmark(args: BenchmarkArgs) -> BenchmarkReport:
    """Run the full M0 benchmark and return the assembled report.

    Orchestrates every measurement required by Requirement 21.1 and 21.2 using
    Telethon directly, then packages the results via the pure
    :func:`build_report` helper.
    """
    creds = load_credentials()
    client = _build_client(creds)
    warnings: list[str] = []

    await client.connect()
    try:
        upload_rate, message_ids = await _measure_sustained_upload(
            client,
            args.channel_id,
            args.chunk_records,
            args.concurrency,
            args.duration,
        )

        download_rate = await _measure_sustained_download(
            client,
            args.channel_id,
            message_ids,
            args.concurrency,
            args.duration,
        )

        if message_ids:
            cold_ttfb_ms = await _measure_cold_ttfb(
                creds, args.channel_id, message_ids[0]
            )
            refresh = await _measure_file_reference_refresh(
                client, args.channel_id, message_ids[0], MIN_ITERATIONS
            )
        else:
            warnings.append("no documents uploaded; cold TTFB and refresh skipped")
            cold_ttfb_ms = 0.0
            refresh = _empty_metric()

        flood_onset = await _probe_flood_onset(
            client, args.channel_id, args.chunk_records
        )

        scan = await _measure_channel_scan(client, args.channel_id, MIN_ITERATIONS)
    finally:
        await client.disconnect()

    return build_report(
        channel_id=args.channel_id,
        chunk_records=args.chunk_records,
        duration_seconds=args.duration,
        concurrency=args.concurrency,
        upload_bytes_per_second=upload_rate,
        download_bytes_per_second=download_rate,
        cold_ttfb_ms=cold_ttfb_ms,
        flood_onset=flood_onset,
        file_reference_refresh=refresh,
        channel_scan=scan,
        warnings=warnings,
    )


def _empty_metric() -> AggregatedMetric:
    """A zeroed metric used when a measurement is skipped."""
    return AggregatedMetric(
        iterations=0,
        avg_latency_ms=0.0,
        avg_api_calls=0.0,
        avg_items_per_second=0.0,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point: run the benchmark and print the JSON report to stdout."""
    args = parse_args(argv)
    report = asyncio.run(run_benchmark(args))
    print(report.to_json())
    return 0


if __name__ == "__main__":  # pragma: no cover - module CLI entry point
    raise SystemExit(main())

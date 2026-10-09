"""Master key rotation CLI — re-wrap every bucket KEK under a new master key.

This command rotates the root of the TGOBJECTS3 key hierarchy. The master key
derives (via HKDF) a *root key* that wraps every per-bucket KEK; rotating the
master key therefore means unwrapping each bucket's KEK with the old root key
and re-wrapping it with the new one, then publishing the new wrapping as a fresh
:class:`~gateway.app.format.types.BucketManifest` message in the bucket's
Telegram channel and updating the ``buckets`` index row (requirement 13.8).

Atomicity model (requirements 13.8, 13.9). Telegram offers no cross-channel
transaction, so rotation cannot be made atomic across all buckets in a single
irreversible step. Instead the command rotates buckets one at a time and treats
each bucket as its own atomic unit of work:

* The bucket KEK itself never changes — only its *wrapping* changes — so the
  plaintext key material is identical before and after. A bucket is "rotated"
  once a BucketManifest wrapped under the **new** root key exists in its channel
  and the index row records that new wrapping.
* The new BucketManifest is written to Telegram **before** the index row is
  updated. If the process dies between those two writes, the durable truth
  (Telegram) already holds the new wrapping and a re-run reconciles the index.
  The index is a rebuildable cache, never the source of truth.
* If a write fails partway through the run, every already-rotated bucket keeps
  its new wrapping and every not-yet-rotated bucket keeps its old wrapping
  untouched (requirement 13.9). The run stops at the first failure and reports
  exactly which buckets were rotated, which were skipped (already rotated), and
  which remain under the old key — with an actionable message so the operator
  can re-run to finish the remainder.

Idempotency / safe re-run (requirement 13.9). The command classifies each
bucket by trying to unwrap its stored ``kek_wrapped`` first with the **old**
root key and then with the **new** root key:

* unwraps under the old key  → rotate it now;
* unwraps under the new key  → already rotated, skip it (a prior partial run
  reached this bucket);
* unwraps under neither       → the bucket cannot be rotated with the supplied
  keys; report it and stop so the operator can investigate rather than risk
  stranding a bucket under an unknown key.

Re-running the command after a partial failure therefore completes only the
remaining buckets and leaves the already-rotated ones alone.

Scope boundary (task 37.1 only). This module owns the rotation logic, the CLI
surface and the operator report. The mid-rotation-failure integration test is
task 37.2. :func:`run_rotation` takes the :class:`TelegramPort` as an argument
so the orchestration is testable against a fake port without Telethon or
network access, mirroring ``cli/rebuild.py``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

from gateway.app.format import bucket_manifest as bucket_manifest_codec
from gateway.app.format.key_wrap import KeyUnwrapError, derive_root_key, unwrap, wrap
from gateway.app.format.types import BucketManifest
from gateway.app.telegram.errors import FloodWaitError, TelegramUnavailableError
from gateway.app.telegram.port import TelegramPort
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from cli.rebuild import TelegramCredentials, load_credentials

__all__ = [
    "EXIT_ERROR",
    "EXIT_OK",
    "EXIT_USAGE",
    "BucketOutcome",
    "OutcomeKind",
    "RotationArgs",
    "RotationError",
    "RotationResult",
    "format_report",
    "main",
    "parse_args",
    "rotate_bucket",
    "run_rotation",
]

logger = logging.getLogger("tgobjects3.rotate_master_key")

#: Caption prefix for a BucketManifest message (design "Telegram Message Format"
#: table): ``tgo3-k-<version>``. The version suffix is the manifest's
#: ``kek_version`` so each rotation generation lands as its own captioned
#: message rather than overwriting the previous one.
_PREFIX_BUCKET_MANIFEST = "tgo3-k-"

#: File name attached to the uploaded BucketManifest document. The caption, not
#: the file name, carries the logical identity; the name is cosmetic.
_MANIFEST_FILE_NAME = "bucket_manifest.msgpack"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2


class RotationError(Exception):
    """A bucket could not be rotated with the supplied old/new master keys.

    Raised when a bucket's stored wrapped KEK unwraps under neither the old nor
    the new root key, which means the keys given on the command line do not
    match the key currently protecting that bucket. Rotation stops rather than
    guessing, so the operator can investigate without stranding the bucket
    (requirement 13.9).
    """


class OutcomeKind(Enum):
    """How a single bucket fared during a rotation run.

    :attr:`ROTATED` and :attr:`ALREADY_ROTATED` are both successful terminal
    states for a bucket (its channel holds a manifest under the new key);
    :attr:`NOT_ROTATED` records a bucket left under the old key because the run
    stopped before reaching it.
    """

    ROTATED = "rotated"
    ALREADY_ROTATED = "already_rotated"
    NOT_ROTATED = "not_rotated"


@dataclass(frozen=True)
class BucketOutcome:
    """The per-bucket result of a rotation run (requirement 13.9 reporting)."""

    bucket_name: str
    channel_id: int
    kind: OutcomeKind
    #: The ``kek_version`` recorded after this bucket's processing: the new
    #: version for a bucket rotated or already rotated in this run, the old
    #: version for a bucket left untouched.
    kek_version: int


@dataclass(frozen=True)
class RotationResult:
    """Aggregate result across every bucket a rotation run considered.

    Carries the per-bucket outcomes and the overall success flag the operator
    report and the process exit code are derived from. ``error`` holds the
    actionable message when the run stopped before rotating every bucket.
    """

    outcomes: tuple[BucketOutcome, ...]
    completed: bool
    error: str | None = None

    @property
    def rotated(self) -> tuple[BucketOutcome, ...]:
        """Buckets re-wrapped under the new key during this run."""
        return tuple(o for o in self.outcomes if o.kind is OutcomeKind.ROTATED)

    @property
    def already_rotated(self) -> tuple[BucketOutcome, ...]:
        """Buckets already under the new key before this run (skipped)."""
        return tuple(
            o for o in self.outcomes if o.kind is OutcomeKind.ALREADY_ROTATED
        )

    @property
    def not_rotated(self) -> tuple[BucketOutcome, ...]:
        """Buckets still under the old key because the run stopped early."""
        return tuple(o for o in self.outcomes if o.kind is OutcomeKind.NOT_ROTATED)


# ---------------------------------------------------------------------------
# Per-bucket rotation
# ---------------------------------------------------------------------------


async def rotate_bucket(
    session: AsyncSession,
    port: TelegramPort,
    bucket: _BucketRow,
    old_root_key: bytes,
    new_root_key: bytes,
) -> BucketOutcome:
    """Rotate a single bucket, or recognise it as already rotated.

    The bucket's stored wrapped KEK is classified by attempting an unwrap first
    with ``old_root_key`` then with ``new_root_key``:

    * **old key unwraps** → re-wrap the recovered KEK under ``new_root_key``,
      bump ``kek_version``, write a new :class:`BucketManifest` message to the
      bucket's channel (caption ``tgo3-k-<new_version>``) and only then update
      the index row. Writing Telegram before the index keeps the durable truth
      ahead of the cache, so a crash between the two leaves the new wrapping
      recoverable and a re-run idempotent (requirements 13.8, 13.9).
    * **new key unwraps** → the bucket was rotated by an earlier (possibly
      partial) run; it is left untouched and reported as already rotated.
    * **neither unwraps** → :class:`RotationError`; the supplied keys do not
      match this bucket, so the caller stops rather than strand it.

    The KEK plaintext is identical before and after — only its wrapping changes
    — so no object data needs re-encryption.
    """
    old_kek = _try_unwrap(old_root_key, bucket.kek_wrapped)
    if old_kek is None:
        if _try_unwrap(new_root_key, bucket.kek_wrapped) is not None:
            logger.info(
                "bucket %r (channel %d) already rotated to kek_version %d; "
                "skipping",
                bucket.name,
                bucket.channel_id,
                bucket.kek_version,
            )
            return BucketOutcome(
                bucket_name=bucket.name,
                channel_id=bucket.channel_id,
                kind=OutcomeKind.ALREADY_ROTATED,
                kek_version=bucket.kek_version,
            )
        raise RotationError(
            f"bucket {bucket.name!r} (channel {bucket.channel_id}) wrapped KEK "
            f"could not be unwrapped with either the old or the new master key; "
            f"the supplied keys do not match this bucket"
        )

    new_version = bucket.kek_version + 1
    new_wrapped = wrap(new_root_key, old_kek)

    manifest = BucketManifest(
        format_version=1,
        bucket_name=bucket.name,
        kek_wrapped=new_wrapped,
        kek_version=new_version,
    )
    payload = bucket_manifest_codec.serialise(manifest)
    caption = f"{_PREFIX_BUCKET_MANIFEST}{new_version}"

    # Durable truth first: publish the new wrapping to Telegram before touching
    # the index. If this raises, the bucket is left under the old key and the
    # caller reports it as not rotated (requirement 13.9).
    await port.send_document(
        bucket.channel_id, payload, _MANIFEST_FILE_NAME, caption
    )

    # Index is a cache: update it to reflect the new wrapping. A failure here
    # leaves the new manifest in Telegram, so a re-run classifies the bucket as
    # already rotated and reconciles the index.
    await _update_bucket_row(session, bucket.id, new_wrapped, new_version)

    logger.info(
        "rotated bucket %r (channel %d) to kek_version %d",
        bucket.name,
        bucket.channel_id,
        new_version,
    )
    return BucketOutcome(
        bucket_name=bucket.name,
        channel_id=bucket.channel_id,
        kind=OutcomeKind.ROTATED,
        kek_version=new_version,
    )


def _try_unwrap(root_key: bytes, wrapped: bytes) -> bytes | None:
    """Unwrap ``wrapped`` under ``root_key``, or ``None`` on integrity failure.

    AES Key Wrap carries an integrity check, so a failed unwrap unambiguously
    means the wrong key — exactly the signal used to classify whether a bucket
    is still under the old key or already under the new one.
    """
    try:
        return unwrap(root_key, wrapped)
    except KeyUnwrapError:
        return None


# ---------------------------------------------------------------------------
# Database access
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _BucketRow:
    """A snapshot of the ``buckets`` columns rotation needs.

    Read eagerly into a frozen value object so the long-running per-bucket work
    (Telegram writes) does not hold a live ORM row across the rotation loop.
    """

    id: object
    name: str
    channel_id: int
    kek_wrapped: bytes
    kek_version: int


async def _load_buckets(session: AsyncSession) -> list[_BucketRow]:
    """Load every bucket's rotation-relevant columns, ordered by name.

    A stable order makes the operator report deterministic and, on a re-run
    after a partial failure, keeps the already-rotated prefix contiguous.
    """
    from gateway.app.db.models import Bucket

    result = await session.execute(
        select(
            Bucket.id,
            Bucket.name,
            Bucket.channel_id,
            Bucket.kek_wrapped,
            Bucket.kek_version,
        ).order_by(Bucket.name)
    )
    return [
        _BucketRow(
            id=row.id,
            name=row.name,
            channel_id=row.channel_id,
            kek_wrapped=row.kek_wrapped,
            kek_version=row.kek_version,
        )
        for row in result.all()
    ]


async def _update_bucket_row(
    session: AsyncSession, bucket_id: object, kek_wrapped: bytes, kek_version: int
) -> None:
    """Persist the new wrapping and version for one bucket, then commit.

    Each bucket is committed independently so an interruption later in the run
    cannot roll back an already-published rotation's index update — the index
    stays consistent with what Telegram durably holds (requirement 13.9).
    """
    from gateway.app.db.models import Bucket
    from sqlalchemy import update as sa_update

    await session.execute(
        sa_update(Bucket)
        .where(Bucket.id == bucket_id)
        .values(kek_wrapped=kek_wrapped, kek_version=kek_version)
    )
    await session.commit()


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def run_rotation(
    args: RotationArgs,
    port: TelegramPort,
) -> RotationResult:
    """Rotate every bucket's KEK from the old master key to the new one.

    Derives the old and new root keys, loads every bucket, and rotates them one
    at a time via :func:`rotate_bucket` (requirement 13.8). The run is tolerant
    of being resumed: buckets already under the new key are skipped, so a re-run
    after a partial failure only finishes the remainder.

    On the first failure — a Telegram write error, or a bucket whose KEK matches
    neither key (:class:`RotationError`) — the run stops. Every bucket processed
    so far keeps its real outcome (rotated or already rotated) and every
    remaining bucket is recorded as :attr:`OutcomeKind.NOT_ROTATED` under the
    old key, so the result names exactly which manifests were and were not
    updated (requirement 13.9). The database engine is created from
    ``args.db_url`` and disposed before returning.

    Args:
        args: Parsed CLI arguments carrying the old and new master keys and the
            database URL.
        port: The Telegram boundary used to publish new BucketManifest
            messages. Injected so the orchestration is testable without
            Telethon.

    Returns:
        A :class:`RotationResult` whose ``completed`` flag is ``True`` only when
        every bucket ended under the new key.
    """
    old_root_key = derive_root_key(args.old_master_key)
    new_root_key = derive_root_key(args.new_master_key)

    engine = create_async_engine(args.db_url)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessionmaker() as session:
            buckets = await _load_buckets(session)
            return await _rotate_all(session, port, buckets, old_root_key, new_root_key)
    finally:
        await engine.dispose()


async def _rotate_all(
    session: AsyncSession,
    port: TelegramPort,
    buckets: Sequence[_BucketRow],
    old_root_key: bytes,
    new_root_key: bytes,
) -> RotationResult:
    """Rotate buckets in order, stopping and reporting at the first failure.

    Collects a :class:`BucketOutcome` per bucket. When a bucket raises, the run
    halts: the failing bucket and every bucket after it are recorded as
    :attr:`OutcomeKind.NOT_ROTATED` (they remain under the old key), and an
    actionable error naming the failure and the unrotated buckets is attached to
    the result (requirement 13.9).
    """
    outcomes: list[BucketOutcome] = []
    for index, bucket in enumerate(buckets):
        try:
            outcome = await rotate_bucket(
                session, port, bucket, old_root_key, new_root_key
            )
        except (RotationError, TelegramUnavailableError, FloodWaitError) as exc:
            remaining = buckets[index:]
            outcomes.extend(_not_rotated(b) for b in remaining)
            error = _format_stop_error(bucket, remaining, exc)
            logger.error("%s", error)
            return RotationResult(
                outcomes=tuple(outcomes), completed=False, error=error
            )
        outcomes.append(outcome)

    return RotationResult(outcomes=tuple(outcomes), completed=True)


def _not_rotated(bucket: _BucketRow) -> BucketOutcome:
    """Build a NOT_ROTATED outcome for a bucket the run never reached."""
    return BucketOutcome(
        bucket_name=bucket.name,
        channel_id=bucket.channel_id,
        kind=OutcomeKind.NOT_ROTATED,
        kek_version=bucket.kek_version,
    )


def _format_stop_error(
    failed: _BucketRow, remaining: Sequence[_BucketRow], exc: Exception
) -> str:
    """Build the actionable stop message naming the unrotated buckets."""
    names = ", ".join(b.name for b in remaining)
    return (
        f"rotation stopped at bucket {failed.name!r} (channel "
        f"{failed.channel_id}): {exc}. "
        f"{len(remaining)} bucket(s) remain under the old master key: {names}. "
        f"Re-run with the same old and new keys to rotate the remainder; "
        f"already-rotated buckets are skipped."
    )


# ---------------------------------------------------------------------------
# Operator report
# ---------------------------------------------------------------------------


def format_report(result: RotationResult) -> str:
    """Render the operator-facing rotation report (requirement 13.9).

    Lists exactly which buckets were rotated in this run, which were already
    rotated (skipped), and which remain under the old key, so the operator can
    see the precise before/after state and knows whether a re-run is needed.
    Returns the report as a single string with no trailing newline; the caller
    emits it.
    """
    lines: list[str] = []
    if result.completed:
        lines.append("Master key rotation complete.")
    else:
        lines.append("Master key rotation incomplete — see error below.")
    lines.append(f"  Rotated now:      {len(result.rotated)}")
    lines.append(f"  Already rotated:  {len(result.already_rotated)}")
    lines.append(f"  Not rotated:      {len(result.not_rotated)}")
    lines.append("")

    if not result.outcomes:
        lines.append("  (no buckets found)")
        return "\n".join(lines)

    for outcome in result.rotated:
        lines.append(
            f"  [rotated]         {outcome.bucket_name} "
            f"(channel {outcome.channel_id}) -> kek_version {outcome.kek_version}"
        )
    for outcome in result.already_rotated:
        lines.append(
            f"  [already rotated] {outcome.bucket_name} "
            f"(channel {outcome.channel_id}) at kek_version {outcome.kek_version}"
        )
    for outcome in result.not_rotated:
        lines.append(
            f"  [NOT rotated]     {outcome.bucket_name} "
            f"(channel {outcome.channel_id}) at kek_version {outcome.kek_version}"
        )

    if result.error is not None:
        lines.append("")
        lines.append(f"  {result.error}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RotationArgs:
    """Parsed command-line arguments for ``cli/rotate_master_key``."""

    old_master_key: str
    new_master_key: str
    db_url: str


def parse_args(
    argv: Sequence[str] | None = None, env: dict[str, str] | None = None
) -> RotationArgs:
    """Parse command-line arguments for ``cli/rotate_master_key``.

    Exposes ``--old-master-key``, ``--new-master-key`` and ``--db-url``
    (requirement 13.8). The old master key falls back to the ``TGOS3_MASTER_KEY``
    environment variable when ``--old-master-key`` is omitted, matching the
    gateway's own configuration source; the new key must always be given
    explicitly so a rotation cannot be issued by accident.
    """
    source = os.environ if env is None else env
    parser = argparse.ArgumentParser(
        prog="tgobjects3-rotate-master-key",
        description=(
            "Rotate the master key: re-wrap every bucket KEK under the new "
            "master key, publish a new BucketManifest to each bucket channel, "
            "and update the index. Safe to re-run after a partial failure — "
            "already-rotated buckets are skipped."
        ),
    )
    parser.add_argument(
        "--old-master-key",
        default=None,
        help=(
            "Current master key whose root key currently wraps the bucket KEKs. "
            "Defaults to the TGOS3_MASTER_KEY environment variable when omitted."
        ),
    )
    parser.add_argument(
        "--new-master-key",
        required=True,
        help="New master key to re-wrap every bucket KEK under.",
    )
    parser.add_argument(
        "--db-url",
        required=True,
        help="PostgreSQL connection URL holding the bucket index.",
    )

    ns = parser.parse_args(argv)

    old_master_key = ns.old_master_key or source.get("TGOS3_MASTER_KEY")
    if not old_master_key:
        parser.error(
            "the old master key is required: pass --old-master-key or set "
            "TGOS3_MASTER_KEY"
        )
    if not ns.new_master_key:
        parser.error("--new-master-key must not be empty")
    if not ns.db_url:
        parser.error("--db-url must not be empty")
    if old_master_key == ns.new_master_key:
        parser.error("the new master key must differ from the old master key")

    return RotationArgs(
        old_master_key=old_master_key,
        new_master_key=ns.new_master_key,
        db_url=ns.db_url,
    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


async def _run_with_telethon(args: RotationArgs) -> RotationResult:
    """Build a connected Telethon-backed port and run the rotation through it.

    The adapter is connected before the run and disconnected afterwards even on
    failure, so the single MTProto session is always released. Telethon is
    imported lazily so importing this module and testing :func:`run_rotation`
    against a fake port need neither Telethon nor credentials.
    """
    from gateway.app.telegram.adapter import TelethonAdapter

    from cli.rebuild import _build_telegram_client

    creds: TelegramCredentials = load_credentials()
    adapter = TelethonAdapter(_build_telegram_client(creds))
    await adapter.connect()
    try:
        return await run_rotation(args, adapter)
    finally:
        await adapter.disconnect()


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for ``cli/rotate_master_key``: parse, rotate, report.

    Parses the command line, builds the Telegram port from environment
    credentials, runs the rotation via :func:`asyncio.run`, prints the operator
    report (requirement 13.9) to stdout, and returns a process exit code: ``0``
    when every bucket ended under the new key, ``1`` when the rotation stopped
    before completing (the report names the unrotated buckets so the operator
    can re-run). Credential and argument problems exit with code ``2``.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = parse_args(argv)
    try:
        result = asyncio.run(_run_with_telethon(args))
    except TelegramUnavailableError as exc:
        print(f"rotation failed: Telegram unavailable: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except FloodWaitError as exc:
        print(
            f"rotation failed: Telegram flood-wait "
            f"(retry after {exc.retry_after}s)",
            file=sys.stderr,
        )
        return EXIT_ERROR
    except OSError as exc:
        print(f"rotation failed: {exc}", file=sys.stderr)
        return EXIT_ERROR

    print(format_report(result))
    return EXIT_OK if result.completed else EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover - exercised via the console entry
    raise SystemExit(main())

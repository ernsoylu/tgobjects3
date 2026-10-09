"""Recovery CLI — channel scanner, message classifier and decryption layer.

This module is the first slice of the Recovery CLI (``cli/rebuild``). It
rebuilds the understanding of a bucket channel's contents from Telegram alone,
without any prior PostgreSQL state, by:

* scanning a channel's messages in ascending ``message_id`` order through the
  :class:`~gateway.app.telegram.port.TelegramPort` boundary (requirement 15.1);
* using the Checkpoint with the highest ``message_id`` as the scan start point,
  so only messages strictly newer than that checkpoint's recorded high-water
  mark are reprocessed (requirement 15.2);
* classifying each message by its caption prefix (the ``tgo3-*`` scheme from the
  design's Telegram Message Format table); and
* decrypting :class:`~gateway.app.format.types.BlobManifest` and
  :class:`~gateway.app.format.types.CommitRecord` payloads using the bucket KEK
  recovered from the channel's :class:`~gateway.app.format.types.BucketManifest`
  message(s), which is itself unwrapped by the HKDF root key derived from the
  master key (requirements 15.3, 15.7).

Scope boundary (task 11.1 only). This layer is deliberately pure with respect to
PostgreSQL and user-facing output: :func:`scan_channel` *yields* typed
:class:`ScanOutcome` results — one per message — and never writes to a database
or prints a report. Index reconstruction (task 11.2) and the final CLI output /
gap reporting (task 11.3) consume this stream and build on it. Keeping the
scan/parse/decrypt concern isolated here lets those tasks slot in without
reshaping this code.

The format itself is frozen at version 1 (requirement 16): a payload whose
``format_version`` differs is surfaced as a :class:`FormatVersionRejected`
outcome carrying the exact value found, the message id and the channel, rather
than being interpreted (requirements 16.3, 16.4). Any other decode or decrypt
failure becomes an :class:`UnreadableMessage` outcome so the caller can count
and skip it and continue scanning (requirement 15.1).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from gateway.app.db.models import Generation, ObjectHead
from gateway.app.format import (
    blob_manifest as blob_manifest_codec,
)
from gateway.app.format import (
    bucket_manifest as bucket_manifest_codec,
)
from gateway.app.format import (
    checkpoint as checkpoint_codec,
)
from gateway.app.format import (
    commit_record as commit_record_codec,
)
from gateway.app.format import (
    tombstone as tombstone_codec,
)
from gateway.app.format.key_wrap import KeyUnwrapError, derive_root_key, unwrap
from gateway.app.format.types import (
    BlobManifest,
    BucketManifest,
    Checkpoint,
    CommitRecord,
    FormatVersionError,
    Tombstone,
)
from gateway.app.telegram.errors import FloodWaitError, TelegramUnavailableError
from gateway.app.telegram.port import TelegramMessage, TelegramPort
from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

if TYPE_CHECKING:  # pragma: no cover - import only for type checking
    from telethon import TelegramClient

__all__ = [
    "DEFAULT_SCAN_BATCH_SIZE",
    "ChannelRebuildResult",
    "GapRange",
    "MessageKind",
    "ParsedCheckpoint",
    "RebuildArgs",
    "RebuildResult",
    "ScanOutcome",
    "FormatVersionRejected",
    "UnreadableMessage",
    "ParsedBucketManifest",
    "ParsedBlobManifest",
    "ParsedCommitRecord",
    "ParsedTombstone",
    "ParsedCheckpointMessage",
    "SkipReason",
    "TelegramCredentials",
    "classify_caption",
    "format_report",
    "load_credentials",
    "main",
    "run_rebuild",
    "find_checkpoint_start",
    "parse_args",
    "reconstruct_channel",
    "scan_channel",
]

logger = logging.getLogger("tgobjects3.rebuild")

#: Caption prefixes from the design's "Telegram Message Format" table. Each
#: message's logical role is recovered from the prefix of its caption; the
#: suffix after the prefix carries the logical identity (blob id, generation id,
#: KEK version or timestamp) but is not needed to classify or decode the body.
_PREFIX_DATA = "tgo3-d-"
_PREFIX_BLOB_MANIFEST = "tgo3-b-"
_PREFIX_COMMIT = "tgo3-c-"
_PREFIX_TOMBSTONE = "tgo3-t-"
_PREFIX_BUCKET_MANIFEST = "tgo3-k-"
_PREFIX_CHECKPOINT = "tgo3-p-"

#: Number of consecutive message ids requested per :meth:`TelegramPort.get_messages`
#: call while scanning a channel in ascending order.
DEFAULT_SCAN_BATCH_SIZE = 200

#: How many consecutive empty batches (no message found for any requested id)
#: end the scan. Telegram message ids are per-channel monotonic but not gap
#: free; a run of fully empty batches past the last seen message signals the
#: tail of the channel has been reached.
_EMPTY_BATCH_STOP = 3


class MessageKind(Enum):
    """The logical role of a channel message, derived from its caption prefix.

    :attr:`UNKNOWN` covers any message whose caption does not match one of the
    ``tgo3-*`` prefixes (including an empty caption) — such a message carries no
    recoverable format-layer content and is skipped by the scanner.
    """

    DATA = "data"
    BLOB_MANIFEST = "blob_manifest"
    COMMIT = "commit"
    TOMBSTONE = "tombstone"
    BUCKET_MANIFEST = "bucket_manifest"
    CHECKPOINT = "checkpoint"
    UNKNOWN = "unknown"


class SkipReason(Enum):
    """Why a message was skipped (surfaced on :class:`UnreadableMessage`).

    These let task 11.3 report *why* a message counted toward the skipped total
    without re-deriving the cause.
    """

    #: The message carried no caption prefix we recognise (not a format message).
    NOT_A_FORMAT_MESSAGE = "not_a_format_message"
    #: The message had a recognised caption but no downloadable document body.
    MISSING_DOCUMENT = "missing_document"
    #: The document body failed to download from Telegram.
    DOWNLOAD_FAILED = "download_failed"
    #: The msgpack payload could not be decoded into its declared model.
    DECODE_FAILED = "decode_failed"
    #: No bucket KEK was available to decrypt a BlobManifest / CommitRecord.
    KEK_UNAVAILABLE = "kek_unavailable"
    #: A wrapped key failed its AES Key Wrap integrity check.
    KEY_UNWRAP_FAILED = "key_unwrap_failed"


def _reject_format_version(
    channel_id: int, message_id: int, kind: MessageKind, found: int
) -> FormatVersionRejected:
    """Build — and log — a :class:`FormatVersionRejected` outcome.

    Requirements 16.3 and 16.4: the Recovery CLI rejects any message whose
    ``format_version`` is not 1 without processing its content, and logs the
    rejection together with the Telegram message id, the channel, and the exact
    version value found. Centralising construction here guarantees every
    rejection — whatever message kind produced it — is logged with that same
    required detail before it is surfaced to the caller.
    """
    logger.warning(
        "rejecting %s message: unsupported format_version %r "
        "(channel=%d message_id=%d)",
        kind.value,
        found,
        channel_id,
        message_id,
    )
    return FormatVersionRejected(
        channel_id=channel_id,
        message_id=message_id,
        kind=kind,
        found=found,
    )


def classify_caption(caption: str) -> MessageKind:
    """Classify a message by the prefix of its caption.

    Returns :attr:`MessageKind.UNKNOWN` for an empty caption or any caption that
    does not start with a known ``tgo3-*`` prefix.
    """
    if caption.startswith(_PREFIX_DATA):
        return MessageKind.DATA
    if caption.startswith(_PREFIX_BLOB_MANIFEST):
        return MessageKind.BLOB_MANIFEST
    if caption.startswith(_PREFIX_COMMIT):
        return MessageKind.COMMIT
    if caption.startswith(_PREFIX_TOMBSTONE):
        return MessageKind.TOMBSTONE
    if caption.startswith(_PREFIX_BUCKET_MANIFEST):
        return MessageKind.BUCKET_MANIFEST
    if caption.startswith(_PREFIX_CHECKPOINT):
        return MessageKind.CHECKPOINT
    return MessageKind.UNKNOWN


# ---------------------------------------------------------------------------
# Scan outcomes
#
# One outcome is yielded per message the scanner visits. They form a small
# closed set so task 11.2 (index reconstruction) can dispatch on type and
# task 11.3 (reporting) can tally skips, rejections and gaps without this
# layer knowing anything about PostgreSQL or stdout.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _MessageOutcome:
    """Common identity carried by every scan outcome."""

    channel_id: int
    message_id: int


@dataclass(frozen=True)
class ParsedBucketManifest(_MessageOutcome):
    """A successfully decoded :class:`BucketManifest` message."""

    manifest: BucketManifest


@dataclass(frozen=True)
class ParsedBlobManifest(_MessageOutcome):
    """A successfully decoded and decrypted :class:`BlobManifest` message.

    ``blob_key`` is the per-blob AES key recovered by unwrapping
    ``manifest.key_wrapped`` with the bucket KEK; downstream record decryption
    (outside this task) uses it with :mod:`gateway.app.format.record_codec`.
    """

    manifest: BlobManifest
    blob_key: bytes


@dataclass(frozen=True)
class ParsedCommitRecord(_MessageOutcome):
    """A successfully decoded :class:`CommitRecord` message.

    A CommitRecord's msgpack body is not itself encrypted — it is the durability
    boundary that makes a generation recoverable — but recovery is gated on the
    channel's bucket KEK being present (requirement 15.3): absent a KEK, a
    channel's commit records are treated as unreadable. When the KEK is present
    the record is decoded and surfaced here for task 11.2 to turn into a
    generation row.
    """

    record: CommitRecord


@dataclass(frozen=True)
class ParsedTombstone(_MessageOutcome):
    """A successfully decoded :class:`Tombstone` message."""

    tombstone: Tombstone


@dataclass(frozen=True)
class ParsedCheckpointMessage(_MessageOutcome):
    """A successfully decoded :class:`Checkpoint` message seen during the scan."""

    checkpoint: Checkpoint


@dataclass(frozen=True)
class FormatVersionRejected(_MessageOutcome):
    """A message rejected because its ``format_version`` was not 1.

    Carries the exact ``found`` version alongside the message id and channel so
    task 11.3 can log the rejection as required by 16.3 and 16.4.
    """

    kind: MessageKind
    found: int


@dataclass(frozen=True)
class UnreadableMessage(_MessageOutcome):
    """A message that was skipped because it could not be read or decrypted.

    ``reason`` records the cause (see :class:`SkipReason`); ``kind`` is the
    classified role when known. These feed the skipped-message counter the
    Recovery CLI reports on completion (requirements 15.1, 15.3).
    """

    kind: MessageKind
    reason: SkipReason
    detail: str = ""


#: Union of every outcome :func:`scan_channel` can yield.
ScanOutcome = (
    ParsedBucketManifest
    | ParsedBlobManifest
    | ParsedCommitRecord
    | ParsedTombstone
    | ParsedCheckpointMessage
    | FormatVersionRejected
    | UnreadableMessage
)


@dataclass(frozen=True)
class ParsedCheckpoint:
    """The checkpoint chosen as the scan start point.

    ``message_id`` is the Telegram message id of the highest-id Checkpoint found
    in the system channel; ``high_water`` is that checkpoint's per-channel map of
    the highest included message id. For a given bucket channel the scan begins
    strictly after ``high_water[str(channel_id)]`` when present (requirement
    15.2).
    """

    message_id: int
    checkpoint: Checkpoint

    def start_after(self, channel_id: int) -> int:
        """Return the exclusive lower bound ``message_id`` to scan a channel from.

        Messages are scanned only when their id is strictly greater than the
        returned value. When the checkpoint recorded no high-water mark for the
        channel, returns ``0`` so the whole channel is scanned.
        """
        return self.checkpoint.high_water.get(str(channel_id), 0)


# ---------------------------------------------------------------------------
# Checkpoint discovery (scan start point)
# ---------------------------------------------------------------------------


async def find_checkpoint_start(
    port: TelegramPort,
    system_channel_id: int,
    *,
    batch_size: int = DEFAULT_SCAN_BATCH_SIZE,
) -> ParsedCheckpoint | None:
    """Find the Checkpoint with the highest ``message_id`` in the system channel.

    Scans the system channel's messages, decodes every ``tgo3-p-*`` checkpoint,
    and returns the one with the greatest Telegram ``message_id`` (requirement
    15.2). Checkpoint payloads are plain msgpack (the embedded ``pg_dump_enc``
    envelope is opaque here and is not decrypted at this stage), so no KEK is
    needed to locate the start point.

    Returns ``None`` when the system channel holds no readable checkpoint, in
    which case the caller scans bucket channels from the beginning.

    A checkpoint message whose ``format_version`` is not 1, or whose body fails
    to decode, is ignored for start-point selection; such rejections are logged
    and surfaced per-message when the system channel is itself scanned.
    """
    best: ParsedCheckpoint | None = None
    async for message in _iter_messages_ascending(
        port, system_channel_id, start_after=0, batch_size=batch_size
    ):
        if classify_caption(message.caption) is not MessageKind.CHECKPOINT:
            continue
        checkpoint = await _decode_checkpoint(port, system_channel_id, message)
        if checkpoint is None:
            continue
        if best is None or message.message_id > best.message_id:
            best = ParsedCheckpoint(message_id=message.message_id, checkpoint=checkpoint)
    return best


async def _decode_checkpoint(
    port: TelegramPort, channel_id: int, message: TelegramMessage
) -> Checkpoint | None:
    """Download and decode a checkpoint message, or ``None`` if unusable."""
    body = await _download(port, channel_id, message)
    if body is None:
        return None
    try:
        return checkpoint_codec.deserialise(body)
    except FormatVersionError as exc:
        logger.warning(
            "rejecting checkpoint: unsupported format_version %r "
            "(channel=%d message_id=%d)",
            exc.found,
            channel_id,
            message.message_id,
        )
        return None
    except (ValueError, KeyError) as exc:
        logger.warning(
            "skipping undecodable checkpoint (channel=%d message_id=%d): %s",
            channel_id,
            message.message_id,
            exc,
        )
        return None


# ---------------------------------------------------------------------------
# Channel scan
# ---------------------------------------------------------------------------


async def scan_channel(
    port: TelegramPort,
    channel_id: int,
    master_key: str,
    *,
    start_after: int = 0,
    batch_size: int = DEFAULT_SCAN_BATCH_SIZE,
) -> AsyncIterator[ScanOutcome]:
    """Scan one bucket channel and yield one typed outcome per message.

    Walks the channel in ascending ``message_id`` order starting strictly after
    ``start_after`` (requirements 15.1, 15.2). Each message is classified by
    caption prefix and, where it carries format-layer content, downloaded and
    decoded:

    * BucketManifest messages are decoded and their wrapped bucket KEK is
      unwrapped eagerly with the HKDF root key, so the recovered KEK is
      available to decrypt BlobManifest and CommitRecord messages encountered
      later in the ascending scan (requirements 15.3, 15.7). The decoded
      manifest is also yielded as a :class:`ParsedBucketManifest`.
    * BlobManifest messages are decoded and their per-blob key is unwrapped with
      the bucket KEK, yielding a :class:`ParsedBlobManifest`.
    * CommitRecord and Tombstone messages are decoded and yielded as
      :class:`ParsedCommitRecord` / :class:`ParsedTombstone`. CommitRecord
      decoding is gated on a bucket KEK being present for the channel: with no
      KEK the record is surfaced as an :class:`UnreadableMessage` with reason
      :attr:`SkipReason.KEK_UNAVAILABLE` (requirement 15.3).
    * Checkpoint messages found in the channel are decoded and yielded as
      :class:`ParsedCheckpointMessage`.
    * Data chunks (``tgo3-d-*``) carry raw sealed records and no index metadata,
      so they are not an outcome here; they are only fetched on demand during a
      read, not during index rebuild.

    Any message with an unsupported ``format_version`` is yielded as a
    :class:`FormatVersionRejected`; any other decode/download/unwrap failure is
    yielded as an :class:`UnreadableMessage`. The scan never raises on a single
    bad message — it records the outcome and continues (requirement 15.1).

    This function performs no database writes and prints nothing; it is the
    substrate tasks 11.2 and 11.3 build on.
    """
    root_key = derive_root_key(master_key)
    bucket_kek: bytes | None = None

    async for message in _iter_messages_ascending(
        port, channel_id, start_after=start_after, batch_size=batch_size
    ):
        kind = classify_caption(message.caption)

        if kind is MessageKind.DATA or kind is MessageKind.UNKNOWN:
            # Data chunks and non-format messages carry no index metadata.
            if kind is MessageKind.UNKNOWN:
                yield UnreadableMessage(
                    channel_id=channel_id,
                    message_id=message.message_id,
                    kind=kind,
                    reason=SkipReason.NOT_A_FORMAT_MESSAGE,
                )
            continue

        body = await _download(port, channel_id, message)
        if body is None:
            yield UnreadableMessage(
                channel_id=channel_id,
                message_id=message.message_id,
                kind=kind,
                reason=(
                    SkipReason.MISSING_DOCUMENT
                    if message.document_id is None
                    else SkipReason.DOWNLOAD_FAILED
                ),
            )
            continue

        if kind is MessageKind.BUCKET_MANIFEST:
            outcome, bucket_kek = _handle_bucket_manifest(
                channel_id, message, body, root_key, bucket_kek
            )
            yield outcome
        elif kind is MessageKind.BLOB_MANIFEST:
            yield _handle_blob_manifest(channel_id, message, body, bucket_kek)
        elif kind is MessageKind.COMMIT:
            yield _handle_commit(channel_id, message, body, bucket_kek)
        elif kind is MessageKind.TOMBSTONE:
            yield _handle_tombstone(channel_id, message, body)
        elif kind is MessageKind.CHECKPOINT:
            yield _handle_checkpoint(channel_id, message, body)


def _handle_bucket_manifest(
    channel_id: int,
    message: TelegramMessage,
    body: bytes,
    root_key: bytes,
    current_kek: bytes | None,
) -> tuple[ScanOutcome, bytes | None]:
    """Decode a BucketManifest and recover the bucket KEK.

    Returns the outcome to yield and the bucket KEK to carry forward. On any
    failure the previously recovered KEK (if any) is preserved so a single bad
    manifest does not strand an otherwise decryptable channel.
    """
    try:
        manifest = bucket_manifest_codec.deserialise(body)
    except FormatVersionError as exc:
        return (
            _reject_format_version(
                channel_id,
                message.message_id,
                MessageKind.BUCKET_MANIFEST,
                exc.found,
            ),
            current_kek,
        )
    except (ValueError, KeyError) as exc:
        return (
            UnreadableMessage(
                channel_id=channel_id,
                message_id=message.message_id,
                kind=MessageKind.BUCKET_MANIFEST,
                reason=SkipReason.DECODE_FAILED,
                detail=str(exc),
            ),
            current_kek,
        )

    try:
        kek = unwrap(root_key, manifest.kek_wrapped)
    except KeyUnwrapError as exc:
        return (
            UnreadableMessage(
                channel_id=channel_id,
                message_id=message.message_id,
                kind=MessageKind.BUCKET_MANIFEST,
                reason=SkipReason.KEY_UNWRAP_FAILED,
                detail=str(exc),
            ),
            current_kek,
        )

    return (
        ParsedBucketManifest(
            channel_id=channel_id,
            message_id=message.message_id,
            manifest=manifest,
        ),
        kek,
    )


def _handle_blob_manifest(
    channel_id: int,
    message: TelegramMessage,
    body: bytes,
    bucket_kek: bytes | None,
) -> ScanOutcome:
    """Decode a BlobManifest and unwrap its per-blob key with the bucket KEK."""
    if bucket_kek is None:
        return UnreadableMessage(
            channel_id=channel_id,
            message_id=message.message_id,
            kind=MessageKind.BLOB_MANIFEST,
            reason=SkipReason.KEK_UNAVAILABLE,
        )
    try:
        manifest = blob_manifest_codec.deserialise(body)
    except FormatVersionError as exc:
        return _reject_format_version(
            channel_id, message.message_id, MessageKind.BLOB_MANIFEST, exc.found
        )
    except (ValueError, KeyError) as exc:
        return UnreadableMessage(
            channel_id=channel_id,
            message_id=message.message_id,
            kind=MessageKind.BLOB_MANIFEST,
            reason=SkipReason.DECODE_FAILED,
            detail=str(exc),
        )

    try:
        blob_key = unwrap(bucket_kek, manifest.key_wrapped)
    except KeyUnwrapError as exc:
        return UnreadableMessage(
            channel_id=channel_id,
            message_id=message.message_id,
            kind=MessageKind.BLOB_MANIFEST,
            reason=SkipReason.KEY_UNWRAP_FAILED,
            detail=str(exc),
        )

    return ParsedBlobManifest(
        channel_id=channel_id,
        message_id=message.message_id,
        manifest=manifest,
        blob_key=blob_key,
    )


def _handle_commit(
    channel_id: int,
    message: TelegramMessage,
    body: bytes,
    bucket_kek: bytes | None,
) -> ScanOutcome:
    """Decode a CommitRecord, gated on the channel's bucket KEK being present.

    Requirement 15.3: absent a KEK / BucketManifest, all commit records in the
    channel are treated as unreadable.
    """
    if bucket_kek is None:
        return UnreadableMessage(
            channel_id=channel_id,
            message_id=message.message_id,
            kind=MessageKind.COMMIT,
            reason=SkipReason.KEK_UNAVAILABLE,
        )
    try:
        record = commit_record_codec.deserialise(body)
    except FormatVersionError as exc:
        return _reject_format_version(
            channel_id, message.message_id, MessageKind.COMMIT, exc.found
        )
    except (ValueError, KeyError) as exc:
        return UnreadableMessage(
            channel_id=channel_id,
            message_id=message.message_id,
            kind=MessageKind.COMMIT,
            reason=SkipReason.DECODE_FAILED,
            detail=str(exc),
        )
    return ParsedCommitRecord(
        channel_id=channel_id,
        message_id=message.message_id,
        record=record,
    )


def _handle_tombstone(
    channel_id: int,
    message: TelegramMessage,
    body: bytes,
) -> ScanOutcome:
    """Decode a Tombstone message."""
    try:
        tomb = tombstone_codec.deserialise(body)
    except FormatVersionError as exc:
        return _reject_format_version(
            channel_id, message.message_id, MessageKind.TOMBSTONE, exc.found
        )
    except (ValueError, KeyError) as exc:
        return UnreadableMessage(
            channel_id=channel_id,
            message_id=message.message_id,
            kind=MessageKind.TOMBSTONE,
            reason=SkipReason.DECODE_FAILED,
            detail=str(exc),
        )
    return ParsedTombstone(
        channel_id=channel_id,
        message_id=message.message_id,
        tombstone=tomb,
    )


def _handle_checkpoint(
    channel_id: int,
    message: TelegramMessage,
    body: bytes,
) -> ScanOutcome:
    """Decode a Checkpoint message encountered during a channel scan."""
    try:
        checkpoint = checkpoint_codec.deserialise(body)
    except FormatVersionError as exc:
        return _reject_format_version(
            channel_id, message.message_id, MessageKind.CHECKPOINT, exc.found
        )
    except (ValueError, KeyError) as exc:
        return UnreadableMessage(
            channel_id=channel_id,
            message_id=message.message_id,
            kind=MessageKind.CHECKPOINT,
            reason=SkipReason.DECODE_FAILED,
            detail=str(exc),
        )
    return ParsedCheckpointMessage(
        channel_id=channel_id,
        message_id=message.message_id,
        checkpoint=checkpoint,
    )


# ---------------------------------------------------------------------------
# Low-level Telegram helpers
# ---------------------------------------------------------------------------


async def _iter_messages_ascending(
    port: TelegramPort,
    channel_id: int,
    *,
    start_after: int,
    batch_size: int,
) -> AsyncIterator[TelegramMessage]:
    """Yield a channel's messages in ascending ``message_id`` order.

    The :class:`TelegramPort` boundary exposes message retrieval only by
    explicit id (:meth:`~gateway.app.telegram.port.TelegramPort.get_messages`),
    so the scan requests contiguous id windows ``(start_after, start_after +
    batch_size]`` and advances. Telegram ids are per-channel monotonic but not
    gap free (deleted/service messages leave holes), so each batch's returned
    messages are sorted by id before yielding, and the walk stops after
    :data:`_EMPTY_BATCH_STOP` consecutive wholly empty batches past the last
    message seen.
    """
    if batch_size < 1:
        raise ValueError(f"batch_size must be >= 1, got {batch_size}")

    next_id = start_after + 1
    empty_batches = 0
    while empty_batches < _EMPTY_BATCH_STOP:
        ids = list(range(next_id, next_id + batch_size))
        messages = await _get_messages(port, channel_id, ids)
        if messages:
            empty_batches = 0
            for message in sorted(messages, key=lambda m: m.message_id):
                yield message
        else:
            empty_batches += 1
        next_id += batch_size


async def _get_messages(
    port: TelegramPort, channel_id: int, ids: Sequence[int]
) -> list[TelegramMessage]:
    """Fetch a batch of messages, treating Telegram unavailability as empty.

    A transient Telegram error for a single id window is logged and treated as
    an empty batch rather than aborting the whole scan, keeping recovery
    best-effort over the reachable messages (requirement 15.1). Flood-wait is
    re-raised so the caller/driver can honour the backoff.
    """
    try:
        return await port.get_messages(channel_id, list(ids))
    except FloodWaitError:
        raise
    except TelegramUnavailableError as exc:
        logger.warning(
            "telegram unavailable fetching ids %d-%d on channel %d: %s",
            ids[0],
            ids[-1],
            channel_id,
            exc,
        )
        return []


async def _download(
    port: TelegramPort, channel_id: int, message: TelegramMessage
) -> bytes | None:
    """Download a message's document body, or ``None`` if unavailable.

    Returns ``None`` when the message has no document or the download fails, so
    the caller can record the appropriate skip reason and continue.
    """
    if message.document_id is None:
        return None
    try:
        return await port.download_document(channel_id, message.message_id)
    except FloodWaitError:
        raise
    except TelegramUnavailableError as exc:
        logger.warning(
            "download failed (channel=%d message_id=%d): %s",
            channel_id,
            message.message_id,
            exc,
        )
        return None


# ---------------------------------------------------------------------------
# Index reconstruction (task 11.2)
#
# Consumes the typed outcome stream from :func:`scan_channel` and writes the
# reconstructed object index into PostgreSQL. This layer owns the PostgreSQL
# session but no stdout: it returns a structured :class:`RebuildResult` that
# task 11.3 renders into the operator-facing report. Keeping the DB writes here
# and the formatting there means the counts, gaps and per-channel detail are
# computed exactly once and are independently testable.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GapRange:
    """A contiguous run of message ids holding no index-bearing message.

    Requirement 15.4 defines a gap as a range of one or more consecutive
    message ids within a scanned channel for which no Commit Record, Tombstone
    or system message (Checkpoint / BucketManifest) is found. ``start`` and
    ``end`` are inclusive. A single missing id yields ``start == end``.
    """

    start: int
    end: int

    @property
    def length(self) -> int:
        """Number of message ids covered by this gap (always ``>= 1``)."""
        return self.end - self.start + 1


@dataclass(frozen=True)
class ChannelRebuildResult:
    """Per-channel reconstruction detail for one bucket channel.

    Carries the counts and gap ranges task 11.3 reports. ``skipped`` is the sum
    of unreadable messages and format-version rejections (both count toward the
    skipped-message total of requirements 15.1 / 15.4). ``generations`` is the
    number of generation rows inserted from Commit Records; ``tombstones`` the
    number of Tombstones applied; ``heads`` the number of ``object_heads``
    entries that point at a generation after this channel was replayed.
    """

    channel_id: int
    generations: int = 0
    tombstones: int = 0
    heads: int = 0
    skipped: int = 0
    rejected_versions: int = 0
    missing_blob_links: int = 0
    gaps: tuple[GapRange, ...] = field(default=())


@dataclass(frozen=True)
class RebuildResult:
    """Aggregate reconstruction result across every scanned channel.

    Task 11.3 renders this into the completion report (reconstructed generation
    count, skipped-message count, gap ranges). The aggregate counters are the
    sums of the per-channel detail so the renderer never has to re-add them.
    """

    reconstructed_generations: int = 0
    applied_tombstones: int = 0
    skipped_messages: int = 0
    channels: tuple[ChannelRebuildResult, ...] = field(default=())

    @property
    def gap_count(self) -> int:
        """Total number of gap ranges detected across all channels."""
        return sum(len(c.gaps) for c in self.channels)


class _GapTracker:
    """Collects index-bearing message ids and derives the channel's gap ranges.

    A gap (requirement 15.4) is a run of consecutive message ids *between*
    index-bearing messages for which no Commit Record, Tombstone or system
    message exists. We record only the ids of index-bearing messages and, on
    :meth:`finish`, report every integer interval strictly between consecutive
    recorded ids. Ids before the first and after the last index-bearing message
    are not gaps — the channel's span is bounded by what it actually contains.
    """

    def __init__(self) -> None:
        self._ids: set[int] = set()

    def record(self, message_id: int) -> None:
        """Mark ``message_id`` as holding an index-bearing message."""
        self._ids.add(message_id)

    def finish(self) -> tuple[GapRange, ...]:
        """Return the gap ranges between the recorded index-bearing ids."""
        if len(self._ids) < 2:
            return ()
        ordered = sorted(self._ids)
        gaps: list[GapRange] = []
        for prev, nxt in zip(ordered, ordered[1:], strict=False):
            if nxt - prev > 1:
                gaps.append(GapRange(start=prev + 1, end=nxt - 1))
        return tuple(gaps)


class _HeadState:
    """Tracks the winning generation per key while a channel is replayed.

    The object head for a ``(bucket_id, key)`` points at the generation with
    the highest ``generation_number`` (overwrites win). A Tombstone for the
    current head clears it; a Tombstone for a superseded generation leaves the
    head untouched. The resolved heads are flushed to ``object_heads`` once the
    whole channel has been replayed, so a late-arriving higher generation never
    produces an intermediate head write that is immediately overwritten.
    """

    def __init__(self) -> None:
        # (bucket_id, key) -> (generation_number, generation_id, deleted)
        self._heads: dict[tuple[uuid.UUID, str], tuple[int, uuid.UUID, bool]] = {}

    def observe_commit(
        self, bucket_id: uuid.UUID, key: str, generation_number: int, generation_id: uuid.UUID
    ) -> None:
        """Record a committed generation, keeping the highest number per key."""
        slot = (bucket_id, key)
        current = self._heads.get(slot)
        if current is None or generation_number >= current[0]:
            self._heads[slot] = (generation_number, generation_id, False)

    def observe_tombstone(
        self, bucket_id: uuid.UUID, key: str, generation_number: int
    ) -> None:
        """Record a delete, clearing the head only when it is the current one."""
        slot = (bucket_id, key)
        current = self._heads.get(slot)
        if current is not None and generation_number >= current[0]:
            self._heads[slot] = (generation_number, current[1], True)

    def live_heads(self) -> dict[tuple[uuid.UUID, str], uuid.UUID]:
        """Return ``(bucket_id, key) -> generation_id`` for non-deleted heads."""
        return {
            slot: gen_id
            for slot, (_num, gen_id, deleted) in self._heads.items()
            if not deleted
        }

    def deleted_heads(self) -> list[tuple[uuid.UUID, str]]:
        """Return the ``(bucket_id, key)`` slots whose final state is deleted."""
        return [
            slot for slot, (_num, _gen_id, deleted) in self._heads.items() if deleted
        ]


def _generation_values_from_commit(
    record: CommitRecord, commit_message_id: int
) -> dict[str, object]:
    """Build the ``generations`` row values from a Commit Record.

    The generation's ``id`` is the Commit Record's ``generation_id`` so that a
    later Tombstone (which carries the same ``generation_id``) can mark exactly
    this row DELETED. ``blob_id`` is left NULL: this reconstruction pass rebuilds
    the object index from Commit Records and Tombstones alone and does not
    re-materialise ``blobs`` rows, so the generation→blob link is deferred (the
    caller counts these as ``missing_blob_links``). ``commit_message_id`` records
    where the record was found for traceability.
    """
    return {
        "id": record.generation_id,
        "bucket_id": record.bucket_id,
        "key": record.key,
        "kind": "object",
        "blob_id": None,
        "size": 0,
        "etag": record.etag,
        "checksums": dict(record.checksums),
        "content_type": record.content_type,
        "user_meta": dict(record.user_meta),
        "last_modified": record.last_modified,
        "state": "COMMITTED",
        "commit_message_id": commit_message_id,
        "staging_path": None,
    }


async def reconstruct_channel(
    session: AsyncSession,
    outcomes: AsyncIterator[ScanOutcome],
) -> ChannelRebuildResult:
    """Replay one channel's scan outcomes into PostgreSQL.

    Consumes every outcome :func:`scan_channel` yields for a single channel and
    applies it to the index (requirements 14.4, 15.1, 15.4):

    * :class:`ParsedCommitRecord` → upsert a ``generations`` row in state
      ``COMMITTED`` keyed on the record's ``generation_id`` (requirement 14.4).
      The highest ``generation_number`` per key wins the object head.
    * :class:`ParsedTombstone` → mark the referenced generation ``DELETED`` and,
      when it is the current head, clear the head (requirement 15.1).
    * :class:`ParsedBucketManifest` / :class:`ParsedBlobManifest` /
      :class:`ParsedCheckpointMessage` → structural / system messages: they are
      not index rows here but they *do* count as index-bearing for gap
      detection, so a run of them never registers as a gap.
    * :class:`UnreadableMessage` / :class:`FormatVersionRejected` → skipped;
      counted toward the skipped total and treated as a gap candidate
      (requirement 15.4).

    All writes for the channel share the caller's ``session`` and are flushed
    but not committed here, so the caller controls the transaction boundary
    across multiple channels. The object heads are resolved in memory and
    written once the channel is fully replayed, keeping the final head pointing
    at the highest generation number even when records arrive out of order.
    """
    channel_id: int | None = None
    generations = 0
    tombstones = 0
    skipped = 0
    rejected = 0
    missing_blob_links = 0
    heads = _HeadState()
    gaps = _GapTracker()

    async for outcome in outcomes:
        channel_id = outcome.channel_id

        if isinstance(outcome, ParsedCommitRecord):
            gaps.record(outcome.message_id)
            record = outcome.record
            await _upsert_generation(session, record, outcome.message_id)
            heads.observe_commit(
                record.bucket_id,
                record.key,
                record.generation_number,
                record.generation_id,
            )
            missing_blob_links += 1
            generations += 1
        elif isinstance(outcome, ParsedTombstone):
            gaps.record(outcome.message_id)
            tomb = outcome.tombstone
            applied = await _apply_tombstone(session, tomb)
            heads.observe_tombstone(tomb.bucket_id, tomb.key, tomb.generation_number)
            if applied:
                tombstones += 1
        elif isinstance(
            outcome,
            ParsedBucketManifest | ParsedBlobManifest | ParsedCheckpointMessage,
        ):
            # System / structural messages are index-bearing for gap purposes
            # (requirement 15.4) but produce no index row in this pass.
            gaps.record(outcome.message_id)
        elif isinstance(outcome, FormatVersionRejected):
            skipped += 1
            rejected += 1
        elif isinstance(outcome, UnreadableMessage):
            skipped += 1

    head_rows = await _flush_heads(session, heads)

    return ChannelRebuildResult(
        channel_id=channel_id if channel_id is not None else 0,
        generations=generations,
        tombstones=tombstones,
        heads=head_rows,
        skipped=skipped,
        rejected_versions=rejected,
        missing_blob_links=missing_blob_links,
        gaps=gaps.finish(),
    )


async def _upsert_generation(
    session: AsyncSession, record: CommitRecord, commit_message_id: int
) -> None:
    """Insert or update the ``generations`` row for a Commit Record.

    Idempotent on the record's ``generation_id``: re-running recovery, or a
    channel that happens to carry the same Commit Record twice, converges on a
    single COMMITTED row rather than failing on the primary key.
    """
    values = _generation_values_from_commit(record, commit_message_id)
    stmt = pg_insert(Generation).values(**values)
    update_cols = {k: stmt.excluded[k] for k in values if k != "id"}
    stmt = stmt.on_conflict_do_update(index_elements=[Generation.id], set_=update_cols)
    await session.execute(stmt)


async def _apply_tombstone(session: AsyncSession, tomb: Tombstone) -> bool:
    """Mark the generation referenced by a Tombstone ``DELETED``.

    Returns ``True`` when a matching generation row was updated. A Tombstone for
    a generation that was never reconstructed (its Commit Record is missing or
    unreadable) updates nothing and returns ``False``; the delete intent is
    still reflected in the in-memory head state so the head is not resurrected.
    """
    result = await session.execute(
        select(Generation).where(Generation.id == tomb.generation_id)
    )
    generation = result.scalar_one_or_none()
    if generation is None:
        return False
    generation.state = "DELETED"
    return True


async def _flush_heads(session: AsyncSession, heads: _HeadState) -> int:
    """Write the resolved object heads for a channel to ``object_heads``.

    Live heads (highest-numbered non-deleted generation per key) are upserted so
    overwrites land on the correct generation. Keys whose head was tombstoned
    have any existing head row removed, so a rebuilt index never points at a
    deleted generation.
    """
    live = heads.live_heads()
    written = 0
    for (bucket_id, key), generation_id in live.items():
        stmt = pg_insert(ObjectHead).values(
            bucket_id=bucket_id, key=key, generation_id=generation_id
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[ObjectHead.bucket_id, ObjectHead.key],
            set_={"generation_id": stmt.excluded.generation_id},
        )
        await session.execute(stmt)
        written += 1

    # Remove heads for keys whose final state in this channel is deleted.
    for bucket_id, key in heads.deleted_heads():
        await session.execute(
            sa_delete(ObjectHead).where(
                ObjectHead.bucket_id == bucket_id, ObjectHead.key == key
            )
        )

    await session.flush()
    return written


def create_rebuild_engine_sessionmaker(
    db_url: str,
) -> async_sessionmaker[AsyncSession]:
    """Build an async session factory for the reconstruction writes.

    The Recovery CLI runs standalone with no gateway process, so it owns its own
    engine. The caller is responsible for disposing of the underlying engine via
    the returned factory's bind when the run completes.
    """
    engine = create_async_engine(db_url)
    return async_sessionmaker(engine, expire_on_commit=False)


# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RebuildArgs:
    """Parsed command-line arguments for the recovery CLI."""

    channel_id: int
    master_key: str
    db_url: str
    system_channel_id: int
    extra_channel_ids: tuple[int, ...] = field(default=())


def parse_args(argv: Sequence[str] | None = None) -> RebuildArgs:
    """Parse command-line arguments for ``cli/rebuild``.

    Exposes the four arguments task 11.1 requires: ``--channel-id``,
    ``--master-key``, ``--db-url`` and ``--system-channel-id``. ``--channel-id``
    may be repeated to recover more than one bucket channel in a single run; the
    first value is the primary channel and any additional values are collected
    into :attr:`RebuildArgs.extra_channel_ids`.
    """
    parser = argparse.ArgumentParser(
        prog="tgobjects3-rebuild",
        description=(
            "Rebuild the PostgreSQL index from Telegram alone: scan bucket "
            "channels in ascending message-id order, decrypt commit records and "
            "tombstones using the master key and per-bucket KEKs, and "
            "reconstruct the object index."
        ),
    )
    parser.add_argument(
        "--channel-id",
        dest="channel_ids",
        type=int,
        action="append",
        required=True,
        metavar="CHANNEL_ID",
        help=(
            "Bucket channel id to scan. May be given more than once to recover "
            "several channels in one run."
        ),
    )
    parser.add_argument(
        "--master-key",
        required=True,
        help="Master key used to derive the root key that unwraps bucket KEKs.",
    )
    parser.add_argument(
        "--db-url",
        required=True,
        help="PostgreSQL connection URL the reconstructed index is written to.",
    )
    parser.add_argument(
        "--system-channel-id",
        type=int,
        required=True,
        help=(
            "System channel id holding checkpoints; its highest-id checkpoint "
            "sets the scan start point."
        ),
    )

    ns = parser.parse_args(argv)

    channel_ids: list[int] = ns.channel_ids
    if not ns.master_key:
        parser.error("--master-key must not be empty")
    if not ns.db_url:
        parser.error("--db-url must not be empty")

    return RebuildArgs(
        channel_id=channel_ids[0],
        master_key=ns.master_key,
        db_url=ns.db_url,
        system_channel_id=ns.system_channel_id,
        extra_channel_ids=tuple(channel_ids[1:]),
    )


# ---------------------------------------------------------------------------
# Orchestration (task 11.3)
#
# Ties the scan (11.1) and reconstruction (11.2) layers into a single runnable
# pass over one or more bucket channels. This function owns the transaction
# boundary: each channel is replayed inside its own session and committed only
# when its reconstruction completes without raising, so a failure mid-channel
# never leaves a partially-rebuilt index behind. It then aggregates the
# per-channel detail into the :class:`RebuildResult` the reporter renders.
# ---------------------------------------------------------------------------


async def run_rebuild(
    args: RebuildArgs,
    port: TelegramPort,
    *,
    batch_size: int = DEFAULT_SCAN_BATCH_SIZE,
) -> RebuildResult:
    """Rebuild the object index for every requested channel from Telegram alone.

    Orchestrates the full recovery pass (requirements 15.1–15.4):

    1. Locate the scan start point by finding the highest-``message_id``
       Checkpoint in the system channel (requirement 15.2). When no checkpoint
       is present each channel is scanned from the beginning.
    2. For the primary channel and every extra channel, scan it from the
       checkpoint's per-channel high-water mark and replay the resulting outcome
       stream into PostgreSQL within a single transaction, committing on success
       (requirement 14.4 durability of the rebuilt rows).
    3. Aggregate the per-channel :class:`ChannelRebuildResult` values into one
       :class:`RebuildResult` whose top-level counters are the sums across
       channels, ready for :func:`format_report`.

    The database engine is created from ``args.db_url`` and disposed before
    returning so the CLI leaves no connection pool open. A channel whose
    reconstruction raises aborts that channel's transaction and propagates, so
    partial index state is never committed.

    Args:
        args: Parsed CLI arguments carrying the channel ids, master key,
            database URL and system channel id.
        port: The Telegram boundary used for all reads. Injected so the
            orchestration is testable against a fake port without Telethon.
        batch_size: Consecutive message-id window size per scan request.

    Returns:
        The aggregated :class:`RebuildResult` across every scanned channel.
    """
    checkpoint = await find_checkpoint_start(
        port, args.system_channel_id, batch_size=batch_size
    )
    if checkpoint is None:
        logger.info(
            "no checkpoint found in system channel %d; scanning channels from "
            "the beginning",
            args.system_channel_id,
        )
    else:
        logger.info(
            "using checkpoint at message_id=%d as scan start point",
            checkpoint.message_id,
        )

    channel_ids = (args.channel_id, *args.extra_channel_ids)
    engine = create_async_engine(args.db_url)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    channel_results: list[ChannelRebuildResult] = []
    try:
        for channel_id in channel_ids:
            start_after = checkpoint.start_after(channel_id) if checkpoint else 0
            logger.info(
                "scanning channel %d from message_id > %d",
                channel_id,
                start_after,
            )
            result = await _rebuild_one_channel(
                sessionmaker,
                port,
                channel_id,
                args.master_key,
                start_after=start_after,
                batch_size=batch_size,
            )
            channel_results.append(result)
    finally:
        await engine.dispose()

    return _aggregate(channel_results)


async def _rebuild_one_channel(
    sessionmaker: async_sessionmaker[AsyncSession],
    port: TelegramPort,
    channel_id: int,
    master_key: str,
    *,
    start_after: int,
    batch_size: int,
) -> ChannelRebuildResult:
    """Scan and reconstruct one channel inside its own committed transaction.

    The scan outcome stream for the channel is replayed by
    :func:`reconstruct_channel` within a single session; the transaction is
    committed only once the whole channel has been replayed without raising, so
    an interrupted channel contributes nothing to the index.
    """
    outcomes = scan_channel(
        port,
        channel_id,
        master_key,
        start_after=start_after,
        batch_size=batch_size,
    )
    async with sessionmaker() as session, session.begin():
        return await reconstruct_channel(session, outcomes)


def _aggregate(results: Sequence[ChannelRebuildResult]) -> RebuildResult:
    """Fold per-channel results into one aggregate :class:`RebuildResult`."""
    return RebuildResult(
        reconstructed_generations=sum(r.generations for r in results),
        applied_tombstones=sum(r.tombstones for r in results),
        skipped_messages=sum(r.skipped for r in results),
        channels=tuple(results),
    )


# ---------------------------------------------------------------------------
# Completion report (task 11.3)
#
# Renders a :class:`RebuildResult` into the operator-facing report required by
# requirement 15.4: the number of reconstructed generations, the number of
# skipped / unreadable messages, and any gaps detected — broken out per channel
# so an operator can see exactly which channel carried which detail. The
# renderer is pure (it returns a string) so it is trivially testable; the CLI
# prints what it returns.
# ---------------------------------------------------------------------------


def format_report(result: RebuildResult) -> str:
    """Render the completion report for a finished rebuild (requirement 15.4).

    Produces a human-readable, multi-line report with a top-level summary — the
    total reconstructed generation count and the total skipped-message count —
    followed by a per-channel breakdown that lists each channel's generation,
    tombstone and head counts, its skipped and version-rejected tallies, and the
    exact gap ranges detected for it. Channels whose Commit Records were all
    treated as unreadable because no bucket KEK / Bucket Manifest was present
    (requirement 15.3) surface naturally here as a channel that reconstructed
    zero generations with every commit counted toward ``skipped``.

    Returns the report as a single string with no trailing newline; the caller
    is responsible for emitting it.
    """
    lines: list[str] = []
    lines.append("Recovery complete.")
    lines.append(f"  Reconstructed generations: {result.reconstructed_generations}")
    lines.append(f"  Applied tombstones:        {result.applied_tombstones}")
    lines.append(f"  Skipped/unreadable msgs:   {result.skipped_messages}")
    lines.append(f"  Gap ranges detected:       {result.gap_count}")
    lines.append("")

    if not result.channels:
        lines.append("  (no channels scanned)")
        return "\n".join(lines)

    for channel in result.channels:
        lines.append(f"  Channel {channel.channel_id}:")
        lines.append(f"    generations reconstructed: {channel.generations}")
        lines.append(f"    tombstones applied:        {channel.tombstones}")
        lines.append(f"    object heads written:      {channel.heads}")
        lines.append(f"    skipped/unreadable:        {channel.skipped}")
        lines.append(f"    format-version rejected:   {channel.rejected_versions}")
        if channel.missing_blob_links:
            lines.append(
                f"    generations missing blob link: {channel.missing_blob_links}"
            )
        if channel.gaps:
            lines.append(f"    gaps ({len(channel.gaps)}):")
            for gap in channel.gaps:
                if gap.start == gap.end:
                    lines.append(f"      - message_id {gap.start}")
                else:
                    lines.append(
                        f"      - message_id {gap.start}-{gap.end} "
                        f"({gap.length} ids)"
                    )
        else:
            lines.append("    gaps:                      none")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI entry point (task 11.3)
#
# Wires argument parsing, Telegram credential loading, the Telethon adapter, the
# orchestration and the report into a single runnable command returning a
# process exit code. The Telegram port is built from the same environment-based
# credentials the M0 benchmark harness uses (``TGOS3_API_ID``,
# ``TGOS3_API_HASH``, ``TGOS3_SESSION``); run_rebuild itself takes the port as
# an argument so the orchestration stays injectable and testable without
# Telethon or network access.
# ---------------------------------------------------------------------------

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2


@dataclass(frozen=True)
class TelegramCredentials:
    """Telegram API credentials sourced from the environment."""

    api_id: int
    api_hash: str
    session: str


def load_credentials(env: dict[str, str] | None = None) -> TelegramCredentials:
    """Read Telegram API credentials from the environment.

    Reads ``TGOS3_API_ID``, ``TGOS3_API_HASH`` and ``TGOS3_SESSION`` (the latter
    may be a StringSession value or a path to a session file on disk), mirroring
    the convention the M0 benchmark harness established. No secrets are
    hardcoded, and no secret values are echoed back on failure — only the names
    of any missing variables are reported.

    Raises:
        SystemExit: With code 2 if any required variable is missing or
            ``TGOS3_API_ID`` is not an integer. Each missing variable is
            reported on its own line to stderr.
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
            raise SystemExit(EXIT_USAGE) from None

    if missing:
        for name in missing:
            print(f"missing required environment variable: {name}", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)

    assert api_hash is not None  # narrowed by the missing-vars check above
    assert session is not None
    return TelegramCredentials(api_id=api_id, api_hash=api_hash, session=session)


def _build_telegram_client(creds: TelegramCredentials) -> TelegramClient:
    """Construct an (unconnected) Telethon client from credentials.

    Telethon is imported lazily so that importing this module — and running the
    orchestration against a fake :class:`TelegramPort` in tests — never requires
    Telethon to be installed, credentials to be set, or network access.
    """
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    if os.path.exists(creds.session):
        # Treat the value as a session file on disk.
        return TelegramClient(creds.session, creds.api_id, creds.api_hash)
    # Otherwise treat the value as an in-memory StringSession.
    return TelegramClient(StringSession(creds.session), creds.api_id, creds.api_hash)


async def _run_with_telethon(args: RebuildArgs) -> RebuildResult:
    """Build a connected :class:`TelethonAdapter` and run the rebuild through it.

    The adapter is connected before the scan and disconnected afterwards even if
    the rebuild raises, so the single MTProto session is always released.
    """
    from gateway.app.telegram.adapter import TelethonAdapter

    creds = load_credentials()
    client = _build_telegram_client(creds)
    adapter = TelethonAdapter(client)
    await adapter.connect()
    try:
        return await run_rebuild(args, adapter)
    finally:
        await adapter.disconnect()


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for ``cli/rebuild``: parse args, rebuild, print the report.

    Parses the command line, builds the Telegram port from environment
    credentials, runs the full recovery pass via :func:`asyncio.run`, prints the
    completion report (requirement 15.4) to stdout, and returns a process exit
    code: ``0`` on success, ``1`` if the rebuild could not complete (for example
    Telegram was unavailable or the database could not be reached). Credential
    and argument-parsing problems exit with code ``2`` from the parser /
    :func:`load_credentials`.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = parse_args(argv)
    try:
        result = asyncio.run(_run_with_telethon(args))
    except TelegramUnavailableError as exc:
        print(f"recovery failed: Telegram unavailable: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except FloodWaitError as exc:
        print(
            f"recovery failed: Telegram flood-wait "
            f"(retry after {exc.retry_after}s)",
            file=sys.stderr,
        )
        return EXIT_ERROR
    except OSError as exc:
        print(f"recovery failed: {exc}", file=sys.stderr)
        return EXIT_ERROR

    print(format_report(result))
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - exercised via the console entry
    raise SystemExit(main())

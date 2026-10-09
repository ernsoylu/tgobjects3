"""Record-cache ranking and watermark eviction (Requirements 12.2/12.3/12.4).

This module is the policy half of the on-disk record cache. The sibling
:mod:`gateway.app.cache.store` owns the files on disk and writes the
``access_stats`` rows that record every access; this module *reads* those rows,
ranks the cached records by a decayed-frequency priority, and drives a
background loop that evicts the lowest-priority records whenever the cache grows
too large.

The dependency points one way: eviction depends on the store, never the
reverse. The store exposes the three seams this module uses —
:meth:`~gateway.app.cache.store.RecordCache.delete`,
:meth:`~gateway.app.cache.store.RecordCache.total_size_bytes`, and the
``access_stats`` rows — so this module plugs in without the store importing it.

Ranking score (Requirement 12.2)
---------------------------------
Each ``access_stats`` row carries a cumulative, access-decayed score ``s_prev``
captured at ``scored_at``. At ranking time the stored score is decayed to the
present using the half-life ``H`` (``CACHE_HALF_LIFE``, seconds) and the design's
recurrence, then biased by record size with exponent ``α`` (``CACHE_SIZE_BIAS``)::

    s(t)        = s_prev × 2^(−Δt / H) + 1
    priority(t) = s(t) / size^α

where ``Δt`` is the elapsed seconds since ``scored_at``. A larger ``s(t)`` (more
recent, more frequent accesses) raises priority; a larger record lowers it, so
scarce cache space is spent on small, hot records. Records with *lower* priority
are ranked lower and evicted first.

Eviction loop (Requirements 12.3/12.4)
--------------------------------------
A background task sleeps until the cache fills to ``CACHE_HIGH_WATERMARK`` (90%
of ``CACHE_MAX_SIZE``) and then evicts the lowest-priority records one at a time
until the total on-disk size drops to ``CACHE_LOW_WATERMARK`` (80%). When two
records share the lowest priority the least-recently-accessed one (oldest
``scored_at``) is evicted first (Requirement 12.3 tie-break). Each eviction is a
:meth:`~gateway.app.cache.store.RecordCache.delete` call whose returned byte
count is subtracted from the running total, so the loop never re-walks the whole
tree per victim. A delete that reclaims nothing (the file was already gone, or a
disk error) simply drops the stale ``access_stats`` row and moves on, so the
loop always makes progress rather than spinning on an un-evictable record.

All configuration is consumed through the injected :class:`Settings` instance;
this module never reads the environment directly.
"""

from __future__ import annotations

import asyncio
import logging
import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import delete as sql_delete
from sqlalchemy import select

from gateway.app.cache.store import RecordCache
from gateway.app.config import Settings
from gateway.app.db.models import AccessStat
from gateway.app.db.session import AsyncSessionFactory

logger = logging.getLogger(__name__)

__all__ = [
    "RankedRecord",
    "decay_score",
    "priority",
    "CacheEvictor",
]


# ---------------------------------------------------------------------------
# Scoring (Requirement 12.2)
# ---------------------------------------------------------------------------


def decay_score(s_prev: float, delta_seconds: float, half_life_seconds: float) -> float:
    """Return the design's decayed-and-incremented score ``s(t)``.

    Implements ``s(t) = s_prev × 2^(−Δt / H) + 1`` (Requirement 12.2): the stored
    cumulative score ``s_prev`` is decayed toward zero by the elapsed time
    ``delta_seconds`` relative to the half-life ``half_life_seconds`` (``H``), then
    one access unit is added for the access that triggered the (re)scoring. With
    ``Δt > 0`` and ``H > 0`` the decay factor is strictly below one, so an older
    score contributes strictly less than a fresher one of equal magnitude.

    Args:
        s_prev: The cumulative score last persisted for the record.
        delta_seconds: Seconds elapsed since the score was persisted. Negative
            values (clock skew) are clamped to zero so decay never *increases* a
            score.
        half_life_seconds: The half-life ``H`` in seconds; must be positive.

    Returns:
        The decayed-plus-one score ``s(t)``.
    """
    if half_life_seconds <= 0.0:
        raise ValueError("half_life_seconds must be positive")
    delta = max(delta_seconds, 0.0)
    return s_prev * math.pow(2.0, -delta / half_life_seconds) + 1.0


def priority(score: float, size_bytes: int, size_bias: float) -> float:
    """Return the size-biased eviction priority ``s(t) / size^α``.

    Implements ``priority(t) = s(t) / size^α`` (Requirement 12.2). A larger
    ``score`` raises priority; a larger ``size_bytes`` lowers it in proportion to
    the bias exponent ``α`` (``size_bias``). Records with lower priority are
    ranked lower and evicted first.

    A zero-byte record would make ``size^α`` zero for ``α > 0`` and divide by
    zero, so its size is treated as one byte (the smallest meaningful unit); this
    keeps a zero-length record rankable and, being the smallest possible, biased
    toward retention exactly as the formula intends.

    Args:
        score: The decayed score ``s(t)`` from :func:`decay_score`.
        size_bytes: The record's on-disk size in bytes.
        size_bias: The bias exponent ``α`` (``CACHE_SIZE_BIAS``); non-negative.

    Returns:
        The eviction priority; higher means more worth keeping.
    """
    effective_size = float(size_bytes) if size_bytes > 0 else 1.0
    return score / math.pow(effective_size, size_bias)


# ---------------------------------------------------------------------------
# Ranked record view
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RankedRecord:
    """A single cached record scored for eviction, lowest-priority first.

    Produced by :meth:`CacheEvictor.rank`. Carries the record identity, its
    on-disk size, the computed :attr:`priority`, and the ``scored_at`` timestamp
    used as the tie-breaker (least recently accessed loses first).

    Attributes:
        blob_id_hex: The blob's 32-char hex id, as the store's paths and
            :meth:`~gateway.app.cache.store.RecordCache.delete` expect it.
        record_index: Zero-based record position within the blob.
        size_bytes: The record file's size in bytes at ranking time.
        priority: The size-biased decayed priority; lower is evicted first.
        scored_at: When the record's score was last persisted; the Requirement
            12.3 tie-break orders equal priorities by this, oldest first.
    """

    blob_id_hex: str
    record_index: int
    size_bytes: int
    priority: float
    scored_at: datetime


# ---------------------------------------------------------------------------
# Evictor
# ---------------------------------------------------------------------------


class CacheEvictor:
    """Ranks cached records and runs the background watermark eviction loop.

    One instance lives per gateway process alongside the :class:`RecordCache` it
    drives. :meth:`run_forever` is the long-lived background task; :meth:`stop`
    requests it to exit after the current cycle. :meth:`evict_once` performs one
    high-watermark check and eviction pass and is the unit both the loop and
    tests drive. :meth:`rank` exposes the ordering so callers (and tests) can
    inspect the policy without mutating the cache.

    Configuration — the half-life ``H``, size bias ``α``, maximum size, and the
    high/low watermark fractions — is taken from the injected :class:`Settings`
    and never from the environment.
    """

    def __init__(
        self,
        settings: Settings,
        session_factory: AsyncSessionFactory,
        cache: RecordCache,
        *,
        cache_root: Path,
        poll_interval_seconds: float = 5.0,
    ) -> None:
        """Build an evictor bound to a cache, its store, and the index.

        Args:
            settings: Supplies ``CACHE_MAX_SIZE``, ``CACHE_HALF_LIFE``,
                ``CACHE_SIZE_BIAS``, ``CACHE_HIGH_WATERMARK`` and
                ``CACHE_LOW_WATERMARK``. Consumed only through this object.
            session_factory: Opens short sessions to read ``access_stats`` and
                prune rows for records that no longer exist on disk.
            cache: The :class:`RecordCache` whose ``delete`` and
                ``total_size_bytes`` seams this evictor calls to reclaim space.
            cache_root: The on-disk cache root, matching the one the store was
                constructed with. Used to measure each record's size via the
                documented two-level layout
                ``{root}/{blob_id[:2]}/{blob_id}/{record_index}`` without
                reaching into the store's internals.
            poll_interval_seconds: How long :meth:`run_forever` sleeps between
                watermark checks when the cache is below the high watermark.
        """
        self._settings = settings
        self._session_factory = session_factory
        self._cache = cache
        self._root = cache_root
        self._poll_interval = poll_interval_seconds

        self._half_life = float(settings.CACHE_HALF_LIFE)
        self._size_bias = float(settings.CACHE_SIZE_BIAS)
        self._max_size = int(settings.CACHE_MAX_SIZE)
        self._high_bytes = int(self._max_size * settings.CACHE_HIGH_WATERMARK)
        self._low_bytes = int(self._max_size * settings.CACHE_LOW_WATERMARK)

        self._stopping = False

    # -- Paths ------------------------------------------------------------

    def _record_path(self, blob_id_hex: str, record_index: int) -> Path:
        """Return the on-disk path for a record per the documented layout.

        Mirrors the store's two-level fan-out
        (``{root}/{blob_id[:2]}/{blob_id}/{record_index}``) so the evictor can
        measure a record's size without importing or depending on the store's
        private path helper.
        """
        return self._root / blob_id_hex[:2] / blob_id_hex / str(record_index)

    def _record_size(self, blob_id_hex: str, record_index: int) -> int | None:
        """Return a record file's size in bytes, or ``None`` if it is gone.

        A missing file means the ``access_stats`` row is stale (the record was
        evicted or never written); the caller prunes such rows rather than
        ranking them.
        """
        path = self._record_path(blob_id_hex, record_index)
        try:
            return path.stat().st_size
        except FileNotFoundError:
            return None

    # -- Ranking ----------------------------------------------------------

    async def rank(self, *, now: datetime | None = None) -> list[RankedRecord]:
        """Return every cached record ranked lowest-priority first.

        Reads all ``access_stats`` rows, decays each stored score to ``now`` and
        divides by ``size^α`` to get the eviction priority (Requirement 12.2),
        and sorts ascending by priority so index 0 is the first eviction
        candidate. Equal priorities are ordered by ``scored_at`` ascending, so
        the least recently accessed record loses the tie (Requirement 12.3).

        Rows whose on-disk file no longer exists are dropped from the ranking and
        their stale rows pruned, keeping the index aligned with the files.

        Args:
            now: Timestamp to decay against; defaults to the current UTC time.
                Injectable for deterministic tests.

        Returns:
            The ranked records, lowest priority (first to evict) first.
        """
        reference = now if now is not None else datetime.now(UTC)
        rows = await self._load_access_stats()

        ranked: list[RankedRecord] = []
        stale: list[tuple[uuid.UUID, int]] = []
        for blob_id, record_index, score, scored_at in rows:
            blob_id_hex = blob_id.hex
            size = self._record_size(blob_id_hex, record_index)
            if size is None:
                stale.append((blob_id, record_index))
                continue
            delta = (reference - _as_utc(scored_at)).total_seconds()
            decayed = decay_score(score, delta, self._half_life)
            ranked.append(
                RankedRecord(
                    blob_id_hex=blob_id_hex,
                    record_index=record_index,
                    size_bytes=size,
                    priority=priority(decayed, size, self._size_bias),
                    scored_at=_as_utc(scored_at),
                )
            )

        if stale:
            await self._prune_stats(stale)

        ranked.sort(key=lambda r: (r.priority, r.scored_at))
        return ranked

    async def _load_access_stats(
        self,
    ) -> list[tuple[uuid.UUID, int, float, datetime]]:
        """Fetch every ``access_stats`` row as a plain tuple list."""
        async with self._session_factory.session() as session:
            result = await session.execute(
                select(
                    AccessStat.blob_id,
                    AccessStat.record_index,
                    AccessStat.score,
                    AccessStat.scored_at,
                )
            )
            return [
                (row[0], int(row[1]), float(row[2]), row[3])
                for row in result.all()
            ]

    async def _prune_stats(self, keys: list[tuple[uuid.UUID, int]]) -> None:
        """Delete ``access_stats`` rows for records that are no longer on disk.

        Keeps the ranking index from accumulating rows for files that were
        already evicted or never materialised. Failures here are non-fatal to
        eviction progress and are allowed to surface only as the session's own
        error for the caller's transaction policy.
        """
        async with self._session_factory.session() as session:
            for blob_id, record_index in keys:
                await session.execute(
                    sql_delete(AccessStat).where(
                        AccessStat.blob_id == blob_id,
                        AccessStat.record_index == record_index,
                    )
                )

    # -- Eviction loop (Requirements 12.3/12.4) ---------------------------

    async def evict_once(self, *, now: datetime | None = None) -> int:
        """Run one watermark check and eviction pass; return bytes reclaimed.

        If the cache is below ``CACHE_HIGH_WATERMARK`` nothing is evicted and the
        return value is zero. Otherwise records are evicted in ascending-priority
        order (least recently accessed first on ties) until the running total
        drops to ``CACHE_LOW_WATERMARK`` or the candidate list is exhausted.

        Each victim is removed with the store's ``delete`` seam; the bytes it
        returns are subtracted from the running total so the loop converges
        without re-measuring the whole tree per victim. A victim that reclaims
        nothing (file already gone, or a disk error inside ``delete``) has its
        stale index row pruned and does not stall the pass (Requirement 12.4 —
        progress is maintained and no partial record is written).

        Args:
            now: Timestamp to rank against; defaults to the current UTC time.

        Returns:
            Total bytes reclaimed in this pass.
        """
        total = await self._cache.total_size_bytes()
        if total < self._high_bytes:
            return 0

        logger.info(
            "cache high watermark reached (%d/%d bytes, high=%d); evicting to %d",
            total,
            self._max_size,
            self._high_bytes,
            self._low_bytes,
        )

        candidates = await self.rank(now=now)
        reclaimed = 0
        pruned: list[tuple[uuid.UUID, int]] = []
        for record in candidates:
            if total <= self._low_bytes:
                break
            freed = await self._cache.delete(record.blob_id_hex, record.record_index)
            total -= freed
            reclaimed += freed
            # Whether or not bytes were freed, the record should leave the index:
            # a freed file is gone; a zero-free file was already absent or failed
            # to delete, so its stale row is pruned to keep the index aligned.
            pruned.append((uuid.UUID(hex=record.blob_id_hex), record.record_index))

        if pruned:
            await self._prune_stats(pruned)

        logger.info(
            "eviction pass reclaimed %d bytes; cache now ~%d bytes", reclaimed, total
        )
        return reclaimed

    def stop(self) -> None:
        """Request :meth:`run_forever` to exit after its current cycle."""
        self._stopping = True

    async def run_forever(self) -> None:
        """Evict on each cycle, sleeping ``poll_interval`` between passes.

        Loops until :meth:`stop` is called. Each cycle runs :meth:`evict_once`;
        an unexpected error in a cycle is logged and the loop continues, so a
        transient database or disk fault never kills the background task.
        """
        while not self._stopping:
            try:
                await self.evict_once()
            except Exception:  # noqa: BLE001 - loop must survive transient faults
                logger.exception("cache eviction cycle failed; continuing")
            await asyncio.sleep(self._poll_interval)


def _as_utc(value: datetime) -> datetime:
    """Return ``value`` as a timezone-aware UTC datetime.

    ``access_stats.scored_at`` is written timezone-aware, but a naive value read
    back from some drivers is interpreted as UTC so the elapsed-time arithmetic
    in :meth:`CacheEvictor.rank` never mixes aware and naive datetimes.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)

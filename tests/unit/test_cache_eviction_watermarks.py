"""Example-based tests for the record-cache watermark eviction (Requirement 12.3).

These tests drive the real :class:`~gateway.app.cache.eviction.CacheEvictor`
against a real :class:`~gateway.app.cache.store.RecordCache` whose files live in
a temporary directory, so the filesystem seams (``total_size_bytes`` and
``delete``) exercise genuine on-disk behaviour. Only the ``access_stats`` index
is faked: a tiny in-memory session factory serves the ranking rows and absorbs
the stale-row prune, which keeps the test fast and free of a PostgreSQL
dependency while still letting the evictor's real ranking and watermark loop run.

The central example (Requirement 12.3): fill the cache to exactly the high
watermark (90% of ``CACHE_MAX_SIZE``), run one eviction pass, and assert that it
triggers and drains the cache down to the low watermark (80%).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from gateway.app.cache.eviction import CacheEvictor
from gateway.app.cache.store import RecordCache
from gateway.app.config import Settings
from gateway.app.db.session import AsyncSessionFactory

# ---------------------------------------------------------------------------
# Deterministic cache sizing
# ---------------------------------------------------------------------------

#: A small maximum cache size keeps the test fast and the watermark maths exact.
#: 1000 bytes gives round high/low watermark byte thresholds (900 / 800).
_CACHE_MAX_SIZE = 1000
#: 900 bytes — the byte threshold for CACHE_HIGH_WATERMARK = 0.90.
_HIGH_BYTES = 900
#: 800 bytes — the byte threshold for CACHE_LOW_WATERMARK = 0.80.
_LOW_BYTES = 800

#: Each cached record is exactly this many bytes. With encryption off the
#: on-disk file size equals the payload length, so N records occupy N * 100
#: bytes exactly and the watermark thresholds land on record boundaries.
_RECORD_SIZE = 100

#: A 16-char master key meets the TGOS3_MASTER_KEY minimum length.
_MASTER_KEY = "0123456789abcdef"


# ---------------------------------------------------------------------------
# In-memory access_stats fake
# ---------------------------------------------------------------------------


class _FakeResult:
    """A minimal stand-in for a SQLAlchemy ``Result`` over access-stat rows."""

    def __init__(self, rows: list[tuple[uuid.UUID, int, float, datetime]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[uuid.UUID, int, float, datetime]]:
        return self._rows


class _FakeSession:
    """Serves ``select(...)`` over the shared rows and ignores ``delete(...)``.

    The evictor only reads the four ranking columns and, for stale rows, issues
    deletes. The store (not the DB) owns the files, so the fake answers the
    select with the current rows and treats prune-deletes as no-ops; the files
    themselves are removed by the real :meth:`RecordCache.delete`.
    """

    def __init__(self, rows: list[tuple[uuid.UUID, int, float, datetime]]) -> None:
        self._rows = rows

    async def execute(self, statement: Any) -> _FakeResult:
        # A prune delete has no ``.column_descriptions``; a select does. We only
        # need to answer selects with the ranking rows and swallow deletes.
        if getattr(statement, "is_delete", False):
            return _FakeResult([])
        return _FakeResult(self._rows)


class _FakeSessionFactory:
    """Hands out a shared :class:`_FakeSession` via the ``session()`` CM."""

    def __init__(self, rows: list[tuple[uuid.UUID, int, float, datetime]]) -> None:
        self._session = _FakeSession(rows)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[_FakeSession]:
        yield self._session


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _settings() -> Settings:
    """Build a Settings instance with the deterministic cache sizing above."""
    return Settings(
        TGOS3_MASTER_KEY=_MASTER_KEY,
        DATABASE_URL="postgresql+asyncpg://user:pass@localhost/db",
        CACHE_MAX_SIZE=_CACHE_MAX_SIZE,
        CACHE_HIGH_WATERMARK=0.90,
        CACHE_LOW_WATERMARK=0.80,
        CACHE_ENCRYPT=False,
        CACHE_SIZE_BIAS=0.0,  # size-neutral: priority is driven purely by score.
    )


async def _fill_records(
    cache: RecordCache,
    blob_id: uuid.UUID,
    count: int,
) -> None:
    """Write ``count`` records of ``_RECORD_SIZE`` bytes each into the cache."""
    blob_hex = blob_id.hex
    for index in range(count):
        await cache.put(blob_hex, index, b"\x00" * _RECORD_SIZE)


def _ranking_rows(
    blob_id: uuid.UUID,
    count: int,
    *,
    base_time: datetime,
) -> list[tuple[uuid.UUID, int, float, datetime]]:
    """Build access-stat rows where lower index = lower score (evicted first).

    Record ``index`` is given score ``index + 1`` and a ``scored_at`` that grows
    with the index, so record 0 is the coldest and ranks first for eviction.
    """
    return [
        (blob_id, index, float(index + 1), base_time + timedelta(seconds=index))
        for index in range(count)
    ]


def _make_evictor(
    settings: Settings,
    cache: RecordCache,
    rows: list[tuple[uuid.UUID, int, float, datetime]],
    cache_root: Path,
) -> CacheEvictor:
    """Wire a CacheEvictor over the real cache and the in-memory stats fake."""
    factory = cast(AsyncSessionFactory, _FakeSessionFactory(rows))
    return CacheEvictor(settings, factory, cache, cache_root=cache_root)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_eviction_triggers_at_high_watermark_and_drains_to_low(
    tmp_path: Path,
) -> None:
    """Filling to exactly 90% triggers eviction that drains down to 80%.

    Validates: Requirements 12.3
    """
    settings = _settings()
    factory_rows: list[tuple[uuid.UUID, int, float, datetime]] = []
    cache = RecordCache(
        settings,
        cast(AsyncSessionFactory, _FakeSessionFactory(factory_rows)),
        cache_root=tmp_path,
    )
    blob_id = uuid.uuid4()

    # Fill to exactly the high watermark: 9 records * 100 bytes = 900 bytes.
    high_record_count = _HIGH_BYTES // _RECORD_SIZE
    await _fill_records(cache, blob_id, high_record_count)
    assert await cache.total_size_bytes() == _HIGH_BYTES

    rows = _ranking_rows(
        blob_id, high_record_count, base_time=datetime(2024, 1, 1, tzinfo=UTC)
    )
    evictor = _make_evictor(settings, cache, rows, tmp_path)

    reclaimed = await evictor.evict_once(now=datetime(2024, 1, 2, tzinfo=UTC))

    # Eviction triggered (reclaimed > 0) and drained to at or below the low mark.
    assert reclaimed > 0
    size_after = await cache.total_size_bytes()
    assert size_after <= _LOW_BYTES
    # It stopped as soon as it reached the low watermark rather than over-evicting:
    # exactly one 100-byte record removed takes 900 -> 800.
    assert size_after == _LOW_BYTES
    assert reclaimed == _RECORD_SIZE


async def test_no_eviction_below_high_watermark(tmp_path: Path) -> None:
    """Below the high watermark nothing is evicted and the cache is untouched.

    Validates: Requirements 12.3
    """
    settings = _settings()
    factory_rows: list[tuple[uuid.UUID, int, float, datetime]] = []
    cache = RecordCache(
        settings,
        cast(AsyncSessionFactory, _FakeSessionFactory(factory_rows)),
        cache_root=tmp_path,
    )
    blob_id = uuid.uuid4()

    # 8 records * 100 bytes = 800 bytes, strictly below the 900-byte high mark.
    below_count = (_HIGH_BYTES // _RECORD_SIZE) - 1
    await _fill_records(cache, blob_id, below_count)
    size_before = await cache.total_size_bytes()
    assert size_before < _HIGH_BYTES

    rows = _ranking_rows(
        blob_id, below_count, base_time=datetime(2024, 1, 1, tzinfo=UTC)
    )
    evictor = _make_evictor(settings, cache, rows, tmp_path)

    reclaimed = await evictor.evict_once(now=datetime(2024, 1, 2, tzinfo=UTC))

    assert reclaimed == 0
    assert await cache.total_size_bytes() == size_before


async def test_eviction_removes_lowest_priority_records_first(
    tmp_path: Path,
) -> None:
    """The coldest (lowest-score) records are the ones evicted (Requirement 12.3).

    Validates: Requirements 12.3
    """
    settings = _settings()
    factory_rows: list[tuple[uuid.UUID, int, float, datetime]] = []
    cache = RecordCache(
        settings,
        cast(AsyncSessionFactory, _FakeSessionFactory(factory_rows)),
        cache_root=tmp_path,
    )
    blob_id = uuid.uuid4()
    blob_hex = blob_id.hex

    high_record_count = _HIGH_BYTES // _RECORD_SIZE
    await _fill_records(cache, blob_id, high_record_count)

    # Lower index -> lower score -> evicted first. Record 0 is coldest.
    rows = _ranking_rows(
        blob_id, high_record_count, base_time=datetime(2024, 1, 1, tzinfo=UTC)
    )
    evictor = _make_evictor(settings, cache, rows, tmp_path)

    await evictor.evict_once(now=datetime(2024, 1, 2, tzinfo=UTC))

    # The single eviction (900 -> 800) must remove the coldest record, index 0.
    assert await cache.get(blob_hex, 0) is None
    for index in range(1, high_record_count):
        assert await cache.get(blob_hex, index) is not None


@pytest.mark.parametrize("extra_records", [1, 2, 3])
async def test_eviction_drains_to_low_watermark_for_various_overfills(
    tmp_path: Path, extra_records: int
) -> None:
    """From above the high mark, eviction always lands at the low watermark.

    Starting a few records over 90% and running one pass must bring the cache to
    exactly the 80% low watermark, removing only as many records as needed.

    Validates: Requirements 12.3
    """
    settings = _settings()
    factory_rows: list[tuple[uuid.UUID, int, float, datetime]] = []
    cache = RecordCache(
        settings,
        cast(AsyncSessionFactory, _FakeSessionFactory(factory_rows)),
        cache_root=tmp_path,
    )
    blob_id = uuid.uuid4()

    high_record_count = _HIGH_BYTES // _RECORD_SIZE
    total_records = high_record_count + extra_records
    await _fill_records(cache, blob_id, total_records)
    assert await cache.total_size_bytes() >= _HIGH_BYTES

    rows = _ranking_rows(
        blob_id, total_records, base_time=datetime(2024, 1, 1, tzinfo=UTC)
    )
    evictor = _make_evictor(settings, cache, rows, tmp_path)

    await evictor.evict_once(now=datetime(2024, 1, 2, tzinfo=UTC))

    # Drained down to the low watermark exactly (thresholds land on 100-byte
    # record boundaries, so there is no residual slack).
    assert await cache.total_size_bytes() == _LOW_BYTES

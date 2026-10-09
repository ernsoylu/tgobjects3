"""On-disk record cache: admission, dedup, and optional at-rest encryption.

The record cache stores decrypted record payloads on local disk so that repeated
reads of the same object do not pay Telegram download latency (Requirement 12).
Durable truth still lives in Telegram; this cache is a pure accelerator and may
be deleted at any time without data loss.

Layout
------
Each cached record is a single file under a two-level fan-out path::

    {cache_root}/{blob_id[:2]}/{blob_id}/{record_index}

``blob_id`` is the 32-character hex form of the blob UUID; the first two hex
characters form the first-level directory, which keeps any single directory from
holding more than 256 immediate subdirectories. ``record_index`` is the decimal
record position within the blob.

Responsibilities (this module)
------------------------------
* ``get(blob_id, record_index) -> bytes | None`` — return the cached plaintext
  for a record, or ``None`` on a miss (Requirement 12.1 — the cache is an
  accelerator, misses are normal).
* ``put(blob_id, record_index, data)`` — write a record to disk and record an
  access in ``access_stats`` so the ranking/eviction policy (task 19.2) can
  score it (Requirements 12.1).
* ``fetch(...)`` — a miss coordinator that deduplicates concurrent misses for the
  same ``(blob_id, record_index)`` so exactly one Telegram download occurs and
  every waiter receives the same result or the same error (Requirements 12.5,
  12.6).
* **Admission policy** — decide *whether* a freshly fetched record is worth
  storing (Requirement 12 "Admission policy"): objects ≤ 64 MiB admit every
  record on first access; larger objects admit in 8-record blocks and only on
  the *second* access of a block within one half-life, which prevents a single
  sequential scan from thrashing the whole cache.
* **Optional encryption** — when ``CACHE_ENCRYPT`` is true, each cached file is
  sealed with AES-256-GCM under a per-record key derived from the master key via
  HKDF (Requirement 12.7).

Explicitly *not* this module's job
----------------------------------
The ranking score and the background watermark eviction loop live in the sibling
module ``gateway/app/cache/eviction.py`` (task 19.2, Requirements 12.2/12.3/
12.4). This store writes the ``access_stats`` rows that eviction consumes and
exposes :meth:`RecordCache.delete` and :meth:`RecordCache.total_size_bytes` so an
eviction driver can plug in without this module importing it. The dependency
points one way: eviction depends on the store, never the reverse.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.app.config import Settings
from gateway.app.db.models import AccessStat
from gateway.app.db.session import AsyncSessionFactory

__all__ = [
    "ADMISSION_SMALL_OBJECT_LIMIT",
    "ADMISSION_BLOCK_RECORDS",
    "CACHE_KEY_INFO",
    "AdmissionPolicy",
    "CacheFetchError",
    "CacheWriteError",
    "RecordCache",
    "RecordLoader",
]

#: A loader supplies a record's plaintext from the authoritative source
#: (Telegram). :meth:`RecordCache.fetch` invokes it exactly once per
#: ``(blob_id, record_index)`` miss, deduplicating concurrent callers.
RecordLoader = Callable[[], Awaitable[bytes]]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default on-disk root for the cache. Overridable via the constructor so tests
#: and alternate deployments can relocate it.
DEFAULT_CACHE_ROOT: Final[Path] = Path("/data/cache")

#: Objects at or below this size (bytes) admit every record on first access.
#: Larger objects fall under the block-admission policy. 64 MiB per the design.
ADMISSION_SMALL_OBJECT_LIMIT: Final[int] = 64 * 1024 * 1024

#: Records per admission block for large objects. Cold blocks of a large object
#: are admitted only on the second access of the block within one half-life.
ADMISSION_BLOCK_RECORDS: Final[int] = 8

#: HKDF domain-separation info prefix for per-record cache-file keys. The record
#: identity (blob id + index) is mixed in via the HKDF ``info`` so every cached
#: file gets a distinct key and GCM nonce reuse across files is impossible.
CACHE_KEY_INFO: Final[bytes] = b"tgobjects3-v1-cache"

#: AES-256 key length in bytes for the cache-file seal.
_CACHE_KEY_LENGTH: Final[int] = 32

#: GCM nonce length in bytes for the cache-file seal.
_CACHE_NONCE_LENGTH: Final[int] = 12


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CacheWriteError(Exception):
    """A cached record could not be written to disk.

    Raised by :meth:`RecordCache.put` when the on-disk write fails (for example a
    disk I/O error). The partially written file, if any, is removed before this
    is raised so the cache never exposes a truncated record (Requirement 12.4,
    12.6 — never store a partial record).
    """


class CacheFetchError(Exception):
    """The deduplicated upstream fetch for a record failed.

    Raised to *every* waiter blocked on the same ``(blob_id, record_index)`` when
    the single in-flight fetch coordinated by :meth:`RecordCache.fetch` fails
    (Requirement 12.6). No partial record is stored in that case.
    """


# ---------------------------------------------------------------------------
# Admission policy
# ---------------------------------------------------------------------------


@dataclass
class AdmissionPolicy:
    """Decides whether a freshly fetched record should be cached.

    Implements the design's admission rules (Requirement 12 "Admission policy"):

    * **Small objects** (``object_size`` ≤ :data:`ADMISSION_SMALL_OBJECT_LIMIT`):
      every record is admitted on first access. Such objects fit comfortably and
      caching them whole maximises hit rate.
    * **Large objects** (``object_size`` > the limit): records are grouped into
      :data:`ADMISSION_BLOCK_RECORDS`-record blocks. A block is admitted only on
      its *second* access that falls within one half-life
      (:attr:`half_life_seconds`) of the first. A lone sequential pass over a
      huge object therefore caches nothing, which stops a single scan from
      evicting hotter data.

    The policy keeps only transient first-touch timestamps per large-object
    block, keyed by ``(blob_id, block_index)``. Entries older than one half-life
    are pruned lazily on access, so the bookkeeping stays bounded by the set of
    recently touched cold blocks. This object is not safe for concurrent mutation
    on its own; :class:`RecordCache` serialises calls to it under its per-key
    locks and a policy mutex.
    """

    half_life_seconds: float
    #: Maps ``(blob_id_hex, block_index)`` → monotonic time of first touch.
    _first_touch: dict[tuple[str, int], float] = field(default_factory=dict)

    def should_admit(
        self,
        blob_id_hex: str,
        record_index: int,
        object_size: int,
        *,
        now: float | None = None,
    ) -> bool:
        """Return whether the record at ``record_index`` should be cached now.

        Args:
            blob_id_hex: The blob's 32-char hex id (used only as a dict key).
            record_index: Zero-based record position within the blob.
            object_size: Total size in bytes of the object the record belongs to.
            now: Monotonic timestamp override for testing; defaults to
                :func:`time.monotonic` via the event loop clock.

        Returns:
            ``True`` to admit (cache) the record, ``False`` to skip it this time.
        """
        if object_size <= ADMISSION_SMALL_OBJECT_LIMIT:
            return True

        clock = asyncio.get_event_loop().time() if now is None else now
        self._prune(clock)

        block_index = record_index // ADMISSION_BLOCK_RECORDS
        key = (blob_id_hex, block_index)
        first = self._first_touch.get(key)
        if first is None:
            # First access of this cold block: remember it, do not admit yet.
            self._first_touch[key] = clock
            return False
        if clock - first <= self.half_life_seconds:
            # Second access within one half-life: admit the whole block now.
            del self._first_touch[key]
            return True
        # The earlier touch has aged out; treat this as a fresh first access.
        self._first_touch[key] = clock
        return False

    def _prune(self, now: float) -> None:
        """Drop first-touch records older than one half-life."""
        cutoff = now - self.half_life_seconds
        stale = [key for key, touched in self._first_touch.items() if touched < cutoff]
        for key in stale:
            del self._first_touch[key]


# ---------------------------------------------------------------------------
# Record cache
# ---------------------------------------------------------------------------


class RecordCache:
    """On-disk record cache with miss dedup and optional at-rest encryption.

    Construct one instance per gateway process. All public methods are
    coroutines; filesystem I/O is dispatched to a worker thread so the event loop
    is never blocked.

    Concurrency model
    -----------------
    A per-``(blob_id, record_index)`` :class:`asyncio.Lock` guards each record's
    file so a read never observes a half-written file and two writers never race.
    Separately, :meth:`fetch` coordinates concurrent *misses* for the same record
    with a per-key :class:`asyncio.Event`: the first caller performs the upstream
    download while later callers await the event and then read the now-populated
    cache, so exactly one download happens (Requirements 12.5, 12.6).
    """

    def __init__(
        self,
        settings: Settings,
        session_factory: AsyncSessionFactory,
        *,
        cache_root: Path | None = None,
    ) -> None:
        """Build a cache bound to ``settings`` and a database session factory.

        Args:
            settings: The gateway settings; supplies ``TGOS3_MASTER_KEY``,
                ``CACHE_ENCRYPT`` and ``CACHE_HALF_LIFE``. Configuration is only
                ever consumed through this object, never read from the
                environment directly.
            session_factory: Opens short-lived sessions for ``access_stats``
                upserts. The cache owns the transaction for each stats write.
            cache_root: On-disk root; defaults to :data:`DEFAULT_CACHE_ROOT`.
        """
        self._settings = settings
        self._session_factory = session_factory
        self._root = cache_root if cache_root is not None else DEFAULT_CACHE_ROOT
        self._encrypt = settings.CACHE_ENCRYPT
        self._master_key = settings.TGOS3_MASTER_KEY.encode("utf-8")
        self.admission = AdmissionPolicy(half_life_seconds=float(settings.CACHE_HALF_LIFE))

        # Per-record file locks and the miss-dedup events. Both are created on
        # demand and keyed by ``(blob_id_hex, record_index)``.
        self._file_locks: dict[tuple[str, int], asyncio.Lock] = {}
        self._inflight: dict[tuple[str, int], asyncio.Event] = {}
        # Guards creation/removal of entries in the two maps above.
        self._registry_lock = asyncio.Lock()

    # -- Paths ------------------------------------------------------------

    def _record_path(self, blob_id_hex: str, record_index: int) -> Path:
        """Return the on-disk path for a record (see module layout docstring)."""
        return self._root / blob_id_hex[:2] / blob_id_hex / str(record_index)

    async def _get_file_lock(self, key: tuple[str, int]) -> asyncio.Lock:
        """Return the per-record file lock, creating it on first use."""
        async with self._registry_lock:
            lock = self._file_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._file_locks[key] = lock
            return lock

    # -- Encryption -------------------------------------------------------

    def _derive_record_key(self, blob_id_hex: str, record_index: int) -> bytes:
        """Derive a 32-byte AES key for one cached record via HKDF-SHA256.

        The record identity is folded into the HKDF ``info`` so each cached file
        has a unique key. With a per-file key we can safely use a random nonce
        per write without any cross-file nonce-reuse concern (Requirement 12.7).
        """
        hkdf = HKDF(
            algorithm=hashes.SHA256(),
            length=_CACHE_KEY_LENGTH,
            salt=None,
            info=CACHE_KEY_INFO + b"|" + f"{blob_id_hex}:{record_index}".encode(),
        )
        return hkdf.derive(self._master_key)

    def _seal(self, blob_id_hex: str, record_index: int, plaintext: bytes) -> bytes:
        """Encrypt a record for at-rest storage: ``nonce(12) || ciphertext+tag``."""
        key = self._derive_record_key(blob_id_hex, record_index)
        nonce = os.urandom(_CACHE_NONCE_LENGTH)
        sealed = AESGCM(key).encrypt(nonce, plaintext, None)
        return nonce + sealed

    def _unseal(self, blob_id_hex: str, record_index: int, blob: bytes) -> bytes:
        """Decrypt a record read from disk, verifying its GCM tag first.

        Raises:
            CacheFetchError: If the file is too short or fails authentication.
                Treated as a miss by callers so a corrupt cache file degrades to
                an upstream fetch rather than surfacing bad bytes.
        """
        if len(blob) < _CACHE_NONCE_LENGTH + 16:
            raise CacheFetchError("cached record too short to be valid")
        key = self._derive_record_key(blob_id_hex, record_index)
        nonce = blob[:_CACHE_NONCE_LENGTH]
        body = blob[_CACHE_NONCE_LENGTH:]
        try:
            return AESGCM(key).decrypt(nonce, body, None)
        except InvalidTag as exc:
            raise CacheFetchError("cached record failed authentication") from exc

    # -- Core get/put -----------------------------------------------------

    async def get(self, blob_id_hex: str, record_index: int) -> bytes | None:
        """Return the cached plaintext for a record, or ``None`` on a miss.

        A missing file is a normal miss and yields ``None``. A present-but-corrupt
        encrypted file is also treated as a miss (``None``) so a damaged cache
        entry falls back to an upstream fetch instead of raising. The read is
        taken under the per-record file lock so a concurrent :meth:`put` never
        exposes a partially written file (Requirement 12.1).
        """
        key = (blob_id_hex, record_index)
        path = self._record_path(blob_id_hex, record_index)
        lock = await self._get_file_lock(key)
        async with lock:
            raw = await asyncio.to_thread(self._read_file, path)
        if raw is None:
            return None
        if not self._encrypt:
            return raw
        try:
            return self._unseal(blob_id_hex, record_index, raw)
        except CacheFetchError:
            # Corrupt cache file: degrade to a miss rather than surfacing it.
            return None

    @staticmethod
    def _read_file(path: Path) -> bytes | None:
        """Read a file fully, returning ``None`` if it does not exist."""
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None

    async def put(self, blob_id_hex: str, record_index: int, data: bytes) -> None:
        """Write a record to the cache and record an access in ``access_stats``.

        The write is atomic: bytes are written to a temporary sibling file and
        then renamed into place, so a reader never sees a half-written record and
        a failed write leaves no partial file (Requirements 12.4, 12.6). On a
        successful write an access is recorded so the ranking/eviction policy
        (task 19.2) can score the record.

        Raises:
            CacheWriteError: If the record could not be durably written. Any
                partial temporary file is removed before this is raised.
        """
        key = (blob_id_hex, record_index)
        path = self._record_path(blob_id_hex, record_index)
        payload = self._seal(blob_id_hex, record_index, data) if self._encrypt else data
        lock = await self._get_file_lock(key)
        async with lock:
            try:
                await asyncio.to_thread(self._write_file_atomic, path, payload)
            except OSError as exc:
                raise CacheWriteError(
                    f"failed to write cached record {blob_id_hex}:{record_index}: {exc}"
                ) from exc
        await self._record_access(blob_id_hex, record_index, len(payload))

    @staticmethod
    def _write_file_atomic(path: Path, payload: bytes) -> None:
        """Write ``payload`` to ``path`` atomically via a temp file + rename.

        Creates parent directories as needed. On any failure the temporary file
        is removed so no partial record is left behind.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            with open(tmp, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise

    # -- Miss dedup -------------------------------------------------------

    async def fetch(
        self,
        blob_id_hex: str,
        record_index: int,
        object_size: int,
        loader: RecordLoader,
    ) -> bytes:
        """Return a record, downloading it exactly once across concurrent misses.

        Fast path: if the record is already cached, return it. Otherwise the
        first caller for this ``(blob_id, record_index)`` becomes the *leader*:
        it invokes ``loader`` to perform the single upstream download, applies the
        admission policy, caches the bytes if admitted, and wakes any followers.
        Followers that arrive while a download is in flight wait on a shared
        :class:`asyncio.Event` and then re-read the cache, so exactly one
        download occurs (Requirement 12.5).

        If the leader's download fails, every waiter — leader and followers —
        receives :class:`CacheFetchError` and nothing partial is cached
        (Requirement 12.6).

        Args:
            blob_id_hex: The blob's 32-char hex id.
            record_index: Zero-based record position within the blob.
            object_size: Total object size in bytes, used by the admission policy.
            loader: An awaitable callable returning the record's plaintext bytes
                from the authoritative source (Telegram).

        Returns:
            The record's plaintext bytes.

        Raises:
            CacheFetchError: If the single upstream fetch failed.
        """
        cached = await self.get(blob_id_hex, record_index)
        if cached is not None:
            return cached

        key = (blob_id_hex, record_index)
        async with self._registry_lock:
            event = self._inflight.get(key)
            is_leader = event is None
            if is_leader:
                event = asyncio.Event()
                self._inflight[key] = event

        if not is_leader:
            # A follower: wait for the leader, then read what it cached. If the
            # leader admitted nothing (or failed), fall through to our own load.
            assert event is not None
            await event.wait()
            cached = await self.get(blob_id_hex, record_index)
            if cached is not None:
                return cached
            # Leader did not populate the cache (admission skip or failure).
            # Retry as a fresh fetch so this waiter still gets its bytes.
            return await self.fetch(blob_id_hex, record_index, object_size, loader)

        # Leader path: perform the single upstream download.
        assert event is not None
        try:
            data = await loader()
        except Exception as exc:
            raise CacheFetchError(
                f"upstream fetch failed for {blob_id_hex}:{record_index}: {exc}"
            ) from exc
        else:
            if self.admission.should_admit(blob_id_hex, record_index, object_size):
                # A cache write failure must not fail the read: the bytes are
                # already in hand. Swallow write errors and return the data.
                with contextlib.suppress(CacheWriteError):
                    await self.put(blob_id_hex, record_index, data)
            return data
        finally:
            async with self._registry_lock:
                self._inflight.pop(key, None)
            event.set()

    # -- access_stats seam for eviction (task 19.2) -----------------------

    async def _record_access(
        self, blob_id_hex: str, record_index: int, size_bytes: int
    ) -> None:
        """Record (or refresh) an ``access_stats`` row for a cached record.

        Writes a minimal access marker: the current score contribution and the
        access timestamp. The authoritative decayed-frequency scoring and
        eviction ordering live in task 19.2; this method only ensures a row
        exists and its ``scored_at`` advances on each access, which is the signal
        that policy consumes. The upsert bumps the stored ``score`` by one access
        unit so repeated accesses are reflected even before the decay pass runs.

        ``blob_id_hex`` is converted back to a UUID for the foreign key. A failure
        here is non-fatal to the cache write (the file is already on disk); it is
        allowed to propagate only as a database error for the caller's session
        policy to handle.
        """
        blob_uuid = uuid.UUID(hex=blob_id_hex)
        # ``access_stats.scored_at`` is TIMESTAMP WITHOUT TIME ZONE, so write a
        # naive UTC timestamp: asyncpg rejects a timezone-aware value for that
        # column ("can't subtract offset-naive and offset-aware datetimes"). The
        # upload worker and the write path use naive UTC for the same reason.
        now = datetime.now(UTC).replace(tzinfo=None)
        async with self._session_factory.session() as session:
            await self._upsert_access_stat(session, blob_uuid, record_index, now)

    @staticmethod
    async def _upsert_access_stat(
        session: AsyncSession,
        blob_id: uuid.UUID,
        record_index: int,
        scored_at: datetime,
    ) -> None:
        """Insert or update the ``access_stats`` row for one record.

        On conflict the row's ``score`` is incremented by one and ``scored_at`` is
        advanced to the access time, giving the eviction policy a fresh frequency
        signal. The full decay maths is applied by task 19.2.
        """
        stmt = pg_insert(AccessStat).values(
            blob_id=blob_id,
            record_index=record_index,
            score=1.0,
            scored_at=scored_at,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[AccessStat.blob_id, AccessStat.record_index],
            set_={
                "score": AccessStat.score + 1.0,
                "scored_at": scored_at,
            },
        )
        await session.execute(stmt)

    async def delete(self, blob_id_hex: str, record_index: int) -> int:
        """Remove a cached record file; return the bytes reclaimed (0 if absent).

        Provided as the eviction seam (task 19.2): an eviction driver selects the
        lowest-ranked records from ``access_stats`` and calls this to free disk,
        without this module importing the eviction logic.
        """
        key = (blob_id_hex, record_index)
        path = self._record_path(blob_id_hex, record_index)
        lock = await self._get_file_lock(key)
        async with lock:
            return await asyncio.to_thread(self._unlink_file, path)

    @staticmethod
    def _unlink_file(path: Path) -> int:
        """Delete a file, returning its size in bytes (0 if it did not exist)."""
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return 0
        path.unlink(missing_ok=True)
        return size

    async def total_size_bytes(self) -> int:
        """Return the total on-disk size of all cached record files in bytes.

        Walks the cache tree once. Exposed so the eviction policy (task 19.2) can
        compare against ``CACHE_MAX_SIZE`` and the high/low watermarks without
        reaching into this module's path layout.
        """
        return await asyncio.to_thread(self._tree_size, self._root)

    @staticmethod
    def _tree_size(root: Path) -> int:
        """Sum the sizes of every regular file under ``root`` (0 if absent)."""
        total = 0
        if not root.exists():
            return 0
        for current, _dirs, files in os.walk(root):
            for name in files:
                try:
                    total += (Path(current) / name).stat().st_size
                except FileNotFoundError:
                    continue
        return total

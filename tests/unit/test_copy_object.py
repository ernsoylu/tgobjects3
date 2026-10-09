"""Unit tests for :mod:`gateway.app.s3.operations.copy_object`.

Covers the CopyObject contract (Requirement 11):

* 11.2 — a source object whose SHA-256 matches a COMMITTED blob in the
  destination bucket records the destination generation against that blob and
  writes **no** data to Telegram (only the generation's Commit Record).
* 11.3 — a source key that does not exist (unknown source bucket or no live
  head) returns HTTP 404 and creates no destination object.
* 11.4 — a successful copy returns a ``CopyObjectResult`` body carrying the
  destination object's ETag and the new generation's last-modified timestamp.
* 11.5 — an unknown destination bucket returns HTTP 404 and creates nothing.
* 11.6 — identical source and destination (same bucket, same key) is a no-op
  returning the existing ETag and last-modified without any Telegram write.

The handler performs index reads plus a Commit-Record send and three storage
mutations, so the database is replaced with an in-memory fake session factory
that routes the handler's ``select`` statements, the Telegram boundary with a
recording / failing fake port, and the head/refcount storage helpers with
monkeypatched spies.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from xml.etree import ElementTree as ET

import pytest
from gateway.app.db.models import Blob, Bucket, Generation
from gateway.app.format import commit_record as commit_record_codec
from gateway.app.s3 import router, xml_codecs
from gateway.app.s3.operations import copy_object
from gateway.app.s3.operations.copy_object import (
    CopyObjectHandler,
    serialise_copy_object_result,
)
from gateway.app.storage import generations as generations_storage
from gateway.app.telegram.errors import TelegramUnavailableError

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _State:
    """The fake index state the handler reads from.

    Attributes:
        buckets: name -> Bucket row.
        generations: generation id -> Generation row.
        blobs: blob id -> Blob row.
        committed_counts: (bucket_id, key) -> COMMITTED generation count.
    """

    buckets: dict[str, Bucket] = field(default_factory=dict)
    generations: dict[uuid.UUID, Generation] = field(default_factory=dict)
    blobs: dict[uuid.UUID, Blob] = field(default_factory=dict)
    committed_counts: dict[tuple[uuid.UUID, str], int] = field(default_factory=dict)


class _ScalarResult:
    """A tiny ``Result`` stand-in exposing ``scalar_one`` / ``scalar_one_or_none``."""

    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value

    def scalar_one(self) -> Any:
        return self._value


class _FakeSession:
    """Routes the handler's ``select`` statements against the fake state.

    The handler issues four read shapes:

    * ``select(Bucket).where(Bucket.name == …)`` — bucket lookup by name.
    * ``select(Generation).where(Generation.id == …)`` — generation by id.
    * ``select(Blob).where(Blob.id == …)`` — blob by id.
    * ``select(Blob).where(bucket_id, sha256, state)`` — dedup lookup.
    * ``select(func.count())`` over generations — generation number.

    They are told apart by the selected entity and the bound parameters.
    """

    def __init__(self, state: _State) -> None:
        self._state = state
        self.added: list[Any] = []
        self.in_transaction = False

    async def execute(self, statement: Any) -> Any:
        description = str(statement)
        params = statement.compile().params

        if "count(*)" in description:
            # _generation_number: resolve by the (bucket_id, key) bound params.
            bucket_id = _param(params, "bucket_id_1")
            key = _param(params, "key_1")
            count = self._state.committed_counts.get((bucket_id, key), 0)
            return _ScalarResult(count)

        if "FROM buckets" in description:
            name = _param(params, "name_1")
            return _ScalarResult(self._state.buckets.get(name))

        if "FROM generations" in description:
            gen_id = _param(params, "id_1")
            return _ScalarResult(self._state.generations.get(gen_id))

        if "FROM blobs" in description:
            # The dedup lookup binds a sha256 parameter; the by-id lookup binds
            # only id. Route on the bound parameters, not the column list (which
            # always mentions sha256).
            if any(k.startswith("sha256") for k in params):
                bucket_id = _param(params, "bucket_id_1")
                sha256 = _param(params, "sha256_1")
                for blob in self._state.blobs.values():
                    if (
                        blob.bucket_id == bucket_id
                        and blob.sha256 == sha256
                        and blob.state == "COMMITTED"
                    ):
                        return _ScalarResult(blob)
                return _ScalarResult(None)
            blob_id = _param(params, "id_1")
            return _ScalarResult(self._state.blobs.get(blob_id))

        raise AssertionError(f"unexpected statement: {description}")

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None

    def begin(self) -> Any:
        session = self

        @asynccontextmanager
        async def _ctx() -> Any:
            session.in_transaction = True
            yield session

        return _ctx()


def _param(params: dict[str, Any], name: str) -> Any:
    """Return a bound parameter by name, tolerating SQLAlchemy's numbering."""
    if name in params:
        return params[name]
    # Fall back to the first value whose key starts with the stripped suffix.
    base = name.rsplit("_", 1)[0]
    for key, value in params.items():
        if key.startswith(base):
            return value
    raise KeyError(name)


class _FakeSessionFactory:
    """Session factory exposing the ``.session()`` async context manager."""

    def __init__(self, state: _State) -> None:
        self._state = state
        self.sessions: list[_FakeSession] = []

    @asynccontextmanager
    async def session(self) -> Any:
        session = _FakeSession(self._state)
        self.sessions.append(session)
        yield session


@dataclass
class _SentDocument:
    """One recorded ``send_document`` call."""

    channel_id: int
    data: bytes
    file_name: str
    caption: str


class _RecordingTelegram:
    """Fake TelegramPort recording ``send_document`` calls and succeeding."""

    def __init__(self) -> None:
        self.sent: list[_SentDocument] = []

    async def send_document(
        self, channel_id: int, data: bytes, file_name: str, caption: str
    ) -> int:
        self.sent.append(_SentDocument(channel_id, data, file_name, caption))
        return 9090


class _FailingTelegram:
    """Fake TelegramPort whose ``send_document`` always raises."""

    def __init__(self) -> None:
        self.calls = 0

    async def send_document(
        self, channel_id: int, data: bytes, file_name: str, caption: str
    ) -> int:
        self.calls += 1
        raise TelegramUnavailableError("channel write failed")


# ---------------------------------------------------------------------------
# Builders / helpers
# ---------------------------------------------------------------------------


def _bucket(name: str, channel_id: int) -> Bucket:
    return Bucket(
        id=uuid.uuid4(),
        name=name,
        channel_id=channel_id,
        kek_wrapped=b"kek",
        kek_version=1,
        durability="sync",
        created_by_us=True,
        status="active",
    )


def _blob(bucket_id: uuid.UUID, sha256: bytes, *, size: int = 10) -> Blob:
    return Blob(
        id=uuid.uuid4(),
        bucket_id=bucket_id,
        sha256=sha256,
        size=size,
        key_wrapped=b"wrapped",
        chunk_records=512,
        state="COMMITTED",
        refcount=1,
        manifest_message_id=4242,
    )


def _generation(
    bucket_id: uuid.UUID, key: str, blob_id: uuid.UUID, *, size: int = 10
) -> Generation:
    return Generation(
        id=uuid.uuid4(),
        bucket_id=bucket_id,
        key=key,
        kind="put",
        blob_id=blob_id,
        size=size,
        etag='"abc123"',
        checksums={"sha256": "aa"},
        content_type="text/plain",
        user_meta={"x": "y"},
        last_modified=datetime(2024, 1, 2, 3, 4, 5, 678000, tzinfo=UTC),
        state=generations_storage.STATE_COMMITTED,
        commit_message_id=1,
        staging_path=None,
    )


def _context(
    bucket: str | None, key: str | None, copy_source: str | None
) -> router.S3HandlerContext:
    """Build a handler context with a header-bearing fake request."""

    class _Headers:
        def __init__(self, copy_source: str | None) -> None:
            self._map = (
                {"x-amz-copy-source": copy_source} if copy_source is not None else {}
            )

        def get(self, name: str) -> str | None:
            return self._map.get(name.lower())

    class _Request:
        def __init__(self, copy_source: str | None) -> None:
            self.headers = _Headers(copy_source)

    return router.S3HandlerContext(
        request=_Request(copy_source),  # type: ignore[arg-type]
        operation=router.S3Operation.PUT_OBJECT,
        target=router.ResolvedTarget(bucket=bucket, key=key),
        auth=object(),  # type: ignore[arg-type]
        settings=object(),  # type: ignore[arg-type]
    )


@dataclass
class _StorageSpy:
    """Captures the head advance and refcount mutations the handler performs."""

    advanced_heads: list[tuple[uuid.UUID, str, uuid.UUID]] = field(default_factory=list)
    incremented: list[uuid.UUID] = field(default_factory=list)


def _wire(
    state: _State,
    telegram: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    head: uuid.UUID | None,
) -> tuple[CopyObjectHandler, _FakeSessionFactory, _StorageSpy]:
    """Build a handler over the fakes, stubbing head/refcount storage."""
    factory = _FakeSessionFactory(state)
    spy = _StorageSpy()

    async def _fake_get_head(
        _session: Any, _bucket_id: uuid.UUID, _key: str
    ) -> uuid.UUID | None:
        return head

    async def _fake_advance_head(
        _session: Any, bucket_id: uuid.UUID, key: str, generation_id: uuid.UUID
    ) -> None:
        spy.advanced_heads.append((bucket_id, key, generation_id))

    async def _fake_increment_refcount(
        _session: Any, blob_id: uuid.UUID
    ) -> int:
        spy.incremented.append(blob_id)
        return 2

    monkeypatch.setattr(copy_object.heads, "get_head", _fake_get_head)
    monkeypatch.setattr(copy_object.heads, "advance_head", _fake_advance_head)
    monkeypatch.setattr(
        copy_object.blobs_storage, "increment_refcount", _fake_increment_refcount
    )

    handler = CopyObjectHandler(factory, telegram)  # type: ignore[arg-type]
    return handler, factory, spy


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


# ---------------------------------------------------------------------------
# Requirement 11.2 / 11.4 — dedup copy records a generation, no data upload
# ---------------------------------------------------------------------------


async def test_dedup_copy_records_generation_without_data_upload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A within-bucket copy reuses the blob and writes only a Commit Record."""
    bucket = _bucket("b", 500)
    sha = b"\x11" * 32
    src_blob = _blob(bucket.id, sha)
    src_gen = _generation(bucket.id, "src", src_blob.id)

    state = _State(
        buckets={"b": bucket},
        generations={src_gen.id: src_gen},
        blobs={src_blob.id: src_blob},
        committed_counts={(bucket.id, "dst"): 0},
    )
    telegram = _RecordingTelegram()
    handler, factory, spy = _wire(state, telegram, monkeypatch, head=src_gen.id)

    response = await handler(_context("b", "dst", "/b/src"))

    assert response.status_code == 200

    # Exactly one Telegram write — the Commit Record — and it is NOT a data or
    # manifest message (Requirement 11.2: no data uploaded).
    assert len(telegram.sent) == 1
    sent = telegram.sent[0]
    assert sent.channel_id == 500
    assert sent.caption.startswith("tgo3-c-")
    record = commit_record_codec.deserialise(sent.data)
    assert record.key == "dst"
    assert record.bucket_id == bucket.id
    assert record.blob_manifest_message_id == src_blob.manifest_message_id

    # Index mutations: a COMMITTED copy generation was inserted, the reused
    # blob's refcount bumped, and the destination head advanced to the new gen.
    added_generations = [
        obj
        for session in factory.sessions
        for obj in session.added
        if isinstance(obj, Generation)
    ]
    assert len(added_generations) == 1
    new_gen = added_generations[0]
    assert new_gen.kind == "copy"
    assert new_gen.state == generations_storage.STATE_COMMITTED
    assert new_gen.blob_id == src_blob.id
    assert new_gen.staging_path is None

    assert spy.incremented == [src_blob.id]
    assert spy.advanced_heads == [(bucket.id, "dst", new_gen.id)]


async def test_dedup_copy_result_carries_etag_and_last_modified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CopyObjectResult body carries the ETag and last-modified (11.4)."""
    bucket = _bucket("b", 1)
    sha = b"\x22" * 32
    src_blob = _blob(bucket.id, sha)
    src_gen = _generation(bucket.id, "src", src_blob.id)

    state = _State(
        buckets={"b": bucket},
        generations={src_gen.id: src_gen},
        blobs={src_blob.id: src_blob},
    )
    telegram = _RecordingTelegram()
    handler, _factory, _spy = _wire(state, telegram, monkeypatch, head=src_gen.id)

    response = await handler(_context("b", "dst", "/b/src"))

    assert response.status_code == 200
    root = ET.fromstring(response.body)
    assert _localname(root.tag) == "CopyObjectResult"
    children = {_localname(c.tag): c.text for c in root}
    assert children["ETag"] == '"abc123"'
    # LastModified is a millisecond-ISO timestamp ending in Z.
    assert children["LastModified"] is not None
    assert children["LastModified"].endswith("Z")


# ---------------------------------------------------------------------------
# Requirement 11.6 — same bucket + same key is a no-op
# ---------------------------------------------------------------------------


async def test_same_bucket_same_key_is_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Copying a key onto itself returns the existing ETag with no Telegram write."""
    bucket = _bucket("b", 1)
    sha = b"\x33" * 32
    src_blob = _blob(bucket.id, sha)
    src_gen = _generation(bucket.id, "same", src_blob.id)

    state = _State(
        buckets={"b": bucket},
        generations={src_gen.id: src_gen},
        blobs={src_blob.id: src_blob},
    )
    telegram = _RecordingTelegram()
    handler, factory, spy = _wire(state, telegram, monkeypatch, head=src_gen.id)

    response = await handler(_context("b", "same", "/b/same"))

    assert response.status_code == 200
    assert telegram.sent == []  # no Telegram write for a no-op
    assert spy.incremented == []
    assert spy.advanced_heads == []
    # No generation was inserted.
    added = [obj for s in factory.sessions for obj in s.added]
    assert added == []

    root = ET.fromstring(response.body)
    children = {_localname(c.tag): c.text for c in root}
    assert children["ETag"] == '"abc123"'


# ---------------------------------------------------------------------------
# Requirement 11.3 — missing source key
# ---------------------------------------------------------------------------


async def test_missing_source_key_returns_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A source key with no live head returns 404 and creates nothing (11.3)."""
    bucket = _bucket("b", 1)
    state = _State(buckets={"b": bucket})
    telegram = _RecordingTelegram()
    handler, factory, spy = _wire(state, telegram, monkeypatch, head=None)

    response = await handler(_context("b", "dst", "/b/missing"))

    assert response.status_code == 404
    assert telegram.sent == []
    assert spy.advanced_heads == []
    added = [obj for s in factory.sessions for obj in s.added]
    assert added == []


async def test_unknown_source_bucket_returns_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown source bucket means no source key: 404 (11.3)."""
    bucket = _bucket("dst-bucket", 1)
    state = _State(buckets={"dst-bucket": bucket})
    telegram = _RecordingTelegram()
    handler, _factory, _spy = _wire(state, telegram, monkeypatch, head=None)

    response = await handler(_context("dst-bucket", "k", "/no-such-src/key"))

    assert response.status_code == 404
    assert telegram.sent == []


# ---------------------------------------------------------------------------
# Requirement 11.5 — missing destination bucket
# ---------------------------------------------------------------------------


async def test_missing_destination_bucket_returns_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown destination bucket returns 404 and creates nothing (11.5)."""
    state = _State(buckets={})
    telegram = _RecordingTelegram()
    handler, factory, spy = _wire(state, telegram, monkeypatch, head=uuid.uuid4())

    response = await handler(_context("missing-dst", "k", "/src/key"))

    assert response.status_code == 404
    assert telegram.sent == []
    assert spy.advanced_heads == []
    added = [obj for s in factory.sessions for obj in s.added]
    assert added == []


# ---------------------------------------------------------------------------
# Cross-bucket, content absent in destination → 501 (out of scope)
# ---------------------------------------------------------------------------


async def test_cross_bucket_without_existing_blob_returns_501(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A copy whose content is absent from the destination bucket is 501."""
    src_bucket = _bucket("src", 1)
    dst_bucket = _bucket("dst", 2)
    sha = b"\x44" * 32
    src_blob = _blob(src_bucket.id, sha)
    src_gen = _generation(src_bucket.id, "k", src_blob.id)

    state = _State(
        buckets={"src": src_bucket, "dst": dst_bucket},
        generations={src_gen.id: src_gen},
        blobs={src_blob.id: src_blob},  # only in the source bucket
    )
    telegram = _RecordingTelegram()
    handler, _factory, spy = _wire(state, telegram, monkeypatch, head=src_gen.id)

    response = await handler(_context("dst", "k", "/src/k"))

    assert response.status_code == 501
    assert telegram.sent == []
    assert spy.advanced_heads == []


# ---------------------------------------------------------------------------
# Malformed / missing copy-source header
# ---------------------------------------------------------------------------


async def test_missing_copy_source_header_returns_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without an x-amz-copy-source header the request is a 400."""
    state = _State(buckets={"b": _bucket("b", 1)})
    telegram = _RecordingTelegram()
    handler, _factory, _spy = _wire(state, telegram, monkeypatch, head=None)

    response = await handler(_context("b", "dst", None))

    assert response.status_code == 400
    assert telegram.sent == []


async def test_malformed_copy_source_header_returns_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A copy-source header without a key part is a 400."""
    state = _State(buckets={"b": _bucket("b", 1)})
    telegram = _RecordingTelegram()
    handler, _factory, _spy = _wire(state, telegram, monkeypatch, head=None)

    response = await handler(_context("b", "dst", "/just-a-bucket"))

    assert response.status_code == 400
    assert telegram.sent == []


# ---------------------------------------------------------------------------
# Telegram failure on the commit-record write
# ---------------------------------------------------------------------------


async def test_telegram_failure_returns_503_and_creates_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Commit-Record write failure yields 503 and no index mutation."""
    bucket = _bucket("b", 7)
    sha = b"\x55" * 32
    src_blob = _blob(bucket.id, sha)
    src_gen = _generation(bucket.id, "src", src_blob.id)

    state = _State(
        buckets={"b": bucket},
        generations={src_gen.id: src_gen},
        blobs={src_blob.id: src_blob},
    )
    telegram = _FailingTelegram()
    handler, factory, spy = _wire(state, telegram, monkeypatch, head=src_gen.id)

    response = await handler(_context("b", "dst", "/b/src"))

    assert response.status_code == 503
    assert telegram.calls == 1
    assert spy.advanced_heads == []
    assert spy.incremented == []
    added = [obj for s in factory.sessions for obj in s.added]
    assert added == []


# ---------------------------------------------------------------------------
# CopyObjectResult serialiser (direct)
# ---------------------------------------------------------------------------


def test_serialise_copy_object_result_shape() -> None:
    """The serialiser emits a namespaced CopyObjectResult with a quoted ETag."""
    when = datetime(2023, 6, 1, 12, 0, 0, 123000, tzinfo=UTC)
    body = serialise_copy_object_result("etag-no-quotes", when)

    root = ET.fromstring(body)
    assert root.tag == f"{{{xml_codecs.S3_NAMESPACE}}}CopyObjectResult"
    children = {_localname(c.tag): c.text for c in root}
    assert children["ETag"] == '"etag-no-quotes"'
    assert children["LastModified"] == "2023-06-01T12:00:00.123Z"

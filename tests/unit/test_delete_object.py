"""Unit tests for :mod:`gateway.app.s3.operations.delete_object`.

Covers the DeleteObject contract (Requirements 9.3, 9.4, 9.5):

* 9.3 — deleting a key that exists writes a Tombstone to the bucket's Telegram
  channel, marks the referenced generation DELETED, clears the key's head, and
  returns HTTP 204 with no body.
* 9.4 — if the Telegram write fails, the handler returns HTTP 500 with no body
  and leaves the index unchanged (no DELETED flip, no head clear).
* 9.5 — deleting a key that does not exist (unknown bucket or no live head)
  returns HTTP 204 with no body and writes nothing to Telegram.

A central ordering guarantee is exercised directly: the Tombstone must be sent
to Telegram *before* the index is mutated, and the DELETED flip must never
happen when the Telegram write failed.

The handler performs only index reads plus two storage mutations, so the
database is replaced with a small in-memory fake session factory and the
Telegram boundary with a recording / failing fake port.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import pytest
from gateway.app.format import tombstone as tombstone_codec
from gateway.app.s3 import router
from gateway.app.s3.operations import delete_object
from gateway.app.s3.operations.delete_object import DeleteObjectHandler
from gateway.app.telegram.errors import TelegramUnavailableError

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeSession:
    """Minimal AsyncSession stand-in serving the bucket-id/channel lookup.

    The only ``execute`` the handler issues against a session is
    ``select(Bucket.id, Bucket.channel_id).where(Bucket.name == ...)``; it is
    served from a name -> (id, channel_id) map. Everything else the handler does
    (head read, generation number, mutations) is stubbed at the storage module.
    """

    def __init__(self, buckets: dict[str, tuple[uuid.UUID, int]]) -> None:
        self._buckets = buckets

    async def execute(self, statement: Any) -> Any:
        params = statement.compile().params
        name = next(iter(params.values()))
        row = self._buckets.get(name)

        class _Result:
            def __init__(self, value: tuple[uuid.UUID, int] | None) -> None:
                self._value = value

            def first(self) -> tuple[uuid.UUID, int] | None:
                return self._value

        return _Result(row)


class _FakeSessionFactory:
    """Session factory exposing the ``.session()`` async context manager.

    Records how many sessions were opened so a test can assert the index-mutating
    second session is never entered on the Telegram-failure path.
    """

    def __init__(self, buckets: dict[str, tuple[uuid.UUID, int]]) -> None:
        self._buckets = buckets
        self.sessions_opened = 0

    @asynccontextmanager
    async def session(self) -> Any:
        self.sessions_opened += 1
        yield _FakeSession(self._buckets)


@dataclass
class _SentDocument:
    """One recorded ``send_document`` call."""

    channel_id: int
    data: bytes
    file_name: str
    caption: str


class _RecordingTelegram:
    """Fake TelegramPort that records ``send_document`` calls and succeeds."""

    def __init__(self) -> None:
        self.sent: list[_SentDocument] = []

    async def send_document(
        self, channel_id: int, data: bytes, file_name: str, caption: str
    ) -> int:
        self.sent.append(_SentDocument(channel_id, data, file_name, caption))
        return 7777


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
# Helpers
# ---------------------------------------------------------------------------


def _context(bucket: str | None, key: str | None) -> router.S3HandlerContext:
    """Build a handler context with the given resolved target.

    ``request``, ``auth`` and ``settings`` are never read by DeleteObject, so
    they are supplied as throwaway sentinels.
    """
    return router.S3HandlerContext(
        request=object(),  # type: ignore[arg-type]
        operation=router.S3Operation.DELETE_OBJECT,
        target=router.ResolvedTarget(bucket=bucket, key=key),
        auth=object(),  # type: ignore[arg-type]
        settings=object(),  # type: ignore[arg-type]
    )


@dataclass
class _IndexSpy:
    """Captures the index mutations the handler performs."""

    marked_deleted: list[uuid.UUID]
    cleared_heads: list[tuple[uuid.UUID, str]]


def _wire(
    *,
    buckets: dict[str, tuple[uuid.UUID, int]],
    head: uuid.UUID | None,
    generation_number: int,
    telegram: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[DeleteObjectHandler, _FakeSessionFactory, _IndexSpy]:
    """Build a handler over the fakes, stubbing head/number/mutation storage."""
    factory = _FakeSessionFactory(buckets)
    spy = _IndexSpy(marked_deleted=[], cleared_heads=[])

    async def _fake_get_head(
        _session: Any, _bucket_id: uuid.UUID, _key: str
    ) -> uuid.UUID | None:
        return head

    async def _fake_generation_number(
        _session: Any, _bucket_id: uuid.UUID, _key: str
    ) -> int:
        return generation_number

    async def _fake_mark_deleted(_session: Any, generation_id: uuid.UUID) -> None:
        spy.marked_deleted.append(generation_id)

    async def _fake_clear_head(
        _session: Any, bucket_id: uuid.UUID, key: str
    ) -> None:
        spy.cleared_heads.append((bucket_id, key))

    monkeypatch.setattr(delete_object.heads, "get_head", _fake_get_head)
    monkeypatch.setattr(delete_object.heads, "clear_head", _fake_clear_head)
    monkeypatch.setattr(
        delete_object.generations, "mark_deleted", _fake_mark_deleted
    )
    monkeypatch.setattr(
        DeleteObjectHandler, "_generation_number", staticmethod(_fake_generation_number)
    )

    handler = DeleteObjectHandler(factory, telegram)  # type: ignore[arg-type]
    return handler, factory, spy


# ---------------------------------------------------------------------------
# Requirement 9.3 — existing key
# ---------------------------------------------------------------------------


async def test_delete_existing_key_writes_tombstone_and_returns_204(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An existing key: Tombstone written, generation DELETED, head cleared, 204."""
    bucket_id = uuid.uuid4()
    head_gen = uuid.uuid4()
    telegram = _RecordingTelegram()
    handler, _factory, spy = _wire(
        buckets={"b": (bucket_id, 555)},
        head=head_gen,
        generation_number=3,
        telegram=telegram,
        monkeypatch=monkeypatch,
    )

    response = await handler(_context("b", "k"))

    assert response.status_code == 204
    assert response.body == b""

    # Tombstone written to the bucket's channel with the right caption.
    assert len(telegram.sent) == 1
    sent = telegram.sent[0]
    assert sent.channel_id == 555
    assert sent.caption == f"tgo3-t-{head_gen.hex}"

    # The payload is a valid Tombstone referencing the head generation.
    tomb = tombstone_codec.deserialise(sent.data)
    assert tomb.generation_id == head_gen
    assert tomb.bucket_id == bucket_id
    assert tomb.key == "k"
    assert tomb.generation_number == 3

    # Index caught up: generation DELETED and head cleared.
    assert spy.marked_deleted == [head_gen]
    assert spy.cleared_heads == [(bucket_id, "k")]


# ---------------------------------------------------------------------------
# Requirement 9.4 — Telegram failure leaves index unchanged
# ---------------------------------------------------------------------------


async def test_delete_returns_500_and_leaves_index_unchanged_on_telegram_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Telegram write failure yields 500, no DELETED flip, no head clear (9.4)."""
    bucket_id = uuid.uuid4()
    head_gen = uuid.uuid4()
    telegram = _FailingTelegram()
    handler, factory, spy = _wire(
        buckets={"b": (bucket_id, 42)},
        head=head_gen,
        generation_number=0,
        telegram=telegram,
        monkeypatch=monkeypatch,
    )

    response = await handler(_context("b", "k"))

    assert response.status_code == 500
    assert response.body == b""

    # Telegram was attempted exactly once.
    assert telegram.calls == 1

    # Index is untouched: no mutation ran and the mutating session was never
    # opened (only the initial read session).
    assert spy.marked_deleted == []
    assert spy.cleared_heads == []
    assert factory.sessions_opened == 1


# ---------------------------------------------------------------------------
# Requirement 9.5 — non-existent key is a silent 204
# ---------------------------------------------------------------------------


async def test_delete_unknown_bucket_returns_204_without_telegram(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deleting under an unknown bucket is a silent 204 with no Telegram write."""
    telegram = _RecordingTelegram()
    handler, _factory, spy = _wire(
        buckets={},
        head=None,
        generation_number=0,
        telegram=telegram,
        monkeypatch=monkeypatch,
    )

    response = await handler(_context("missing", "k"))

    assert response.status_code == 204
    assert response.body == b""
    assert telegram.sent == []
    assert spy.marked_deleted == []
    assert spy.cleared_heads == []


async def test_delete_key_without_head_returns_204_without_telegram(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deleting a key with no live head is a silent, idempotent 204 (9.5)."""
    telegram = _RecordingTelegram()
    handler, _factory, spy = _wire(
        buckets={"b": (uuid.uuid4(), 1)},
        head=None,
        generation_number=0,
        telegram=telegram,
        monkeypatch=monkeypatch,
    )

    response = await handler(_context("b", "k"))

    assert response.status_code == 204
    assert response.body == b""
    assert telegram.sent == []
    assert spy.marked_deleted == []
    assert spy.cleared_heads == []


async def test_delete_missing_key_in_target_returns_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A target with no key is a malformed request (defensive 400)."""
    telegram = _RecordingTelegram()
    handler, _factory, _spy = _wire(
        buckets={"b": (uuid.uuid4(), 1)},
        head=None,
        generation_number=0,
        telegram=telegram,
        monkeypatch=monkeypatch,
    )

    response = await handler(_context("b", None))

    assert response.status_code == 400
    assert telegram.sent == []

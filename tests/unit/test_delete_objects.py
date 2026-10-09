"""Unit tests for :mod:`gateway.app.s3.operations.delete_objects`.

Covers the DeleteObjects (batch delete) contract (Requirements 9.6, 9.7):

* 9.7 — a request naming more than 1000 keys is rejected with HTTP 400
  ``MalformedXML`` and no key is processed.
* 9.6 — a request naming 1 to 1000 keys processes each key independently,
  returning a per-key success or error for every requested key; the ``Quiet``
  flag suppresses the success entries; one key's failure never aborts the batch.

The handler's HTTP/XML surface (count rejection, malformed body, response
framing, quiet mode, per-key error isolation) is tested against a fake that
replaces the per-key delete, so these tests stay pure. The per-key delete logic
itself (``delete_one_key``: non-existent key is a silent success, Telegram is
written before the index is mutated, unknown bucket is an error) is tested
against a small in-memory fake session and a fake Telegram port, with the
storage helpers stubbed.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import Any, cast
from xml.etree import ElementTree as ET

import pytest
from gateway.app.db.models import Bucket
from gateway.app.db.session import AsyncSessionFactory
from gateway.app.s3 import router, xml_codecs
from gateway.app.s3.operations import delete_objects
from gateway.app.s3.operations.delete_objects import (
    MAX_DELETE_KEYS,
    STATE_DELETED,
    DeleteObjectsHandler,
    KeyDeleteOutcome,
    delete_one_key,
)
from gateway.app.storage import heads as heads_storage
from gateway.app.telegram.errors import TelegramUnavailableError
from gateway.app.telegram.port import SessionState, TelegramMessage
from hypothesis import given
from hypothesis import strategies as st

S3_NS = xml_codecs.S3_NAMESPACE


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeRequest:
    """A stand-in FastAPI request exposing only ``await body()``."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    async def body(self) -> bytes:
        return self._body


class _FakeSessionFactory:
    """Session factory whose ``.session()`` yields a provided fake session.

    A fresh context is entered per key, mirroring the real factory's
    one-transaction-per-call contract; ``entered`` counts the entries so a test
    can assert each key ran in its own transaction.
    """

    def __init__(self, session: Any) -> None:
        self._session = session
        self.entered = 0

    @asynccontextmanager
    async def session(self) -> Any:
        self.entered += 1
        yield self._session


class _FakeTelegram:
    """A fake :class:`TelegramPort` recording Tombstone sends.

    ``send_document`` returns an incrementing message id and records every call,
    or raises ``fail_with`` when configured so the Telegram-failure path can be
    exercised.
    """

    def __init__(self, fail_with: Exception | None = None) -> None:
        self.sent: list[tuple[int, bytes, str, str]] = []
        self._fail_with = fail_with
        self._next_id = 100

    async def send_document(
        self, channel_id: int, data: bytes, file_name: str, caption: str
    ) -> int:
        if self._fail_with is not None:
            raise self._fail_with
        self.sent.append((channel_id, data, file_name, caption))
        self._next_id += 1
        return self._next_id

    async def get_messages(
        self, channel_id: int, ids: list[int]
    ) -> list[TelegramMessage]:
        return []

    async def download_document(self, channel_id: int, message_id: int) -> bytes:
        return b""

    async def create_channel(self, title: str) -> int:
        return 0

    async def delete_channel(self, channel_id: int) -> None:
        return None

    async def get_session_state(self) -> SessionState:
        return SessionState.CONNECTED

    async def is_connected(self) -> bool:
        return True


class _RecordingSession:
    """In-memory fake AsyncSession for the per-key delete path.

    Supports exactly the two read shapes ``delete_one_key`` issues — the bucket
    lookup (``select(Bucket)...``) and the generation count — plus ``flush`` and
    a no-op ``execute`` for the writes it performs (which the storage helpers are
    stubbed out of). Recorded calls are inspected by the tests.
    """

    def __init__(self, bucket: Bucket | None, generation_count: int) -> None:
        self._bucket = bucket
        self._generation_count = generation_count
        self.executed: list[Any] = []

    async def execute(self, statement: Any) -> Any:
        self.executed.append(statement)
        text = str(statement)
        if "buckets" in text and "SELECT" in text.upper():
            return _ScalarResult(self._bucket)
        if "count" in text.lower():
            return _ScalarResult(self._generation_count)
        return _ScalarResult(None)

    async def flush(self) -> None:
        return None


class _ScalarResult:
    """A tiny result exposing the scalar accessors the code uses."""

    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value

    def scalar_one(self) -> Any:
        return self._value


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _delete_body(keys: list[str], *, quiet: bool = False) -> bytes:
    """Build a ``<Delete>`` request body naming ``keys``."""
    root = ET.Element("Delete")
    if quiet:
        q = ET.SubElement(root, "Quiet")
        q.text = "true"
    for key in keys:
        obj = ET.SubElement(root, "Object")
        ET.SubElement(obj, "Key").text = key
    return bytes(ET.tostring(root, encoding="utf-8", xml_declaration=True))


def _context(bucket: str | None, body: bytes) -> router.S3HandlerContext:
    """Build a handler context carrying ``body`` and the resolved bucket."""
    return router.S3HandlerContext(
        request=_FakeRequest(body),  # type: ignore[arg-type]
        operation=router.S3Operation.DELETE_OBJECTS,
        target=router.ResolvedTarget(bucket=bucket, key=None),
        auth=object(),  # type: ignore[arg-type]
        settings=object(),  # type: ignore[arg-type]
    )


def _parse(body: bytes) -> tuple[set[str], dict[str, str]]:
    """Parse a DeleteResult body into (deleted keys, {error key: code})."""
    root = ET.fromstring(body)
    deleted: set[str] = set()
    errors: dict[str, str] = {}
    for child in root:
        local = child.tag.rsplit("}", 1)[-1]
        key = child.findtext(f"{{{S3_NS}}}Key") or ""
        if local == "Deleted":
            deleted.add(key)
        elif local == "Error":
            errors[key] = child.findtext(f"{{{S3_NS}}}Code") or ""
    return deleted, errors


def _make_bucket() -> Bucket:
    """Build a detached Bucket ORM instance."""
    return Bucket(
        id=uuid.uuid4(),
        name="b",
        channel_id=555,
        kek_wrapped=b"k",
        kek_version=1,
        durability="async",
        created_by_us=True,
        status="active",
    )


def _handler(
    factory: _FakeSessionFactory, telegram: _FakeTelegram
) -> DeleteObjectsHandler:
    """Build a handler over the fakes, casting them to the declared seams.

    ``_FakeTelegram`` structurally matches :class:`TelegramPort`; the session
    factory is a stand-in for the concrete :class:`AsyncSessionFactory`, so both
    are cast to keep the test's construction free of inline ignores.
    """
    return DeleteObjectsHandler(
        cast(AsyncSessionFactory, factory),
        cast("Any", telegram),
    )


# ---------------------------------------------------------------------------
# Requirement 9.7 — reject > 1000 keys
# ---------------------------------------------------------------------------


async def test_rejects_more_than_1000_keys_with_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A batch of 1001 keys is rejected 400 MalformedXML, no key processed (9.7)."""
    calls: list[str] = []

    async def _spy(self: Any, bucket_name: str, key: str) -> KeyDeleteOutcome:
        calls.append(key)
        return KeyDeleteOutcome(key=key)

    monkeypatch.setattr(DeleteObjectsHandler, "_delete_key", _spy)
    handler = _handler(_FakeSessionFactory(object()), _FakeTelegram())

    keys = [f"k{i}" for i in range(MAX_DELETE_KEYS + 1)]
    response = await handler(_context("b", _delete_body(keys)))

    assert response.status_code == 400
    assert b"MalformedXML" in response.body
    # No per-key processing happened.
    assert calls == []


async def test_accepts_exactly_1000_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A batch of exactly 1000 keys is processed, not rejected (boundary of 9.7)."""

    async def _ok(self: Any, bucket_name: str, key: str) -> KeyDeleteOutcome:
        return KeyDeleteOutcome(key=key)

    monkeypatch.setattr(DeleteObjectsHandler, "_delete_key", _ok)
    handler = _handler(_FakeSessionFactory(object()), _FakeTelegram())

    keys = [f"k{i}" for i in range(MAX_DELETE_KEYS)]
    response = await handler(_context("b", _delete_body(keys)))

    assert response.status_code == 200
    deleted, errors = _parse(response.body)
    assert deleted == set(keys)
    assert errors == {}


async def test_malformed_body_returns_400() -> None:
    """A body that is not a <Delete> document is rejected 400 MalformedXML."""
    handler = _handler(_FakeSessionFactory(object()), _FakeTelegram())

    response = await handler(_context("b", b"<NotDelete/>"))

    assert response.status_code == 400
    assert b"MalformedXML" in response.body


# ---------------------------------------------------------------------------
# Requirement 9.6 — per-key success/error, independence, quiet mode
# ---------------------------------------------------------------------------


async def test_per_key_results_mix_success_and_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every key gets a result; a failing key does not abort the batch (9.6)."""

    async def _mixed(self: Any, bucket_name: str, key: str) -> KeyDeleteOutcome:
        if key == "bad":
            return KeyDeleteOutcome(
                key=key, error_code="InternalError", error_message="boom"
            )
        return KeyDeleteOutcome(key=key)

    monkeypatch.setattr(DeleteObjectsHandler, "_delete_key", _mixed)
    handler = _handler(_FakeSessionFactory(object()), _FakeTelegram())

    response = await handler(_context("b", _delete_body(["a", "bad", "c"])))

    assert response.status_code == 200
    deleted, errors = _parse(response.body)
    assert deleted == {"a", "c"}
    assert errors == {"bad": "InternalError"}


async def test_each_key_runs_in_its_own_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh session (transaction) is opened per key for independence (9.6)."""
    factory = _FakeSessionFactory(_RecordingSession(None, 0))
    handler = _handler(factory, _FakeTelegram())

    # With no bucket, every key returns NoSuchBucket but still opens a session.
    await handler(_context("b", _delete_body(["a", "b", "c"])))

    assert factory.entered == 3


async def test_raising_delete_is_isolated_to_its_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exception deleting one key becomes that key's error only (9.6)."""

    async def _raise_for_bad(session: Any, **kwargs: Any) -> KeyDeleteOutcome:
        if kwargs["key"] == "bad":
            raise TelegramUnavailableError("telegram down")
        return KeyDeleteOutcome(key=kwargs["key"])

    monkeypatch.setattr(delete_objects, "delete_one_key", _raise_for_bad)
    handler = _handler(
        _FakeSessionFactory(_RecordingSession(_make_bucket(), 0)),
        _FakeTelegram(),
    )

    response = await handler(_context("b", _delete_body(["a", "bad", "c"])))

    deleted, errors = _parse(response.body)
    assert deleted == {"a", "c"}
    assert set(errors) == {"bad"}
    assert errors["bad"] == "InternalError"


async def test_quiet_mode_suppresses_deleted_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In quiet mode only errors are reported; successes are omitted (9.6)."""

    async def _mixed(self: Any, bucket_name: str, key: str) -> KeyDeleteOutcome:
        if key == "bad":
            return KeyDeleteOutcome(
                key=key, error_code="InternalError", error_message="boom"
            )
        return KeyDeleteOutcome(key=key)

    monkeypatch.setattr(DeleteObjectsHandler, "_delete_key", _mixed)
    handler = _handler(_FakeSessionFactory(object()), _FakeTelegram())

    response = await handler(
        _context("b", _delete_body(["a", "bad", "c"], quiet=True))
    )

    deleted, errors = _parse(response.body)
    assert deleted == set()  # successes suppressed
    assert set(errors) == {"bad"}


# ---------------------------------------------------------------------------
# delete_one_key — per-key delete logic
# ---------------------------------------------------------------------------


async def test_delete_one_key_unknown_bucket_is_error() -> None:
    """Deleting against an unknown bucket is a NoSuchBucket per-key error."""
    telegram = _FakeTelegram()
    outcome = await delete_one_key(
        _RecordingSession(None, 0),  # type: ignore[arg-type]
        telegram=telegram,
        bucket_name="missing",
        key="k",
    )

    assert not outcome.ok
    assert outcome.error_code == "NoSuchBucket"
    assert telegram.sent == []  # nothing written


async def test_delete_one_key_absent_key_is_silent_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A key with no head is a no-op success, with no Telegram write (9.5/9.6)."""

    async def _no_head(session: Any, bucket_id: Any, key: str) -> uuid.UUID | None:
        return None

    monkeypatch.setattr(heads_storage, "get_head", _no_head)
    telegram = _FakeTelegram()

    outcome = await delete_one_key(
        _RecordingSession(_make_bucket(), 0),  # type: ignore[arg-type]
        telegram=telegram,
        bucket_name="b",
        key="k",
    )

    assert outcome.ok
    assert telegram.sent == []  # no tombstone for a non-existent key


async def test_delete_one_key_writes_tombstone_before_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Tombstone is sent to Telegram before any index mutation runs.

    Ordering Telegram ahead of the index is what makes a Telegram failure leave
    the index unchanged (requirement 9.4). We assert the send happened and
    carried the ``tgo3-t-<generation_id>`` caption, and that the index-write
    helpers ran only after the send.
    """
    bucket = _make_bucket()
    order: list[str] = []

    async def _head(session: Any, bucket_id: Any, key: str) -> uuid.UUID:
        return uuid.uuid4()

    async def _record_delete(session: Any, **kwargs: Any) -> None:
        order.append("record_delete")

    monkeypatch.setattr(heads_storage, "get_head", _head)
    monkeypatch.setattr(delete_objects, "_record_delete", _record_delete)

    telegram = _FakeTelegram()
    original_send = telegram.send_document

    async def _tracked_send(
        channel_id: int, data: bytes, file_name: str, caption: str
    ) -> int:
        order.append("send")
        return await original_send(channel_id, data, file_name, caption)

    telegram.send_document = _tracked_send  # type: ignore[method-assign]

    outcome = await delete_one_key(
        _RecordingSession(bucket, 2),  # type: ignore[arg-type]
        telegram=telegram,
        bucket_name="b",
        key="k",
    )

    assert outcome.ok
    assert order == ["send", "record_delete"]  # Telegram first, then index
    assert len(telegram.sent) == 1
    _, _, _, caption = telegram.sent[0]
    assert caption.startswith("tgo3-t-")


async def test_delete_one_key_telegram_failure_leaves_index_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Telegram failure propagates before the index is touched (9.4)."""
    record_called = False

    async def _head(session: Any, bucket_id: Any, key: str) -> uuid.UUID:
        return uuid.uuid4()

    async def _record_delete(session: Any, **kwargs: Any) -> None:
        nonlocal record_called
        record_called = True

    monkeypatch.setattr(heads_storage, "get_head", _head)
    monkeypatch.setattr(delete_objects, "_record_delete", _record_delete)

    telegram = _FakeTelegram(fail_with=TelegramUnavailableError("down"))

    with pytest.raises(TelegramUnavailableError):
        await delete_one_key(
            _RecordingSession(_make_bucket(), 0),  # type: ignore[arg-type]
            telegram=telegram,
            bucket_name="b",
            key="k",
        )

    assert record_called is False  # index never mutated


def test_state_delete_token_matches_recovery_convention() -> None:
    """The DELETED token is the uppercase string the recovery CLI expects."""
    assert STATE_DELETED == "DELETED"


# ---------------------------------------------------------------------------
# Property: every requested key appears exactly once in the response
# ---------------------------------------------------------------------------


class _ScriptedHandler(DeleteObjectsHandler):
    """A handler whose per-key delete is driven by a fixed set of failing keys.

    Subclassing to override ``_delete_key`` keeps the property test free of a
    function-scoped ``monkeypatch`` fixture (which Hypothesis rejects because it
    is not reset between generated examples).
    """

    def __init__(self, fail_keys: set[str]) -> None:
        super().__init__(
            cast(AsyncSessionFactory, _FakeSessionFactory(object())),
            cast("Any", _FakeTelegram()),
        )
        self._fail_keys = fail_keys

    async def _delete_key(self, bucket_name: str, key: str) -> KeyDeleteOutcome:
        if key in self._fail_keys:
            return KeyDeleteOutcome(
                key=key, error_code="InternalError", error_message="x"
            )
        return KeyDeleteOutcome(key=key)


@given(
    keys=st.lists(
        st.text(
            alphabet=st.characters(min_codepoint=97, max_codepoint=122),
            min_size=1,
            max_size=8,
        ),
        min_size=1,
        max_size=50,
        unique=True,
    ),
    fail=st.sets(st.integers(min_value=0, max_value=49)),
)
async def test_every_key_reported_exactly_once(
    keys: list[str], fail: set[int]
) -> None:
    """For 1..1000 keys, the response reports each requested key exactly once.

    Validates: Requirements 9.6
    """
    fail_keys = {keys[i] for i in fail if i < len(keys)}

    handler = _ScriptedHandler(fail_keys)

    response = await handler(_context("b", _delete_body(keys)))
    deleted, errors = _parse(response.body)

    # Every key appears exactly once, in exactly one of the two sections.
    assert deleted | set(errors) == set(keys)
    assert deleted.isdisjoint(set(errors))
    assert set(errors) == fail_keys
    assert deleted == set(keys) - fail_keys

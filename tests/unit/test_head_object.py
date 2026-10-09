"""Unit tests for :mod:`gateway.app.s3.operations.head_object`.

Covers the HeadObject contract (Requirements 9.1, 9.2):

* 9.1 — a key resolving to a COMMITTED generation returns HTTP 200 with an empty
  body and ``Content-Length`` / ``Content-Type`` / ``ETag`` / ``Last-Modified``
  headers.
* 9.2 — a key that does not resolve (unknown bucket, no head, dangling head, or a
  head pointing at a non-COMMITTED generation) returns HTTP 404 with an empty
  body and no error document.

The handler performs only index reads, so the database is replaced with a small
in-memory fake session factory. The pure header helpers (ETag quoting and HTTP
date formatting) are also exercised directly, including a property test that
ETag quoting is idempotent.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from email.utils import format_datetime
from typing import Any

import pytest
from gateway.app.db.models import Generation
from gateway.app.s3 import router
from gateway.app.s3.operations.head_object import (
    DEFAULT_CONTENT_TYPE,
    HeadObjectHandler,
    _format_etag,
    _http_date,
)
from gateway.app.storage import heads
from gateway.app.storage.generations import STATE_COMMITTED, STATE_STAGED
from hypothesis import given
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeSession:
    """Minimal stand-in for an AsyncSession used by HeadObjectHandler.

    Serves ``select(Bucket.id).where(...)`` via ``execute`` from a name→id map
    and ``session.get(Generation, id)`` from a generation map. Only the two
    access shapes the handler uses are supported.
    """

    def __init__(
        self,
        buckets: dict[str, uuid.UUID],
        generations: dict[uuid.UUID, Generation],
    ) -> None:
        self._buckets = buckets
        self._generations = generations

    async def execute(self, statement: Any) -> Any:
        # The only execute() the handler issues is the bucket-id lookup. We
        # recover the requested name from the compiled statement's bound params.
        params = statement.compile().params
        name = next(iter(params.values()))
        bucket_id = self._buckets.get(name)

        class _Result:
            def __init__(self, value: uuid.UUID | None) -> None:
                self._value = value

            def scalar_one_or_none(self) -> uuid.UUID | None:
                return self._value

        return _Result(bucket_id)

    async def get(self, model: Any, pk: uuid.UUID) -> Generation | None:
        assert model is Generation
        return self._generations.get(pk)


class _FakeSessionFactory:
    """Session factory exposing the ``.session()`` async context manager."""

    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    @asynccontextmanager
    async def session(self) -> Any:
        yield self._session


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_generation(
    *,
    state: str,
    size: int = 42,
    content_type: str | None = "text/plain",
    etag: str | None = "abc123",
    last_modified: datetime | None = None,
) -> Generation:
    """Build a detached Generation ORM instance for the fake session."""
    return Generation(
        id=uuid.uuid4(),
        bucket_id=uuid.uuid4(),
        key="k",
        kind="put",
        blob_id=None,
        size=size,
        etag=etag,
        checksums=None,
        content_type=content_type,
        user_meta=None,
        last_modified=last_modified or datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC),
        state=state,
        commit_message_id=1,
        staging_path=None,
    )


def _context(bucket: str | None, key: str | None) -> router.S3HandlerContext:
    """Build a handler context with the given resolved target.

    ``request``, ``auth`` and ``settings`` are never read by HeadObject, so they
    are supplied as throwaway sentinels.
    """
    return router.S3HandlerContext(
        request=object(),  # type: ignore[arg-type]
        operation=router.S3Operation.HEAD_OBJECT,
        target=router.ResolvedTarget(bucket=bucket, key=key),
        auth=object(),  # type: ignore[arg-type]
        settings=object(),  # type: ignore[arg-type]
    )


def _build_handler(
    *,
    buckets: dict[str, uuid.UUID],
    generations: dict[uuid.UUID, Generation],
    head: uuid.UUID | None,
    monkeypatch: pytest.MonkeyPatch,
) -> HeadObjectHandler:
    """Wire a handler over the fakes and stub heads.get_head to return ``head``."""
    session = _FakeSession(buckets, generations)
    factory = _FakeSessionFactory(session)

    async def _fake_get_head(
        _session: Any, _bucket_id: uuid.UUID, _key: str
    ) -> uuid.UUID | None:
        return head

    monkeypatch.setattr(heads, "get_head", _fake_get_head)
    return HeadObjectHandler(factory)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Requirement 9.1 — found
# ---------------------------------------------------------------------------


async def test_head_object_returns_200_with_metadata_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A COMMITTED key returns 200, empty body, and all metadata headers (9.1)."""
    bucket_id = uuid.uuid4()
    gen = _make_generation(
        state=STATE_COMMITTED,
        size=1234,
        content_type="image/png",
        etag="deadbeef",
        last_modified=datetime(2024, 5, 6, 7, 8, 9, tzinfo=UTC),
    )
    handler = _build_handler(
        buckets={"mybucket": bucket_id},
        generations={gen.id: gen},
        head=gen.id,
        monkeypatch=monkeypatch,
    )

    response = await handler(_context("mybucket", "k"))

    assert response.status_code == 200
    assert response.body == b""
    assert response.headers["Content-Length"] == "1234"
    assert response.headers["Content-Type"] == "image/png"
    assert response.headers["ETag"] == '"deadbeef"'
    assert response.headers["Last-Modified"] == format_datetime(
        datetime(2024, 5, 6, 7, 8, 9, tzinfo=UTC), usegmt=True
    )


async def test_head_object_defaults_content_type_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A committed object with no recorded Content-Type uses the default (9.1)."""
    bucket_id = uuid.uuid4()
    gen = _make_generation(state=STATE_COMMITTED, content_type=None, etag=None)
    handler = _build_handler(
        buckets={"b": bucket_id},
        generations={gen.id: gen},
        head=gen.id,
        monkeypatch=monkeypatch,
    )

    response = await handler(_context("b", "k"))

    assert response.status_code == 200
    assert response.headers["Content-Type"] == DEFAULT_CONTENT_TYPE
    # No ETag recorded -> header omitted entirely.
    assert "ETag" not in response.headers


# ---------------------------------------------------------------------------
# Requirement 9.2 — not found
# ---------------------------------------------------------------------------


async def test_head_object_404_when_bucket_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown bucket yields 404 with an empty body (9.2)."""
    handler = _build_handler(
        buckets={},
        generations={},
        head=None,
        monkeypatch=monkeypatch,
    )

    response = await handler(_context("missing", "k"))

    assert response.status_code == 404
    assert response.body == b""


async def test_head_object_404_when_no_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A known bucket with no head row for the key yields 404 (9.2)."""
    handler = _build_handler(
        buckets={"b": uuid.uuid4()},
        generations={},
        head=None,
        monkeypatch=monkeypatch,
    )

    response = await handler(_context("b", "k"))

    assert response.status_code == 404
    assert response.body == b""


async def test_head_object_404_when_head_dangling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A head pointing at a vanished generation yields 404 (9.2)."""
    handler = _build_handler(
        buckets={"b": uuid.uuid4()},
        generations={},
        head=uuid.uuid4(),
        monkeypatch=monkeypatch,
    )

    response = await handler(_context("b", "k"))

    assert response.status_code == 404
    assert response.body == b""


async def test_head_object_404_when_generation_not_committed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A head pointing at a non-COMMITTED generation is treated as absent (9.2)."""
    gen = _make_generation(state=STATE_STAGED)
    handler = _build_handler(
        buckets={"b": uuid.uuid4()},
        generations={gen.id: gen},
        head=gen.id,
        monkeypatch=monkeypatch,
    )

    response = await handler(_context("b", "k"))

    assert response.status_code == 404
    assert response.body == b""


async def test_head_object_404_when_target_missing_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A target without a key short-circuits to 404 (9.2)."""
    handler = _build_handler(
        buckets={"b": uuid.uuid4()},
        generations={},
        head=None,
        monkeypatch=monkeypatch,
    )

    response = await handler(_context("b", None))

    assert response.status_code == 404
    assert response.body == b""


# ---------------------------------------------------------------------------
# Pure header helpers
# ---------------------------------------------------------------------------


def test_format_etag_wraps_unquoted() -> None:
    """A raw ETag is wrapped in double quotes."""
    assert _format_etag("abc") == '"abc"'


def test_format_etag_leaves_quoted_unchanged() -> None:
    """An already-quoted ETag is not double-wrapped."""
    assert _format_etag('"abc"') == '"abc"'


def test_format_etag_none_passthrough() -> None:
    """A missing ETag stays None so the header is omitted."""
    assert _format_etag(None) is None


def test_http_date_formats_utc() -> None:
    """A UTC datetime renders as an IMF-fixdate GMT string."""
    dt = datetime(1994, 11, 6, 8, 49, 37, tzinfo=UTC)
    assert _http_date(dt) == "Sun, 06 Nov 1994 08:49:37 GMT"


def test_http_date_assumes_utc_for_naive() -> None:
    """A naive datetime is treated as UTC rather than raising."""
    naive = datetime(1994, 11, 6, 8, 49, 37)
    assert _http_date(naive) == "Sun, 06 Nov 1994 08:49:37 GMT"


@given(st.text(min_size=0, max_size=20))
def test_format_etag_idempotent(raw: str) -> None:
    """Quoting an already-quoted ETag is a no-op (idempotence)."""
    once = _format_etag(raw)
    assert once is not None
    assert _format_etag(once) == once
    assert once.startswith('"') and once.endswith('"')

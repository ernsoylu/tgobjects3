# Feature: tgobjects3, Property 7: Object listing excludes non-COMMITTED generations
"""Property 7 — object listing excludes non-COMMITTED generations (Requirement 5.4).

Design "Property 7: Object listing excludes non-COMMITTED generations":

    *For any* bucket state containing a mix of STAGED, UPLOADING, COMMITTED,
    DELETED, and SUPERSEDED generations across any set of object keys, a
    ListObjectsV2 query against that state SHALL return only generations whose
    state is COMMITTED, and no key shall appear more than once in Contents.

The listing handler (``gateway/app/s3/operations/list_objects.py``) resolves
this invariant purely in SQL: it joins ``object_heads`` to the generation each
head names and keeps only rows whose ``generations.state`` is ``COMMITTED``
(Requirement 5.4). There is no pure in-memory listing path to exercise — the
filter, the join, and the lexicographic ordering all live in the database — so
this property is validated against a *real* PostgreSQL 17 instance started with
``testcontainers[postgres]``. A fake would have to re-implement the join and the
state filter, which is precisely the behaviour under test, so the test uses the
genuine schema and drives the production :class:`ListObjectsHandler`.

Strategy and oracle
-------------------
The ``bucket_with_generations`` hypothesis strategy produces, for a small set of
distinct keys, one or more generations per key with states drawn from the full
mix {STAGED, UPLOADING, COMMITTED, DELETED, SUPERSEDED}. Each key's
``object_heads`` row is pointed at exactly one of its generations (the "head
generation"), modelling the index state the listing reads. The oracle is then
simple and independent of the handler's SQL: a key must appear in ``Contents``
iff its head generation is COMMITTED, and because ``object_heads`` is keyed on
(bucket, key) a key can appear at most once.

To keep per-example work bounded (and avoid Hypothesis ``DeadlineExceeded``
against a real database) each example seeds only a handful of keys, every
example reuses the one container and bucket seeded once at module scope, each
example cleans up the rows it inserted, and the Hypothesis deadline is disabled
for the single property test — the container start, not per-example logic, is
the slow part.

The suite needs a Docker daemon to launch PostgreSQL. When no container runtime
is available (CI without Docker, or a sandbox) the module skips cleanly rather
than failing, so ``make lint`` / ``make typecheck`` stay meaningful without a
daemon present. No Telegram access occurs: listing never touches the transport.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from xml.etree import ElementTree as ET

import pytest
from gateway.app.config import Settings
from gateway.app.db.models import Generation, ObjectHead
from gateway.app.db.session import AsyncSessionFactory
from gateway.app.s3 import router as s3_router
from gateway.app.s3 import xml_codecs
from gateway.app.s3.operations.list_objects import ListObjectsHandler
from gateway.app.storage.generations import (
    STATE_COMMITTED,
    STATE_DELETED,
    STATE_STAGED,
    STATE_SUPERSEDED,
    STATE_UPLOADING,
)
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request

# ``testcontainers`` is an integration-only dependency and starting a container
# requires a Docker daemon. Guard the import so the module can be collected
# everywhere and skip with a clear reason when the package is absent.
try:
    from testcontainers.postgres import PostgresContainer

    _TESTCONTAINERS_IMPORT_ERROR: str | None = None
except Exception as exc:  # pragma: no cover - exercised only without the extra
    PostgresContainer = None  # type: ignore[assignment,misc]
    _TESTCONTAINERS_IMPORT_ERROR = str(exc)


# Repository root: tests/integration/<file>.py -> repo root is two parents up.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_ALEMBIC_INI = _REPO_ROOT / "alembic.ini"

#: Postgres image pinned to the major version the design mandates (17+).
_POSTGRES_IMAGE = "postgres:17-alpine"

#: Master key satisfying the >=16-char ``TGOS3_MASTER_KEY`` contract. The
#: listing path never touches encryption; this only has to let ``Settings``
#: construct.
_MASTER_KEY = "integration-test-master-key-0001"

#: The one bucket every example seeds its generations into.
_BUCKET_NAME = "list-committed-only"
_BUCKET_CHANNEL_ID = 9201

#: The full state mix the property quantifies over. Exactly one of these is a
#: "visible" head state (COMMITTED); the other four must be invisible to a
#: listing (Requirement 5.4).
_HEAD_STATES = (
    STATE_STAGED,
    STATE_UPLOADING,
    STATE_COMMITTED,
    STATE_DELETED,
    STATE_SUPERSEDED,
)

#: S3 listing XML namespace, used to read ``Contents/Key`` out of the response.
_S3_NS = xml_codecs.S3_NAMESPACE


# ---------------------------------------------------------------------------
# Hypothesis strategy: a bucket state of keys with mixed-state generations
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _KeyPlan:
    """One key's planned generations and which one its head points at.

    Attributes:
        key: The object key.
        states: The states of the generations to insert for this key, in order.
        head_index: Index into ``states`` of the generation the ``object_heads``
            row points at — the only generation that can make the key visible.
    """

    key: str
    states: tuple[str, ...]
    head_index: int


@dataclass(frozen=True)
class _BucketPlan:
    """A whole bucket state: a set of keys, each with mixed-state generations."""

    keys: tuple[_KeyPlan, ...]

    @property
    def expected_committed_keys(self) -> set[str]:
        """Keys whose head generation is COMMITTED — the listing oracle.

        A key is visible in ``Contents`` iff the generation its head names is
        COMMITTED; every other head state is invisible (Requirement 5.4).
        """
        return {
            plan.key
            for plan in self.keys
            if plan.states[plan.head_index] == STATE_COMMITTED
        }


# Keys are drawn from a small lexicographic alphabet so a handful of distinct
# keys fit comfortably and ordering stays easy to reason about. The listing is
# flat (no delimiter), so key shape is irrelevant to the property under test.
_key_strategy = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789/-_",
    min_size=1,
    max_size=12,
)


@st.composite
def _key_plan(draw: st.DrawFn, key: str) -> _KeyPlan:
    """Draw one key's generation states and the head it resolves to.

    Each key gets 1–3 generations with independently-drawn states from the full
    mix, and the head points at a uniformly-chosen one of them. Allowing several
    generations per key (several of them potentially COMMITTED) makes "the head
    decides visibility" a real constraint rather than a one-generation accident.
    """
    states = tuple(
        draw(
            st.lists(
                st.sampled_from(_HEAD_STATES),
                min_size=1,
                max_size=3,
            )
        )
    )
    head_index = draw(st.integers(min_value=0, max_value=len(states) - 1))
    return _KeyPlan(key=key, states=states, head_index=head_index)


@st.composite
def bucket_with_generations(draw: st.DrawFn) -> _BucketPlan:
    """Generate a bucket state of distinct keys with mixed-state generations.

    Produces 0–6 distinct keys (bounded to keep per-example DB work small), each
    with its own 1–3 generations across the {STAGED, UPLOADING, COMMITTED,
    DELETED, SUPERSEDED} mix and a chosen head generation. Keys are drawn as a
    *set* so no key is planned twice — the uniqueness the ``object_heads``
    primary key enforces — which lets the duplicate-free assertion be meaningful
    rather than trivially satisfied by the generator.
    """
    keys = draw(
        st.lists(_key_strategy, min_size=0, max_size=6, unique=True)
    )
    plans = tuple(draw(_key_plan(key)) for key in keys)
    return _BucketPlan(keys=plans)


# ---------------------------------------------------------------------------
# PostgreSQL helpers (same normalisation/migration flow the gateway uses)
# ---------------------------------------------------------------------------


def _async_url(sync_url: str) -> str:
    """Normalise a container connection URL to the ``asyncpg`` driver."""
    bare = sync_url.split("://", 1)[1]
    return "postgresql+asyncpg://" + bare


def _run_alembic_upgrade(async_url: str) -> None:
    """Run ``alembic upgrade head`` through the project's real ``env.py``."""
    from alembic import command
    from alembic.config import Config

    config = Config(str(_ALEMBIC_INI))
    config.set_main_option("script_location", "gateway/app/db/migrations")

    previous_url = os.environ.get("DATABASE_URL")
    previous_cwd = Path.cwd()
    os.environ["DATABASE_URL"] = async_url
    try:
        os.chdir(_REPO_ROOT)
        command.upgrade(config, "head")
    finally:
        os.chdir(previous_cwd)
        if previous_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_url


def _build_settings(async_url: str) -> Settings:
    """Construct a frozen :class:`Settings` for the listing run.

    Only the fields the listing path and ``Settings`` validation require are set;
    the handler never consults durability or size limits. Configuration is
    consumed solely through this object, per the centralised-config convention.
    """
    return Settings(
        TGOS3_MASTER_KEY=_MASTER_KEY,
        DATABASE_URL=async_url,
    )


async def _ensure_bucket(
    session_factory: async_sessionmaker[AsyncSession], bucket_id: uuid.UUID
) -> None:
    """Insert the one ``buckets`` row every example's generations reference.

    Idempotent (``ON CONFLICT (id) DO NOTHING``) because each Hypothesis example
    opens a fresh engine in its own event loop and re-ensures the bucket; the
    first example inserts it and the rest are no-ops. The listing never unwraps
    the KEK, so the wrapped-KEK column is an opaque placeholder; only the row's
    existence and name matter here.
    """
    async with session_factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO buckets "
                "(id, name, channel_id, kek_wrapped, kek_version, durability, "
                " created_by_us, status) "
                "VALUES (:id, :name, :channel_id, :kek_wrapped, :kek_version, "
                " :durability, :created_by_us, :status) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {
                "id": bucket_id,
                "name": _BUCKET_NAME,
                "channel_id": _BUCKET_CHANNEL_ID,
                "kek_wrapped": b"\x00" * 32,
                "kek_version": 1,
                "durability": "async",
                "created_by_us": True,
                "status": "active",
            },
        )


async def _seed_plan(
    session_factory: async_sessionmaker[AsyncSession],
    bucket_id: uuid.UUID,
    plan: _BucketPlan,
) -> None:
    """Insert every planned generation and point each key's head at one of them.

    For each key the generations are inserted in order and the ``object_heads``
    row is set to the planned head generation. Timestamps are naive UTC to match
    the ``generations.last_modified`` TIMESTAMP WITHOUT TIME ZONE column and the
    gateway's own write path.
    """
    now = datetime.now(UTC).replace(tzinfo=None)
    async with session_factory() as session, session.begin():
        for key_plan in plan.keys:
            generation_ids: list[uuid.UUID] = []
            for offset, state in enumerate(key_plan.states):
                generation_id = uuid.uuid4()
                generation_ids.append(generation_id)
                session.add(
                    Generation(
                        id=generation_id,
                        bucket_id=bucket_id,
                        key=key_plan.key,
                        kind="put",
                        blob_id=None,
                        size=offset,
                        etag=f"etag-{offset}",
                        checksums=None,
                        content_type="application/octet-stream",
                        user_meta=None,
                        last_modified=now,
                        state=state,
                        commit_message_id=None,
                        staging_path=None,
                    )
                )
            session.add(
                ObjectHead(
                    bucket_id=bucket_id,
                    key=key_plan.key,
                    generation_id=generation_ids[key_plan.head_index],
                )
            )


async def _clear_rows(
    session_factory: async_sessionmaker[AsyncSession], bucket_id: uuid.UUID
) -> None:
    """Remove all heads and generations for the bucket between examples.

    Every example reuses the one seeded bucket; clearing its heads and
    generations (but keeping the bucket row) keeps per-example work bounded and
    examples independent. ``object_heads`` is deleted first because it
    references ``generations``.
    """
    async with session_factory() as session, session.begin():
        await session.execute(
            text("DELETE FROM object_heads WHERE bucket_id = :b"),
            {"b": bucket_id},
        )
        await session.execute(
            text("DELETE FROM generations WHERE bucket_id = :b"),
            {"b": bucket_id},
        )


# ---------------------------------------------------------------------------
# Driving the production ListObjectsV2 handler in-process
# ---------------------------------------------------------------------------


def _build_asgi_request(path: str, query_string: str) -> Request:
    """Build a real Starlette GET :class:`Request` for the listing path.

    The handler reads ``request.query_params`` (via the ASGI ``query_string``),
    so constructing a genuine ``Request`` over an ASGI scope exercises the same
    parameter-parsing surface a live uvicorn request would, without a server.
    """
    scope: dict[str, Any] = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "path": path,
        "raw_path": path.encode("latin-1"),
        "query_string": query_string.encode("latin-1"),
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "server": ("localhost", 9000),
        "scheme": "http",
    }

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(scope, receive)


def _make_context(settings: Settings) -> s3_router.S3HandlerContext:
    """Assemble the ListObjectsV2 handler context the router would hand over.

    ``?list-type=2`` selects the v2 dialect; ``max-keys`` is raised to its
    ceiling so a whole example's committed keys fit on one page (the property is
    about *which* keys appear, not pagination). SigV4 has already run by the time
    a handler is invoked, so ``auth`` is a throwaway sentinel the listing never
    consults (Requirement 6.1).
    """
    request = _build_asgi_request(
        f"/{_BUCKET_NAME}", "list-type=2&max-keys=1000"
    )
    return s3_router.S3HandlerContext(
        request=request,
        operation=s3_router.S3Operation.LIST_OBJECTS_V2,
        target=s3_router.ResolvedTarget(bucket=_BUCKET_NAME, key=None),
        auth=cast("Any", object()),
        settings=settings,
    )


def _parse_contents_keys(body: bytes) -> list[str]:
    """Extract the ``Contents/Key`` values from a ListObjectsV2 XML body."""
    root = ET.fromstring(body)
    return [
        el.text or ""
        for el in root.findall(f"{{{_S3_NS}}}Contents/{{{_S3_NS}}}Key")
    ]


# ---------------------------------------------------------------------------
# Container harness (module-scoped: start once, reuse per example)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Harness:
    """The live container-backed context every example runs against.

    Only loop-independent values are held here: the connection URL and the
    pre-chosen bucket id. Each Hypothesis example builds (and disposes) its own
    :class:`AsyncSessionFactory` inside a single :func:`asyncio.run`, because
    ``asyncpg`` connections are bound to the event loop that created them and
    Hypothesis invokes the sync test body once per example.
    """

    async_url: str
    settings: Settings
    bucket_id: uuid.UUID


def _skip_if_no_docker() -> None:
    """Skip the whole module when ``testcontainers`` cannot be imported."""
    if PostgresContainer is None:
        pytest.skip(
            "testcontainers[postgres] is unavailable: "
            f"{_TESTCONTAINERS_IMPORT_ERROR}"
        )


@pytest.fixture(scope="module")
def harness() -> Iterator[_Harness]:
    """Start PostgreSQL 17 once, migrate, seed the bucket, yield the harness.

    Module-scoped so the expensive container start and migration run happen once
    for the whole property; each Hypothesis example only inserts and clears a
    few rows against the live factory. Skips cleanly when Docker is unavailable
    so the suite stays collectable everywhere.
    """
    _skip_if_no_docker()

    try:
        container = PostgresContainer(_POSTGRES_IMAGE)
        container.start()
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"Could not start PostgreSQL test container: {exc}")

    try:
        async_url = _async_url(container.get_connection_url())
        _run_alembic_upgrade(async_url)
        settings = _build_settings(async_url)
        bucket_id = uuid.uuid4()
        yield _Harness(
            async_url=async_url,
            settings=settings,
            bucket_id=bucket_id,
        )
    finally:
        container.stop()


# ---------------------------------------------------------------------------
# The property test
# ---------------------------------------------------------------------------


async def _list_committed_keys(harness: _Harness, plan: _BucketPlan) -> list[str]:
    """Seed one bucket state, run ListObjectsV2, return the ``Contents`` keys.

    Builds and disposes a fresh :class:`AsyncSessionFactory` within the single
    event loop this coroutine runs in, so every ``asyncpg`` connection it opens
    belongs to that loop (Hypothesis runs the sync body once per example, each
    under its own :func:`asyncio.run`).
    """
    factory = AsyncSessionFactory.create(harness.async_url)
    try:
        await _ensure_bucket(factory.sessionmaker, harness.bucket_id)
        await _seed_plan(factory.sessionmaker, harness.bucket_id, plan)
        try:
            handler = ListObjectsHandler(factory)
            response = await handler(_make_context(harness.settings))
            assert response.status_code == 200
            return _parse_contents_keys(bytes(response.body))
        finally:
            await _clear_rows(factory.sessionmaker, harness.bucket_id)
    finally:
        await factory.dispose()


@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(plan=bucket_with_generations())
def test_listing_returns_only_committed_without_duplicates(
    harness: _Harness, plan: _BucketPlan
) -> None:
    """ListObjectsV2 returns exactly the COMMITTED-headed keys, each at most once.

    Property 7: across any bucket state mixing STAGED, UPLOADING, COMMITTED,
    DELETED and SUPERSEDED generations over any set of keys, the listing returns
    only keys whose head generation is COMMITTED and no key appears twice in
    ``Contents``.

    Validates: Requirements 5.4
    """
    returned = asyncio.run(_list_committed_keys(harness, plan))

    # No key appears more than once in Contents.
    assert len(returned) == len(set(returned))

    # Exactly the keys whose head generation is COMMITTED are returned — every
    # non-COMMITTED head state (STAGED, UPLOADING, DELETED, SUPERSEDED) is
    # invisible (Requirement 5.4).
    assert set(returned) == plan.expected_committed_keys

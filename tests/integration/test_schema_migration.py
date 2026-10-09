"""Example-based tests for the Alembic schema migration (Requirement 1.4).

Requirement 1.4 states that the Gateway applies all Alembic migrations when it
starts, before accepting any requests. These tests exercise that migration path
end to end against a *real* PostgreSQL 17 instance started with
``testcontainers[postgres]``: they run ``alembic upgrade head`` exactly as the
startup path does, then introspect the resulting database and assert that every
table from the design's PostgreSQL Schema ER diagram exists with the column
layout and types the SQLAlchemy models (``gateway/app/db/models.py``) declare.

Because the index is a *rebuildable cache* whose authoritative state lives in
Telegram, the schema is the contract the recovery CLI and operational tooling
rely on; a drift between the ORM models and what the migration actually creates
would be invisible until recovery time. Verifying the migrated schema here keeps
that contract honest.

Everything talks to PostgreSQL through the project's pinned ``asyncpg`` driver —
the same driver the gateway and ``env.py`` use — so the test needs no extra
sync driver (e.g. psycopg2). Schema reflection is inherently synchronous in
SQLAlchemy, so it runs inside ``AsyncConnection.run_sync`` and the collected
facts are frozen into a plain snapshot the (synchronous) test functions assert
against.

The suite is an *integration* test: it needs a Docker daemon to launch the
PostgreSQL container. When no container runtime is available (e.g. CI without
Docker, or a sandbox) the module skips cleanly rather than failing, so ``make
lint`` / ``make typecheck`` stay meaningful without a daemon present.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

# ``testcontainers`` is an integration-only dependency and starting a container
# requires a Docker daemon. Guard the import so the module can be collected
# everywhere and skip with a clear reason when the package is absent.
try:
    from testcontainers.postgres import PostgresContainer

    _TESTCONTAINERS_IMPORT_ERROR: str | None = None
except Exception as exc:  # pragma: no cover - exercised only without the extra
    PostgresContainer = None
    _TESTCONTAINERS_IMPORT_ERROR = str(exc)


# Repository root, derived from this file's location:
# tests/integration/test_schema_migration.py -> repo root is two parents up.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_ALEMBIC_INI = _REPO_ROOT / "alembic.ini"

#: Postgres image pinned to the major version the design mandates (PostgreSQL
#: 17+). ``-alpine`` keeps the pull small; the server behaviour we assert on is
#: identical to the default image.
_POSTGRES_IMAGE = "postgres:17-alpine"

#: The single head revision of the migration tree (``initial_schema``).
_HEAD_REVISION = "21748135401f"


# ---------------------------------------------------------------------------
# Expected schema, transcribed from the design's PostgreSQL Schema ER diagram
# and the SQLAlchemy models. Each column maps to the reflected SQLAlchemy type
# class the inspector returns for the migrated PostgreSQL column. Using the type
# *class* (rather than a dialect-specific name string) keeps the assertion
# robust while still pinning the storage class the design calls for:
#
#   design type   -> SQLAlchemy reflected type
#   uuid          -> sqlalchemy.UUID
#   text          -> sqlalchemy.Text (reflected as TEXT)
#   bigint        -> sqlalchemy.BigInteger
#   int           -> sqlalchemy.Integer
#   bytea         -> sqlalchemy.LargeBinary (reflected as BYTEA)
#   jsonb         -> sqlalchemy.JSON (postgresql JSONB is a JSON subclass)
#   bool          -> sqlalchemy.Boolean
#   float         -> sqlalchemy.Float (reflected as DOUBLE PRECISION)
#   timestamptz*  -> sqlalchemy.DateTime  (* models use DateTime(); see note)
#
# Note: the ER diagram labels the time columns ``timestamptz`` while the models
# and migration declare ``sa.DateTime()`` (TIMESTAMP WITHOUT TIME ZONE). The
# migration is the authoritative artifact under test, so the expectations below
# follow the migration (plain ``DateTime``). A future migration that switches to
# timezone-aware timestamps would update both the model and this expectation.
# ---------------------------------------------------------------------------
_EXPECTED_SCHEMA: dict[str, dict[str, type[object]]] = {
    "buckets": {
        "id": sa.UUID,
        "name": sa.Text,
        "channel_id": sa.BigInteger,
        "kek_wrapped": sa.LargeBinary,
        "kek_version": sa.Integer,
        "durability": sa.Text,
        "created_by_us": sa.Boolean,
        "status": sa.Text,
    },
    "object_heads": {
        "bucket_id": sa.UUID,
        "key": sa.Text,
        "generation_id": sa.UUID,
    },
    "generations": {
        "id": sa.UUID,
        "bucket_id": sa.UUID,
        "key": sa.Text,
        "kind": sa.Text,
        "blob_id": sa.UUID,
        "size": sa.BigInteger,
        "etag": sa.Text,
        "checksums": sa.JSON,
        "content_type": sa.Text,
        "user_meta": sa.JSON,
        "last_modified": sa.DateTime,
        "state": sa.Text,
        "commit_message_id": sa.BigInteger,
        "staging_path": sa.Text,
    },
    "blobs": {
        "id": sa.UUID,
        "bucket_id": sa.UUID,
        "sha256": sa.LargeBinary,
        "size": sa.BigInteger,
        "key_wrapped": sa.LargeBinary,
        "chunk_records": sa.Integer,
        "state": sa.Text,
        "refcount": sa.Integer,
        "manifest_message_id": sa.BigInteger,
    },
    "chunks": {
        "blob_id": sa.UUID,
        "idx": sa.Integer,
        "message_id": sa.BigInteger,
        "records": sa.Integer,
        "document_id": sa.BigInteger,
        "file_reference": sa.LargeBinary,
        "dc_id": sa.Integer,
    },
    "upload_jobs": {
        "id": sa.UUID,
        "generation_id": sa.UUID,
        "state": sa.Text,
        "attempts": sa.Integer,
        "next_attempt_at": sa.DateTime,
        "last_error": sa.Text,
    },
    "multipart_uploads": {
        "upload_id": sa.UUID,
        "bucket_id": sa.UUID,
        "key": sa.Text,
        "expires_at": sa.DateTime,
    },
    "multipart_parts": {
        "upload_id": sa.UUID,
        "part_no": sa.Integer,
        "size": sa.BigInteger,
        "etag": sa.Text,
        "checksum": sa.JSON,
        "staging_path": sa.Text,
    },
    "access_keys": {
        "access_key_id": sa.Text,
        "secret_enc": sa.Text,
        "label": sa.Text,
        "grants": sa.JSON,
        "disabled": sa.Boolean,
    },
    "checkpoints": {
        "id": sa.UUID,
        "message_id": sa.BigInteger,
        "created_at": sa.DateTime,
        "high_water": sa.JSON,
    },
    "access_stats": {
        "blob_id": sa.UUID,
        "record_index": sa.Integer,
        "score": sa.Float,
        "scored_at": sa.DateTime,
    },
}

#: Named indexes the migration creates on top of the primary-key / unique-
#: constraint indexes. These back the hot query paths and the dedup guarantee;
#: the recovery CLI and operational tooling reference them by name.
_EXPECTED_INDEXES: dict[str, set[str]] = {
    "blobs": {"ix_blobs_bucket_sha256"},
    "generations": {"ix_generations_bucket_key", "ix_generations_state"},
    "upload_jobs": {"ix_upload_jobs_state_next"},
}


@dataclass(frozen=True)
class _TableFacts:
    """Reflected facts about one table, collected once from the live database.

    Attributes:
        columns: Column name -> reflected SQLAlchemy ``TypeEngine`` instance.
        index_names: Names of the (non-constraint) indexes on the table.
        unique_constraint_names: Names of the table's UNIQUE constraints.
        unique_index_names: Names of indexes flagged unique by the reflector.
    """

    columns: dict[str, sa.types.TypeEngine[object]]
    index_names: set[str]
    unique_constraint_names: set[str]
    unique_index_names: set[str]


@dataclass(frozen=True)
class _SchemaSnapshot:
    """An immutable snapshot of the migrated schema for the test functions."""

    table_names: set[str]
    applied_revision: str | None
    tables: dict[str, _TableFacts] = field(default_factory=dict)


def _skip_if_no_docker() -> None:
    """Skip the whole module when ``testcontainers`` cannot be imported."""
    if PostgresContainer is None:
        pytest.skip(
            "testcontainers[postgres] is unavailable: "
            f"{_TESTCONTAINERS_IMPORT_ERROR}"
        )


def _async_url(sync_url: str) -> str:
    """Normalise a container connection URL to the ``asyncpg`` driver.

    ``PostgresContainer.get_connection_url`` returns a sync URL (often naming
    psycopg2). The gateway only ships ``asyncpg``, so we rewrite the scheme to
    match — the same normalisation ``env.py`` performs on ``DATABASE_URL``.
    """
    bare = sync_url.split("://", 1)[1]
    return "postgresql+asyncpg://" + bare


def _run_alembic_upgrade(async_url: str) -> None:
    """Run ``alembic upgrade head`` through the project's real ``env.py``.

    Drives Alembic exactly as the gateway does at startup (Requirement 1.4):
    ``env.py`` reads ``DATABASE_URL`` from the environment and runs the online
    migration against an async engine. We run from the repo root so the
    relative ``script_location`` resolves.
    """
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


def _collect_schema(conn: Connection) -> _SchemaSnapshot:
    """Reflect the live schema into an immutable snapshot (runs under run_sync).

    SQLAlchemy's inspector is synchronous, so this is invoked through
    :meth:`AsyncConnection.run_sync`, which hands it a sync ``Connection`` view
    over the asyncpg connection.
    """
    inspector = inspect(conn)
    table_names = set(inspector.get_table_names())

    applied_revision: str | None = None
    if "alembic_version" in table_names:
        applied_revision = conn.execute(
            sa.text("SELECT version_num FROM alembic_version")
        ).scalar_one()

    tables: dict[str, _TableFacts] = {}
    for table in _EXPECTED_SCHEMA:
        if table not in table_names:
            continue
        columns = {
            col["name"]: col["type"] for col in inspector.get_columns(table)
        }
        indexes = inspector.get_indexes(table)
        index_names = {idx["name"] for idx in indexes if idx["name"]}
        unique_index_names = {
            idx["name"] for idx in indexes if idx["name"] and idx.get("unique")
        }
        unique_constraint_names = {
            uc["name"]
            for uc in inspector.get_unique_constraints(table)
            if uc["name"]
        }
        tables[table] = _TableFacts(
            columns=columns,
            index_names=index_names,
            unique_constraint_names=unique_constraint_names,
            unique_index_names=unique_index_names,
        )

    return _SchemaSnapshot(
        table_names=table_names,
        applied_revision=applied_revision,
        tables=tables,
    )


async def _migrate_and_reflect(async_url: str) -> _SchemaSnapshot:
    """Reflect the schema over an asyncpg engine after the migration has run."""
    engine = create_async_engine(async_url)
    try:
        async with engine.connect() as conn:
            return await conn.run_sync(_collect_schema)
    finally:
        await engine.dispose()


@pytest.fixture(scope="module")
def schema() -> Iterator[_SchemaSnapshot]:
    """Start PostgreSQL 17, run ``alembic upgrade head``, yield a schema snapshot.

    Module-scoped so the (relatively expensive) container start, migration run,
    and reflection happen once for every assertion in this file. Skips cleanly
    when Docker is unavailable so the suite stays collectable everywhere.
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
        snapshot = asyncio.run(_migrate_and_reflect(async_url))
        yield snapshot
    finally:
        container.stop()


def test_all_tables_exist(schema: _SchemaSnapshot) -> None:
    """Every table from the ER diagram is present after ``upgrade head``."""
    expected_tables = set(_EXPECTED_SCHEMA)
    missing = expected_tables - schema.table_names
    assert not missing, f"migration did not create tables: {sorted(missing)}"

    # The only non-ER table that may legitimately exist is Alembic's own
    # version bookkeeping table; assert no *other* unexpected tables appear.
    unexpected = schema.table_names - expected_tables - {"alembic_version"}
    assert not unexpected, f"migration created unexpected tables: {sorted(unexpected)}"


def test_alembic_version_is_head(schema: _SchemaSnapshot) -> None:
    """The applied revision matches the single head revision."""
    assert schema.applied_revision == _HEAD_REVISION


@pytest.mark.parametrize("table_name", sorted(_EXPECTED_SCHEMA))
def test_table_columns_and_types(schema: _SchemaSnapshot, table_name: str) -> None:
    """Each table has exactly its expected columns with the expected types."""
    facts = schema.tables.get(table_name)
    assert facts is not None, f"table {table_name} was not reflected"

    expected_columns = _EXPECTED_SCHEMA[table_name]
    assert set(facts.columns) == set(expected_columns), (
        f"column set mismatch for {table_name}: "
        f"got {sorted(facts.columns)}, expected {sorted(expected_columns)}"
    )

    for col_name, expected_type in expected_columns.items():
        actual_type = facts.columns[col_name]
        assert isinstance(actual_type, expected_type), (
            f"{table_name}.{col_name}: expected a {expected_type.__name__}, "
            f"got {actual_type!r}"
        )


@pytest.mark.parametrize("table_name", sorted(_EXPECTED_INDEXES))
def test_named_indexes_exist(schema: _SchemaSnapshot, table_name: str) -> None:
    """Named secondary indexes from the design are present on each table."""
    facts = schema.tables.get(table_name)
    assert facts is not None, f"table {table_name} was not reflected"

    expected = _EXPECTED_INDEXES[table_name]
    missing = expected - facts.index_names
    assert not missing, (
        f"{table_name} is missing expected indexes {sorted(missing)}; "
        f"present: {sorted(facts.index_names)}"
    )


def test_blobs_dedup_unique_constraint(schema: _SchemaSnapshot) -> None:
    """The dedup guarantee on ``blobs(bucket_id, sha256)`` is enforced as unique.

    Deduplication within a bucket is keyed on the content digest; the unique
    index/constraint on ``blobs(bucket_id, sha256)`` is the hard guarantee
    behind the advisory-lock dedup path, so verify the migration created it.
    """
    facts = schema.tables.get("blobs")
    assert facts is not None, "blobs table was not reflected"

    assert (
        "uq_blobs_bucket_sha256" in facts.unique_constraint_names
        or "ix_blobs_bucket_sha256" in facts.unique_index_names
    ), "expected a unique guarantee on blobs(bucket_id, sha256)"

"""Alembic migration environment for tgobjects3.

The gateway runs migrations at startup (Requirement 1.4) against an async
asyncpg engine. This environment therefore:

* reads the connection string from the ``DATABASE_URL`` environment variable
  rather than ``alembic.ini`` so that credentials never live in a committed
  file;
* normalises the URL to the ``postgresql+asyncpg`` driver so the same value
  works whether an operator supplies a bare ``postgresql://`` URL or an
  explicit async one;
* targets :data:`gateway.app.db.models.Base.metadata` so ``--autogenerate``
  diffs against the ORM models defined in task 4.1;
* runs the online migration through an :class:`~sqlalchemy.ext.asyncio.AsyncEngine`.

Offline mode is still supported (``alembic upgrade --sql``) for operators who
want to inspect the generated SQL without a live database.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context

# ``gateway.app.db.models`` holds the declarative Base with every table.
from gateway.app.db.models import Base
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# Alembic Config object, providing access to values within alembic.ini.
config = context.config

# Configure Python logging from the alembic.ini stanzas if present.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Target metadata for 'autogenerate' support.
target_metadata = Base.metadata


def _database_url() -> str:
    """Return the asyncpg database URL from the environment.

    DATABASE_URL is a required, env-only setting (see the design configuration
    table). We normalise common ``postgresql://`` / ``postgres://`` forms to the
    ``postgresql+asyncpg`` driver so the async engine is always used.
    """
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set; Alembic migrations require it "
            "(it is an env-only, required setting)."
        )
    if url.startswith("postgresql+asyncpg://"):
        return url
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://") :]
    if url.startswith("postgres://"):
        return "postgresql+asyncpg://" + url[len("postgres://") :]
    return url


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL without a DBAPI connection)."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Configure the context on a live connection and run the migrations."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Create an async engine and run migrations within an async context."""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _database_url()

    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode against the async engine."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

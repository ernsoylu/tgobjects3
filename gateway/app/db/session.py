"""Async database session factory and gateway startup/shutdown lifecycle.

This module owns the SQLAlchemy 2 async engine and the single
:class:`~sqlalchemy.ext.asyncio.async_sessionmaker` the rest of the gateway
uses to open :class:`~sqlalchemy.ext.asyncio.AsyncSession` instances. It also
drives the ordered startup sequence mandated by Requirement 1:

1. **Connect (Requirement 1.5).** When PostgreSQL is not yet available at
   startup, retry the connection up to 10 times at 3-second intervals. If all
   10 attempts fail, raise :class:`DatabaseUnreachableError` so the process can
   exit non-zero with a DB-unreachable message.
2. **Migrate (Requirements 1.4, 1.6).** Apply all Alembic migrations
   (``alembic upgrade head``) before the gateway accepts any request. If a
   migration fails, raise :class:`MigrationError` identifying the failure and
   halt — no requests are served.
3. **Recover.** Run the staging crash-recovery scan (owned by
   ``gateway/app/storage/staging.py``, task 15.1) so orphaned staging files are
   quarantined and interrupted uploads are re-enqueued before traffic flows.

Configuration (``db_url`` and the pool parameters) is passed in explicitly
rather than read from a module-level ``Settings`` singleton. ``gateway/app/config.py``
(task 14.1) owns configuration; this module only *consumes* values, which keeps
the two tasks free of an import-time ordering dependency.

The connection pool is sized to the Requirement's min=2 / max=10 / 30 s-timeout
semantics. SQLAlchemy's :class:`~sqlalchemy.pool.QueuePool` keeps ``pool_size``
connections open and allows up to ``max_overflow`` extra ones under load, so a
``pool_size`` of 2 with ``max_overflow`` of 8 caps the total at 10 while keeping
a warm floor of 2. ``pool_timeout`` is the number of seconds a caller waits for
a free connection before erroring.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.sql import text

logger = logging.getLogger(__name__)

# --- Connection-pool defaults (Requirement: min=2, max=10, timeout=30 s). ----
#
# QueuePool keeps ``POOL_MIN_SIZE`` persistent connections and permits
# ``POOL_MAX_SIZE - POOL_MIN_SIZE`` additional overflow connections, so the hard
# ceiling on concurrent connections is ``POOL_MAX_SIZE``.
POOL_MIN_SIZE = 2
POOL_MAX_SIZE = 10
POOL_TIMEOUT_SECONDS = 30.0

# --- Startup connection-retry policy (Requirement 1.5). ----------------------
DB_CONNECT_MAX_ATTEMPTS = 10
DB_CONNECT_RETRY_INTERVAL_SECONDS = 3.0

# Repository-root ``alembic.ini``. ``session.py`` lives at
# ``gateway/app/db/session.py``; the config file is three parents up.
_ALEMBIC_INI = Path(__file__).resolve().parents[3] / "alembic.ini"

# A staging crash-recovery scan: given an open AsyncSession, quarantine orphaned
# staging files and re-enqueue interrupted uploads. Defined here as a type alias
# so startup can accept it as an injected dependency.
StagingScan = Callable[[AsyncSession], Awaitable[None]]


class DatabaseUnreachableError(RuntimeError):
    """PostgreSQL could not be reached within the startup retry budget.

    Raised after :data:`DB_CONNECT_MAX_ATTEMPTS` failed connection attempts
    (Requirement 1.5). Callers should treat this as fatal and exit non-zero.
    """


class MigrationError(RuntimeError):
    """An Alembic migration failed during startup (Requirement 1.6).

    The gateway must halt immediately without accepting requests. The message
    identifies the migration step that failed.
    """


def _normalise_asyncpg_url(db_url: str) -> str:
    """Return ``db_url`` normalised to the ``postgresql+asyncpg`` driver.

    Operators may supply a bare ``postgresql://`` / ``postgres://`` URL. The
    async engine requires the asyncpg driver, so we rewrite the scheme while
    leaving an already-explicit async URL untouched.
    """
    if db_url.startswith("postgresql+asyncpg://"):
        return db_url
    if db_url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + db_url[len("postgresql://") :]
    if db_url.startswith("postgres://"):
        return "postgresql+asyncpg://" + db_url[len("postgres://") :]
    return db_url


def _build_engine(
    db_url: str,
    *,
    pool_min_size: int,
    pool_max_size: int,
    pool_timeout: float,
) -> AsyncEngine:
    """Create the async engine with the project's QueuePool sizing.

    ``pool_size`` is the warm floor of persistent connections; ``max_overflow``
    is how many additional connections may be opened on demand. Their sum is the
    hard ceiling, so we derive ``max_overflow`` from the requested maximum.
    """
    max_overflow = max(pool_max_size - pool_min_size, 0)
    return create_async_engine(
        _normalise_asyncpg_url(db_url),
        pool_size=pool_min_size,
        max_overflow=max_overflow,
        pool_timeout=pool_timeout,
        pool_pre_ping=True,
        future=True,
    )


class AsyncSessionFactory:
    """Owns the async engine and hands out :class:`AsyncSession` instances.

    One instance lives per gateway process. Construct it with
    :meth:`create`, which also wires the pool. Use :meth:`session` as an async
    context manager to borrow a session that is committed on success and rolled
    back on error, or call the instance directly to get a raw session when you
    need to manage the transaction yourself.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        sessionmaker: async_sessionmaker[AsyncSession],
    ) -> None:
        self._engine = engine
        self._sessionmaker = sessionmaker

    @classmethod
    def create(
        cls,
        db_url: str,
        *,
        pool_min_size: int = POOL_MIN_SIZE,
        pool_max_size: int = POOL_MAX_SIZE,
        pool_timeout: float = POOL_TIMEOUT_SECONDS,
    ) -> AsyncSessionFactory:
        """Build a factory and its underlying connection pool.

        Creating the factory does not open any connection; the pool fills lazily
        on first use. Call :func:`startup` for the full connect/migrate/recover
        sequence that must complete before serving requests.
        """
        engine = _build_engine(
            db_url,
            pool_min_size=pool_min_size,
            pool_max_size=pool_max_size,
            pool_timeout=pool_timeout,
        )
        sessionmaker = async_sessionmaker(
            bind=engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
        return cls(engine, sessionmaker)

    @property
    def engine(self) -> AsyncEngine:
        """The underlying async engine (exposed for health checks and tests)."""
        return self._engine

    @property
    def sessionmaker(self) -> async_sessionmaker[AsyncSession]:
        """The underlying ``async_sessionmaker``.

        Exposed for collaborators (the Upload Worker, the PutObject/multipart
        handlers) whose constructors are typed against the bare
        :class:`~sqlalchemy.ext.asyncio.async_sessionmaker` rather than this
        factory wrapper. They open their own transactions, which is exactly what
        the sessionmaker hands out.
        """
        return self._sessionmaker

    def __call__(self) -> AsyncSession:
        """Open a new session without managing its transaction.

        The caller owns the returned session and must close it. Prefer
        :meth:`session` for request-scoped work.
        """
        return self._sessionmaker()

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Yield a session, committing on success and rolling back on error."""
        async with self._sessionmaker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def dispose(self) -> None:
        """Close every pooled connection and release the pool."""
        await self._engine.dispose()


async def wait_for_database(
    engine: AsyncEngine,
    *,
    max_attempts: int = DB_CONNECT_MAX_ATTEMPTS,
    retry_interval: float = DB_CONNECT_RETRY_INTERVAL_SECONDS,
) -> None:
    """Block until PostgreSQL answers, retrying per Requirement 1.5.

    Attempts a trivial ``SELECT 1`` up to ``max_attempts`` times, sleeping
    ``retry_interval`` seconds between failures. Raises
    :class:`DatabaseUnreachableError` once the budget is exhausted so the caller
    can exit non-zero with a DB-unreachable message.
    """
    last_error: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            logger.info("Connected to PostgreSQL on attempt %d.", attempt)
            return
        except (SQLAlchemyError, OSError) as exc:
            last_error = exc
            logger.warning(
                "PostgreSQL not reachable (attempt %d/%d): %s",
                attempt,
                max_attempts,
                exc,
            )
            if attempt < max_attempts:
                await asyncio.sleep(retry_interval)

    raise DatabaseUnreachableError(
        f"PostgreSQL is unreachable after {max_attempts} attempts "
        f"at {retry_interval:g}s intervals"
    ) from last_error


def _make_alembic_config(db_url: str) -> Config:
    """Build an Alembic :class:`Config` pointed at the repo's ``alembic.ini``.

    The migration env reads ``DATABASE_URL`` from the environment, but we also
    set ``sqlalchemy.url`` on the config so a caller-supplied URL is honoured
    without mutating process environment variables.
    """
    config = Config(str(_ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", _normalise_asyncpg_url(db_url))
    return config


def _run_migrations_sync(db_url: str) -> None:
    """Run ``alembic upgrade head`` synchronously.

    Alembic's env.py drives its own ``asyncio.run`` for the online async engine,
    so this must execute off the running event loop (via ``asyncio.to_thread``).
    """
    config = _make_alembic_config(db_url)
    command.upgrade(config, "head")


async def run_migrations(db_url: str) -> None:
    """Apply all Alembic migrations before serving requests.

    Satisfies Requirement 1.4 (migrate on startup) and 1.6 (on failure, halt
    and identify the failure). Any Alembic/SQLAlchemy error is wrapped in
    :class:`MigrationError`.
    """
    logger.info("Applying Alembic migrations (upgrade head).")
    try:
        await asyncio.to_thread(_run_migrations_sync, db_url)
    except Exception as exc:  # noqa: BLE001 - re-raised as a typed startup error
        raise MigrationError(f"Alembic migration failed: {exc}") from exc
    logger.info("Alembic migrations applied; schema is at head.")


async def startup(
    db_url: str,
    *,
    pool_min_size: int = POOL_MIN_SIZE,
    pool_max_size: int = POOL_MAX_SIZE,
    pool_timeout: float = POOL_TIMEOUT_SECONDS,
    max_connect_attempts: int = DB_CONNECT_MAX_ATTEMPTS,
    connect_retry_interval: float = DB_CONNECT_RETRY_INTERVAL_SECONDS,
    staging_scan: StagingScan | None = None,
) -> AsyncSessionFactory:
    """Run the full database startup sequence and return the session factory.

    Steps, in order:

    1. Build the engine/pool and wait for PostgreSQL, retrying per
       Requirement 1.5. Raises :class:`DatabaseUnreachableError` on exhaustion.
    2. Apply Alembic migrations (Requirements 1.4/1.6). Raises
       :class:`MigrationError` on failure.
    3. Run the staging crash-recovery scan so orphaned staging files are
       quarantined and interrupted uploads re-enqueued before traffic flows.

    ``staging_scan`` is injected to avoid a hard import-time dependency on
    ``gateway/app/storage/staging.py`` (task 15.1). When ``None``, this function
    attempts to import that module's scan defensively and skips the step if it
    is not yet available.

    On any failure after the engine is created, the pool is disposed before the
    error propagates so a failed startup leaves no dangling connections.
    """
    factory = AsyncSessionFactory.create(
        db_url,
        pool_min_size=pool_min_size,
        pool_max_size=pool_max_size,
        pool_timeout=pool_timeout,
    )

    try:
        await wait_for_database(
            factory.engine,
            max_attempts=max_connect_attempts,
            retry_interval=connect_retry_interval,
        )
        await run_migrations(db_url)
        await _run_staging_recovery(factory, staging_scan)
    except BaseException:
        # Startup failed; do not leak the pool we just opened.
        await factory.dispose()
        raise

    logger.info("Database startup complete; gateway may accept requests.")
    return factory


async def _run_staging_recovery(
    factory: AsyncSessionFactory,
    staging_scan: StagingScan | None,
) -> None:
    """Run the staging crash-recovery scan with an open session.

    Uses the injected ``staging_scan`` when provided; otherwise resolves it
    defensively from ``gateway.app.storage.staging`` so this module composes
    even before task 15.1 settles that module's public surface. If no scan is
    available, the step is skipped with a warning rather than failing startup.
    """
    scan = staging_scan or _resolve_staging_scan()
    if scan is None:
        logger.warning(
            "No staging crash-recovery scan available; skipping staging recovery."
        )
        return

    logger.info("Running staging crash-recovery scan.")
    async with factory.session() as session:
        await scan(session)
    logger.info("Staging crash-recovery scan complete.")


def _resolve_staging_scan() -> StagingScan | None:
    """Best-effort lookup of the staging scan owned by task 15.1.

    Imported lazily and inside a ``try`` so this module never fails to import
    when ``gateway/app/storage/staging.py`` is absent or its surface is still in
    flux. Recognises a few likely public names for the scan coroutine.
    """
    try:
        from gateway.app.storage import staging
    except ImportError:
        return None

    for name in ("crash_recovery_scan", "startup_recovery_scan", "recover_staging"):
        candidate = getattr(staging, name, None)
        if callable(candidate):
            return candidate  # type: ignore[no-any-return]
    return None


async def shutdown(factory: AsyncSessionFactory) -> None:
    """Gracefully close all database connections.

    Disposes the engine's pool so pending checkouts are returned and sockets are
    closed. Safe to call once per :func:`startup`.
    """
    logger.info("Closing database connections.")
    await factory.dispose()
    logger.info("Database connections closed.")


__all__ = [
    "AsyncSessionFactory",
    "DatabaseUnreachableError",
    "MigrationError",
    "StagingScan",
    "POOL_MIN_SIZE",
    "POOL_MAX_SIZE",
    "POOL_TIMEOUT_SECONDS",
    "DB_CONNECT_MAX_ATTEMPTS",
    "DB_CONNECT_RETRY_INTERVAL_SECONDS",
    "wait_for_database",
    "run_migrations",
    "startup",
    "shutdown",
]

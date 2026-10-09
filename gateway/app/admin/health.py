"""Liveness and readiness health endpoints on the admin port.

This module builds the two observability health endpoints the gateway exposes on
the admin port (requirement 18.2): a *liveness* probe at ``GET /health/live`` and
a *readiness* probe at ``GET /health/ready``. Both are mounted by the application
wiring (task 32.1); this module only constructs the :class:`fastapi.APIRouter`
and the dependency-check seams, following the factory-plus-injected-dependencies
style used by the S3 router (``gateway/app/s3/router.py``).

Semantics
---------
- **Liveness** (``/health/live``, requirement 18.7): answers "is the process up
  and able to handle requests?". A process that is serving the ASGI app is live
  by definition, so the endpoint returns ``200`` with a small JSON body
  ``{"status": "live"}``. The design reserves ``503`` for a non-recoverable
  error state; liveness performs no dependency I/O, because a probe that failed
  on a transient dependency outage would cause an orchestrator to kill and
  restart an otherwise healthy process.
- **Readiness** (``/health/ready``, requirements 18.3, 18.4): answers "can the
  gateway actually serve traffic *right now*?". It probes the two dependencies
  the request path needs — PostgreSQL (the index/cache) and the single Telegram
  MTProto session (durable truth). When every dependency is usable it returns
  ``200`` with ``{"status": "ready"}``; when one or more are unavailable it
  returns ``503`` with ``{"unhealthy": [...]}`` naming the failed dependencies.
  Per the task's body contract the names are the fixed strings ``"postgresql"``
  (requirement 18.3) and ``"telegram"`` (requirement 18.4).

Dependencies are injected rather than imported as singletons: the readiness
check receives the :class:`~gateway.app.db.session.AsyncSessionFactory` and the
:class:`~gateway.app.telegram.port.TelegramPort` so the whole surface can be
driven by fakes in tests and so this module never reaches past its boundary.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.sql import text

from gateway.app.db.session import AsyncSessionFactory
from gateway.app.telegram.errors import TelegramError
from gateway.app.telegram.port import TelegramPort

logger = logging.getLogger(__name__)

__all__ = [
    "DEPENDENCY_POSTGRESQL",
    "DEPENDENCY_TELEGRAM",
    "check_postgresql",
    "check_telegram",
    "create_health_router",
]

#: Fixed dependency names reported in the ``/health/ready`` ``unhealthy`` body.
#: These string values are part of the endpoint's contract (requirements 18.3,
#: 18.4) — tests and operators match on them, so they must not drift.
DEPENDENCY_POSTGRESQL = "postgresql"
DEPENDENCY_TELEGRAM = "telegram"


async def check_postgresql(session_factory: AsyncSessionFactory) -> bool:
    """Return whether PostgreSQL is reachable (requirement 18.3).

    Borrows a session from ``session_factory`` and runs a trivial ``SELECT 1``.
    Any SQLAlchemy or OS-level connection error means the dependency is
    unavailable, so the probe returns ``False`` rather than letting the error
    propagate — readiness must downgrade to ``503``, not raise a ``500``.

    Args:
        session_factory: The async session factory owning the connection pool.

    Returns:
        ``True`` when the ``SELECT 1`` succeeds, ``False`` otherwise.
    """
    try:
        async with session_factory.session() as session:
            await session.execute(text("SELECT 1"))
    except (SQLAlchemyError, OSError) as exc:
        logger.warning("Readiness probe: PostgreSQL unavailable: %s", exc)
        return False
    return True


async def check_telegram(telegram: TelegramPort) -> bool:
    """Return whether the Telegram session is connected (requirement 18.4).

    Consults the port's :meth:`~gateway.app.telegram.port.TelegramPort.is_connected`
    seam. A typed Telegram boundary error (e.g. the session is mid-reconnection)
    is treated as "not connected" and swallowed so readiness degrades to a
    ``503`` instead of surfacing a ``500``.

    Args:
        telegram: The gateway's single Telegram port.

    Returns:
        ``True`` when the session reports itself connected, ``False`` otherwise.
    """
    try:
        return await telegram.is_connected()
    except TelegramError as exc:
        logger.warning("Readiness probe: Telegram unavailable: %s", exc)
        return False


def create_health_router(
    *,
    session_factory: AsyncSessionFactory,
    telegram: TelegramPort,
) -> APIRouter:
    """Build the health :class:`fastapi.APIRouter` for the admin port.

    Registers ``GET /health/live`` and ``GET /health/ready`` (requirement 18.2).
    Dependencies are injected so the router is self-contained and test-driveable
    with fakes; the application wiring (task 32.1) mounts the returned router on
    the admin port.

    Args:
        session_factory: The async session factory, probed by the readiness
            check for PostgreSQL reachability.
        telegram: The Telegram port, probed by the readiness check for session
            connectivity.

    Returns:
        The configured router, ready to mount.
    """
    router = APIRouter()

    @router.get("/health/live", include_in_schema=False)
    async def health_live() -> JSONResponse:
        """Liveness probe: ``200`` while the process can serve requests (18.7).

        Performs no dependency I/O; a running process serving this handler is
        live by definition.
        """
        return JSONResponse(status_code=200, content={"status": "live"})

    @router.get("/health/ready", include_in_schema=False)
    async def health_ready() -> JSONResponse:
        """Readiness probe: ``200`` when ready, ``503`` naming failures (18.3/18.4).

        Probes PostgreSQL and the Telegram session. On full readiness returns
        ``{"status": "ready"}``; otherwise returns ``503`` with
        ``{"unhealthy": [...]}`` listing the unavailable dependencies.
        """
        unhealthy: list[str] = []
        if not await check_postgresql(session_factory):
            unhealthy.append(DEPENDENCY_POSTGRESQL)
        if not await check_telegram(telegram):
            unhealthy.append(DEPENDENCY_TELEGRAM)

        if unhealthy:
            return JSONResponse(status_code=503, content={"unhealthy": unhealthy})
        return JSONResponse(status_code=200, content={"status": "ready"})

    return router

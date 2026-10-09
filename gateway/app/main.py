"""Application entrypoint — load config, wire collaborators, serve the APIs.

This module is the composition root of the gateway (design "System
Architecture": "a single Python process under uvicorn/uvloop"). It owns no
business logic; its job is to construct every collaborator built by the sibling
tasks, connect them, and expose the two HTTP surfaces the design mandates:

* the **S3 API** on ``S3_PORT`` (default 9000), and
* the **Admin API** plus health/metrics on ``ADMIN_PORT`` (default 8080).

Startup contract (Requirement 1)
--------------------------------
The ordered startup sequence and its failure modes come straight from
Requirement 1 and the design's "Startup Failures" section:

* **1.3** — configuration is loaded with fail-fast validation. Missing required
  variables (``TGOS3_MASTER_KEY`` / ``DATABASE_URL``) are each printed on their
  own stderr line and the process exits non-zero *before any port is bound*.
  :func:`gateway.app.config.load_settings` performs this and raises
  :class:`SystemExit`; :func:`main` lets that propagate before the servers are
  created.
* **1.5** — PostgreSQL is connected with up to 10 retries at 3-second intervals;
  exhaustion exits non-zero with a DB-unreachable message.
* **1.4 / 1.6** — all Alembic migrations are applied before any request is
  served; a failed migration halts startup and identifies the failure.
  Both are implemented by :func:`gateway.app.db.session.startup`, which this
  module drives inside the FastAPI ``lifespan``.
* **1.1 / 1.2** — the process exposes the S3 and admin ports a single
  ``docker compose up`` wires; the Dockerfile/compose files are tasks 32.2/32.3.

Lifespan (startup / graceful shutdown)
--------------------------------------
Both FastAPI apps share one :class:`GatewayContext`, assembled once by
:func:`build_context` and driven by :func:`lifespan`:

* **startup** — run the database connect/migrate/staging-recovery sequence,
  connect the single Telegram session (Requirement 3.1), and launch the
  background tasks: ``UPLOAD_CONCURRENCY`` Upload Workers, the cache evictor,
  and — when their modules exist — the checkpoint and GC workers.
* **shutdown** — stop the background workers, stop the Telegram session, and
  dispose the database pool, draining in-flight work gracefully.

Defensive wiring
----------------
Several collaborators land in later tasks (the admin router 41.x, the
health/metrics endpoints 33.x, the checkpoint/GC workers). This module imports
each one inside ``try/except ImportError`` and wires only what exists, leaving a
clear hook (and a logged note) for the rest. The gateway is therefore runnable
end-to-end from this task while each later task lights up its own surface
without touching this file.

Telegram session seam
---------------------
The Telegram credentials (API id/hash and the authenticated session) are
collected by the setup wizard (Requirement 2) and are *not* part of
:class:`~gateway.app.config.Settings` — the design's Configuration table carries
no Telegram variables. Until the credential store lands, :func:`build_context`
accepts an injectable ``telegram_factory`` that produces the single
:class:`~gateway.app.telegram.port.TelegramPort`. When none is supplied the
context wires a :class:`~gateway.app.telegram.session.SessionManager` that is
left disconnected, so every Telegram-dependent request returns HTTP 503
(Requirement 3.3) until setup completes — which is the correct behaviour for a
freshly-deployed, not-yet-configured gateway.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import logging
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, Response
from sqlalchemy import select

from gateway.app.cache.eviction import CacheEvictor
from gateway.app.cache.store import RecordCache
from gateway.app.config import Settings, load_settings
from gateway.app.db import session as db_session
from gateway.app.db.models import AccessKey, Generation
from gateway.app.db.session import AsyncSessionFactory
from gateway.app.format.key_wrap import KeyUnwrapError, derive_root_key, unwrap
from gateway.app.jobs.upload_worker import UploadWorker
from gateway.app.s3 import sigv4
from gateway.app.s3.operations import bucket_ops
from gateway.app.s3.operations.copy_object import CopyObjectHandler
from gateway.app.s3.operations.delete_object import DeleteObjectHandler
from gateway.app.s3.operations.delete_objects import DeleteObjectsHandler
from gateway.app.s3.operations.get_object import GetObjectHandler
from gateway.app.s3.operations.head_object import HeadObjectHandler
from gateway.app.s3.operations.list_objects import ListObjectsHandler
from gateway.app.s3.operations.multipart import MultipartHandler
from gateway.app.s3.operations.put_object import (
    CommitOutcome,
    PutObjectHandler,
)
from gateway.app.s3.router import (
    S3HandlerContext,
    S3Handlers,
    S3Operation,
    SigV4Authenticator,
    create_s3_router,
)
from gateway.app.storage import generations as generations_storage
from gateway.app.storage import staging as staging_mod
from gateway.app.storage.staging import StagingReservation
from gateway.app.telegram.port import SessionState, TelegramMessage
from gateway.app.telegram.session import ManagedTelegramClient, SessionManager

logger = logging.getLogger(__name__)

__all__ = [
    "GatewayContext",
    "DbSecretProvider",
    "WorkerCommitDriver",
    "DEFAULT_STAGING_DIR",
    "DEFAULT_CACHE_DIR",
    "SYNC_COMMIT_DEADLINE_SECONDS",
    "build_context",
    "build_s3_app",
    "build_admin_app",
    "lifespan",
    "main",
    "run",
]

# --- Filesystem layout --------------------------------------------------------
# The ``/data`` volume (compose task 32.3) backs both staging and the record
# cache. These are the on-disk roots the storage/cache components default to;
# the setup wizard may later override them per deployment.
DEFAULT_DATA_DIR = Path("/data")
#: Root directory under ``/data`` where PutObject/multipart stage bodies.
DEFAULT_STAGING_DIR = DEFAULT_DATA_DIR / "staging"
#: Root directory under ``/data`` for the on-disk record cache.
DEFAULT_CACHE_DIR = DEFAULT_DATA_DIR / "cache"

# --- Sync-PUT commit boundary (Requirement 6.14) ------------------------------
#: How long a sync PutObject/CompleteMultipartUpload waits for the Upload Worker
#: to drive its generation to COMMITTED before returning HTTP 503. Mirrors the
#: 30-second Telegram-unreachable boundary in the generation state machine.
SYNC_COMMIT_DEADLINE_SECONDS = 30.0

#: How often :class:`WorkerCommitDriver` re-checks the target generation's state
#: while nudging the Upload Worker, in seconds.
_COMMIT_POLL_INTERVAL_SECONDS = 0.1


# ---------------------------------------------------------------------------
# Secret provider (SigV4 ← access_keys table)
# ---------------------------------------------------------------------------

#: Decrypts a stored ``access_keys.secret_enc`` value into the plaintext SigV4
#: secret, given the derived 32-byte root key. Injected so the credential-store
#: task (41.x) can finalise the on-disk encoding without changing this module.
SecretDecryptor = Callable[[bytes, str], str]


def _default_secret_decryptor(root_key: bytes, secret_enc: str) -> str:
    """Decrypt ``secret_enc`` with AES Key Wrap under the root key.

    The access-key creation path (setup wizard / admin, task 41.x) that writes
    ``secret_enc`` is not yet implemented, so its on-disk encoding is not frozen.
    This default assumes the natural match to the project's existing key
    primitives: the secret is wrapped with AES Key Wrap (RFC 3394) under the
    root key and stored base64-encoded, mirroring how bucket KEKs and blob keys
    are wrapped elsewhere (:mod:`gateway.app.format.key_wrap`). It is isolated as
    the :data:`SecretDecryptor` seam so task 41.x can replace it without touching
    the provider or the router wiring.

    Raises:
        ValueError: If the stored value is not valid base64 or fails the AES Key
            Wrap integrity check (wrong key or corrupted ciphertext).
    """
    try:
        wrapped = base64.b64decode(secret_enc, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("secret_enc is not valid base64") from exc
    try:
        plaintext = unwrap(root_key, wrapped)
    except KeyUnwrapError as exc:
        raise ValueError("secret_enc failed its integrity check") from exc
    return plaintext.decode("utf-8")


class DbSecretProvider:
    """A :class:`~gateway.app.s3.sigv4.SecretProvider` backed by ``access_keys``.

    Resolves an access-key id to the plaintext SigV4 secret the signature
    verifiers need, reading the ``access_keys`` table (Requirement 17) through
    the shared session factory and decrypting ``secret_enc`` with the injected
    :data:`SecretDecryptor`. A missing key, a disabled key, or an undecryptable
    secret all resolve to ``None``, which the verifiers treat as an
    authentication failure.
    """

    def __init__(
        self,
        session_factory: AsyncSessionFactory,
        root_key: bytes,
        *,
        decryptor: SecretDecryptor = _default_secret_decryptor,
    ) -> None:
        """Capture the index session factory, root key, and decryptor seam.

        Args:
            session_factory: Opens short read sessions against ``access_keys``.
            root_key: The 32-byte root key derived from ``TGOS3_MASTER_KEY``,
                used to decrypt each stored secret.
            decryptor: Turns a ``(root_key, secret_enc)`` pair into the plaintext
                secret; defaults to :func:`_default_secret_decryptor`.
        """
        self._session_factory = session_factory
        self._root_key = root_key
        self._decryptor = decryptor

    async def __call__(self, access_key_id: str) -> sigv4.StoredSecret | None:
        """Return the stored secret for ``access_key_id`` or ``None``.

        Looks the key up by id; an unknown key yields ``None``. A row that fails
        to decrypt is logged and treated as unknown rather than raising, so a
        corrupted credential can never crash the auth path.
        """
        async with self._session_factory.session() as session:
            row = (
                await session.execute(
                    select(AccessKey).where(AccessKey.access_key_id == access_key_id)
                )
            ).scalar_one_or_none()

        if row is None:
            return None
        if row.disabled:
            return sigv4.StoredSecret(access_key_id=access_key_id, disabled=True)

        try:
            secret = self._decryptor(self._root_key, row.secret_enc)
        except ValueError:
            logger.warning(
                "access key %s has an undecryptable secret; treating as unknown",
                access_key_id,
            )
            return None
        return sigv4.StoredSecret(access_key_id=access_key_id, secret=secret)


# ---------------------------------------------------------------------------
# Commit driver (sync PUT ← Upload Worker)
# ---------------------------------------------------------------------------


class WorkerCommitDriver:
    """Drive a STAGED generation to its terminal state via the Upload Worker.

    Implements the :class:`~gateway.app.s3.operations.put_object.CommitDriver`
    seam a sync PutObject / CompleteMultipartUpload needs (Requirements
    6.13/6.14). Rather than owning a second upload code path, it nudges the
    shared :class:`~gateway.app.jobs.upload_worker.UploadWorker` — whose
    ``run_once`` claims and processes one due job via ``SKIP LOCKED`` — and polls
    the target generation's state until it reaches a terminal state or the
    30-second Telegram-unreachable boundary elapses.

    A :class:`CommitOutcome` with ``timed_out=True`` is returned when the
    deadline passes before the generation commits (the handler maps that to HTTP
    503 and the STAGED generation is retained for retry); a terminal non-commit
    state (notably FAILED) returns ``committed=False`` with the state recorded.
    """

    def __init__(
        self,
        session_factory: AsyncSessionFactory,
        worker: UploadWorker,
        *,
        deadline_seconds: float = SYNC_COMMIT_DEADLINE_SECONDS,
        poll_interval_seconds: float = _COMMIT_POLL_INTERVAL_SECONDS,
    ) -> None:
        """Capture the index factory, the worker to nudge, and the timing policy.

        Args:
            session_factory: Opens short read sessions to poll the generation.
            worker: The shared Upload Worker; ``run_once`` is pumped until the
                target generation reaches a terminal state.
            deadline_seconds: Wall-clock budget before giving up with
                ``timed_out=True`` (Requirement 6.14).
            poll_interval_seconds: Idle wait between state polls when no due job
                was processed on an iteration.
        """
        self._session_factory = session_factory
        self._worker = worker
        self._deadline = deadline_seconds
        self._poll_interval = poll_interval_seconds

    async def drive_to_commit(self, generation_id: uuid.UUID) -> CommitOutcome:
        """Pump the worker until ``generation_id`` is terminal or the deadline hits.

        Each iteration first checks the generation's current state; once it is
        COMMITTED the committed ETag is returned, and any other terminal state
        (FAILED / DELETED / SUPERSEDED) ends the loop as a non-commit. Otherwise
        the worker is nudged once (processing at most one due job) and, if there
        was nothing to do, the loop briefly sleeps before re-checking. The whole
        loop is bounded by ``deadline_seconds`` so a stuck or unreachable
        Telegram surfaces as a timeout rather than blocking the request forever.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._deadline

        while True:
            state, etag = await self._generation_state(generation_id)
            if state == generations_storage.STATE_COMMITTED:
                return CommitOutcome(committed=True, state=state, etag=etag)
            if state in _TERMINAL_NON_COMMIT_STATES:
                return CommitOutcome(committed=False, state=state, etag=etag)

            if loop.time() >= deadline:
                return CommitOutcome(
                    committed=False, state=state, etag=etag, timed_out=True
                )

            processed = await self._worker.run_once()
            if not processed:
                await asyncio.sleep(self._poll_interval)

    async def _generation_state(
        self, generation_id: uuid.UUID
    ) -> tuple[str, str | None]:
        """Return the generation's current ``(state, etag)``.

        A generation that has vanished (which should not happen for a just-staged
        write) is reported as FAILED so the driver ends rather than spinning.
        """
        async with self._session_factory.session() as session:
            row = await session.get(Generation, generation_id)
            if row is None:
                return generations_storage.STATE_FAILED, None
            return row.state, row.etag


#: Terminal generation states other than COMMITTED; reaching one ends a sync
#: wait as a non-commit (the handler maps it to HTTP 500).
_TERMINAL_NON_COMMIT_STATES = frozenset(
    {
        generations_storage.STATE_FAILED,
        generations_storage.STATE_DELETED,
        generations_storage.STATE_SUPERSEDED,
    }
)


# ---------------------------------------------------------------------------
# PUT dispatch (CopyObject shares the PUT_OBJECT slot)
# ---------------------------------------------------------------------------

#: The header that turns a ``PUT /{bucket}/{key}`` into a CopyObject.
_HDR_COPY_SOURCE = "x-amz-copy-source"


class _PutDispatcher:
    """Route a ``PUT /{bucket}/{key}`` to CopyObject or PutObject.

    The router classifies every object-level ``PUT`` without multipart query
    parameters as :data:`S3Operation.PUT_OBJECT`, regardless of the
    ``x-amz-copy-source`` header (see ``copy_object.py``'s wiring note). This
    dispatcher — registered on the ``PUT_OBJECT`` slot — inspects that header and
    delegates to :class:`CopyObjectHandler` when it is present, falling through
    to :class:`PutObjectHandler` otherwise.
    """

    def __init__(
        self, put_handler: PutObjectHandler, copy_handler: CopyObjectHandler
    ) -> None:
        """Capture the two handlers this dispatcher chooses between."""
        self._put = put_handler
        self._copy = copy_handler

    async def __call__(self, context: S3HandlerContext) -> Response:
        """Delegate to CopyObject when ``x-amz-copy-source`` is set, else PutObject."""
        copy_source = context.request.headers.get(_HDR_COPY_SOURCE)
        if copy_source is not None and copy_source.strip():
            return await self._copy(context)
        return await self._put(context)


# ---------------------------------------------------------------------------
# Gateway context
# ---------------------------------------------------------------------------


@dataclass
class GatewayContext:
    """The assembled, process-wide collaborator graph shared by both apps.

    Built once by :func:`build_context` and driven by :func:`lifespan`. Holds the
    long-lived singletons (settings, the database session factory, the single
    Telegram session, the record cache) and the handles for the background tasks
    the lifespan starts and stops.
    """

    settings: Settings
    session_factory: AsyncSessionFactory
    telegram: SessionManager
    staging: StagingReservation
    cache: RecordCache
    evictor: CacheEvictor
    workers: list[UploadWorker]
    staging_dir: Path
    cache_dir: Path
    #: Background task handles started during lifespan startup.
    _tasks: list[asyncio.Task[None]] = field(default_factory=list)
    #: Guards the lifespan so only the first app to enter runs startup/shutdown.
    _started: bool = False


# ---------------------------------------------------------------------------
# Context construction
# ---------------------------------------------------------------------------

#: Factory that produces the single managed Telegram client. Injected so the
#: setup-wizard credential store (task 41.x) can supply a configured Telethon
#: adapter; defaults to ``None`` (no credentials yet → disconnected session).
TelegramFactory = Callable[[Settings], ManagedTelegramClient]


def build_context(
    settings: Settings,
    *,
    staging_dir: Path = DEFAULT_STAGING_DIR,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    telegram_factory: TelegramFactory | None = None,
) -> GatewayContext:
    """Assemble the collaborator graph from validated ``settings``.

    Constructs the database session factory, the single Telegram session
    manager, the record cache and evictor, and ``UPLOAD_CONCURRENCY`` Upload
    Workers sharing one session factory and Telegram port. No I/O happens here;
    connecting the database and the Telegram session is the lifespan's job.

    Args:
        settings: The validated gateway configuration.
        staging_dir: On-disk staging root (defaults under ``/data``).
        cache_dir: On-disk record-cache root (defaults under ``/data``).
        telegram_factory: Optional factory for the managed Telegram client. When
            ``None`` (no stored credentials yet) a disconnected session is wired
            so Telegram-dependent requests return 503 until setup completes.

    Returns:
        A fully-wired :class:`GatewayContext` ready for :func:`lifespan`.
    """
    session_factory = AsyncSessionFactory.create(settings.DATABASE_URL)

    client: ManagedTelegramClient = (
        telegram_factory(settings)
        if telegram_factory is not None
        else _UnconfiguredTelegramClient()
    )
    telegram = SessionManager(client)

    staging = StagingReservation(settings.STAGING_MAX_SIZE)
    cache = RecordCache(settings, session_factory, cache_root=cache_dir)
    evictor = CacheEvictor(
        settings, session_factory, cache, cache_root=cache_dir
    )

    workers = [
        UploadWorker(
            session_factory.sessionmaker,
            telegram,
            settings.TGOS3_MASTER_KEY,
            upload_concurrency=settings.UPLOAD_CONCURRENCY,
            default_chunk_records=settings.CHUNK_RECORDS,
        )
        for _ in range(settings.UPLOAD_CONCURRENCY)
    ]

    return GatewayContext(
        settings=settings,
        session_factory=session_factory,
        telegram=telegram,
        staging=staging,
        cache=cache,
        evictor=evictor,
        workers=workers,
        staging_dir=Path(staging_dir),
        cache_dir=Path(cache_dir),
    )


class _UnconfiguredTelegramClient:
    """A :class:`ManagedTelegramClient` that never connects.

    The placeholder used until the setup wizard supplies real Telegram
    credentials. ``connect`` succeeds silently but ``is_connected`` stays
    ``False``, so :class:`SessionManager` holds the session in a non-CONNECTED
    state and every Telegram call surfaces as
    :class:`~gateway.app.telegram.errors.TelegramUnavailableError` → HTTP 503
    (Requirement 3.3). No Telethon import happens on this path.
    """

    async def connect(self) -> None:
        """No-op connect; the unconfigured client is never actually usable."""

    async def disconnect(self) -> None:
        """No-op disconnect."""

    async def send_document(
        self, channel_id: int, data: bytes, file_name: str, caption: str
    ) -> int:
        raise _unconfigured()

    async def get_messages(
        self, channel_id: int, ids: list[int]
    ) -> list[TelegramMessage]:
        raise _unconfigured()

    async def download_document(self, channel_id: int, message_id: int) -> bytes:
        raise _unconfigured()

    async def delete_messages(
        self, channel_id: int, message_ids: list[int]
    ) -> None:
        raise _unconfigured()

    async def create_channel(self, title: str) -> int:
        raise _unconfigured()

    async def delete_channel(self, channel_id: int) -> None:
        raise _unconfigured()

    async def get_session_state(self) -> SessionState:
        return SessionState.DISCONNECTED

    async def is_connected(self) -> bool:
        return False


def _unconfigured() -> Exception:
    """Build the error raised when the Telegram session is not configured."""
    from gateway.app.telegram.errors import TelegramUnavailableError

    return TelegramUnavailableError(
        "Telegram session is not configured; complete the setup wizard"
    )


# ---------------------------------------------------------------------------
# S3 handler registration
# ---------------------------------------------------------------------------


def _build_s3_handlers(context: GatewayContext) -> S3Handlers:
    """Construct every operation handler and register it on the registry.

    Fills all bucket-, object-, and multipart-operation slots the router
    dispatches to (Requirements 4, 5, 6, 8–11). The ``PUT_OBJECT`` slot gets the
    :class:`_PutDispatcher` so a ``PUT`` carrying ``x-amz-copy-source`` is routed
    to CopyObject; the single :class:`MultipartHandler` instance is registered on
    all six multipart slots.
    """
    settings = context.settings
    sessions = context.session_factory
    telegram = context.telegram

    commit_driver = WorkerCommitDriver(sessions, context.workers[0])

    handlers = S3Handlers()

    # Bucket operations (Requirement 4).
    handlers.register(
        S3Operation.LIST_BUCKETS, bucket_ops.ListBucketsHandler(sessions)
    )
    handlers.register(
        S3Operation.CREATE_BUCKET,
        bucket_ops.CreateBucketHandler(sessions, telegram),
    )
    handlers.register(
        S3Operation.HEAD_BUCKET, bucket_ops.HeadBucketHandler(sessions)
    )
    handlers.register(
        S3Operation.DELETE_BUCKET,
        bucket_ops.DeleteBucketHandler(sessions, telegram),
    )
    handlers.register(
        S3Operation.GET_BUCKET_LOCATION,
        bucket_ops.GetBucketLocationHandler(sessions),
    )

    # Object listing (Requirement 5) — one handler on both v1 and v2 slots.
    list_objects = ListObjectsHandler(sessions)
    handlers.register(S3Operation.LIST_OBJECTS_V1, list_objects)
    handlers.register(S3Operation.LIST_OBJECTS_V2, list_objects)

    # Object write path: PutObject + CopyObject share the PUT_OBJECT slot.
    put_handler = PutObjectHandler(
        session_factory=sessions.sessionmaker,
        staging=context.staging,
        staging_dir=context.staging_dir,
        commit_driver=commit_driver,
    )
    copy_handler = CopyObjectHandler(sessions, telegram)
    handlers.register(
        S3Operation.PUT_OBJECT, _PutDispatcher(put_handler, copy_handler)
    )

    # Object read / metadata / delete (Requirements 8, 9).
    handlers.register(
        S3Operation.GET_OBJECT,
        GetObjectHandler(sessions, telegram, context.cache, settings),
    )
    handlers.register(S3Operation.HEAD_OBJECT, HeadObjectHandler(sessions))
    handlers.register(
        S3Operation.DELETE_OBJECT, DeleteObjectHandler(sessions, telegram)
    )
    handlers.register(
        S3Operation.DELETE_OBJECTS, DeleteObjectsHandler(sessions, telegram)
    )

    # Multipart (Requirement 10) — one handler on all six slots.
    multipart = MultipartHandler(
        session_factory=sessions,
        staging=context.staging,
        staging_dir=context.staging_dir,
        commit_driver=commit_driver,
    )
    for operation in (
        S3Operation.CREATE_MULTIPART_UPLOAD,
        S3Operation.UPLOAD_PART,
        S3Operation.COMPLETE_MULTIPART_UPLOAD,
        S3Operation.ABORT_MULTIPART_UPLOAD,
        S3Operation.LIST_PARTS,
        S3Operation.LIST_MULTIPART_UPLOADS,
    ):
        handlers.register(operation, multipart)

    return handlers


# ---------------------------------------------------------------------------
# FastAPI app assembly
# ---------------------------------------------------------------------------


def build_s3_app(context: GatewayContext) -> FastAPI:
    """Build the FastAPI app mounted on ``S3_PORT``.

    Registers the S3 catch-all router (:func:`create_s3_router`) with a
    :class:`DbSecretProvider`-backed SigV4 authenticator and the fully-populated
    handler registry. The ``service_host`` used for virtual-hosted-style bucket
    detection defaults to ``localhost``; a deployment behind a known domain can
    override it via the setup wizard without changing this wiring.
    """
    root_key = derive_root_key(context.settings.TGOS3_MASTER_KEY)
    authenticator = SigV4Authenticator(
        DbSecretProvider(context.session_factory, root_key)
    )
    handlers = _build_s3_handlers(context)
    router = create_s3_router(
        settings=context.settings,
        handlers=handlers,
        authenticator=authenticator,
        service_host="localhost",
    )

    app = FastAPI(title="tgobjects3 S3 API", lifespan=lifespan)
    app.state.context = context
    app.include_router(router)
    return app


def build_admin_app(context: GatewayContext) -> FastAPI:
    """Build the FastAPI app mounted on ``ADMIN_PORT``.

    Mounts the admin router (task 41.x) and the health/metrics endpoints (task
    33.x) when their modules exist, importing each defensively so the gateway
    runs before those tasks land. Anything not yet available is logged and left
    as a hook; the app itself always starts so the port binds and liveness can be
    served.
    """
    app = FastAPI(title="tgobjects3 Admin API", lifespan=lifespan)
    app.state.context = context

    _wire_admin_router(app, context)
    _wire_health_endpoints(app, context)
    _wire_metrics_endpoint(app, context)
    return app


def _wire_optional_router(
    app: FastAPI,
    context: GatewayContext,
    *,
    module_name: str,
    factory_name: str,
    label: str,
) -> None:
    """Mount a router from an optional module if present and well-formed.

    Imports ``module_name`` defensively (via :func:`importlib.import_module`, so
    a module that a later task has not created yet raises :class:`ImportError`
    rather than a static type error), looks up ``factory_name``, and includes the
    router it builds. Anything missing is logged as a hook and skipped so the app
    still starts.
    """
    import importlib

    try:
        module = importlib.import_module(module_name)
    except ImportError:
        logger.info("%s not available yet; skipping", label)
        return

    factory = getattr(module, factory_name, None)
    if factory is None:
        logger.info("%s present but exposes no %s", label, factory_name)
        return
    app.include_router(factory(context=context))


def _wire_admin_router(app: FastAPI, context: GatewayContext) -> None:
    """Mount the admin API router, with the setup wizard folded in (tasks 41.x, 42.1).

    The admin router (task 41.x) accepts an optional ``setup_router`` it mounts
    under its own Basic-auth + CSRF dependencies. This wiring builds the setup
    wizard router (task 42.1) and hands it in so ``/admin/setup/*`` is served
    behind the same auth gate as the rest of the admin API. Both modules are
    imported defensively so the admin app still binds its port if either is not
    present; a missing or ill-formed admin factory leaves the app running with no
    admin surface (health/metrics still mount).
    """
    import importlib

    try:
        router_module = importlib.import_module("gateway.app.admin.router")
    except ImportError:
        logger.info("admin router not available yet; skipping")
        return

    create_admin_router = getattr(router_module, "create_admin_router", None)
    if create_admin_router is None:
        logger.info("admin router present but exposes no create_admin_router")
        return

    setup_router = _build_setup_router(context)
    app.include_router(
        create_admin_router(context=context, setup_router=setup_router)
    )


def _build_setup_router(context: GatewayContext) -> object | None:
    """Build the setup wizard router if the module is present (task 42.1).

    Imported defensively and returns ``None`` when unavailable so the admin
    router still mounts its core endpoints. No ``login_factory`` is supplied yet
    — the Telethon-backed interactive-login adapter is a later wiring step — so
    the Telegram login step reports HTTP 503 until it is injected, which is the
    correct state for a deployment that has not yet configured Telegram.
    """
    import importlib

    try:
        setup_module = importlib.import_module("gateway.app.admin.setup")
    except ImportError:
        logger.info("setup wizard router not available yet; skipping")
        return None

    create_setup_router = getattr(setup_module, "create_setup_router", None)
    if create_setup_router is None:
        logger.info("setup module present but exposes no create_setup_router")
        return None
    router: object = create_setup_router(context=context)
    return router


def _wire_health_endpoints(app: FastAPI, context: GatewayContext) -> None:
    """Mount ``/health/live`` and ``/health/ready`` (task 33.1).

    The health module (``gateway/app/admin/health.py``) builds its router from an
    injected session factory and Telegram port, so it is wired explicitly with
    those collaborators rather than through the generic ``context=`` convention
    the admin router/metrics use. Imported defensively so a future reshape of
    that module cannot stop the admin app from binding its port.
    """
    try:
        from gateway.app.admin import health
    except ImportError:
        logger.info("health endpoints not available yet; skipping health wiring")
        return

    app.include_router(
        health.create_health_router(
            session_factory=context.session_factory,
            telegram=context.telegram,
        )
    )


def _wire_metrics_endpoint(app: FastAPI, context: GatewayContext) -> None:
    """Mount ``/metrics`` if the metrics module is available (task 33.2)."""
    _wire_optional_router(
        app,
        context,
        module_name="gateway.app.admin.metrics",
        factory_name="create_metrics_router",
        label="metrics endpoint",
    )


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Run the gateway's startup and graceful-shutdown sequence.

    Shared by both the S3 and admin apps; the context is read from
    ``app.state.context`` so both apps drive the *same* collaborator graph. The
    database connect/migrate/staging-recovery sequence runs once (guarded so the
    second app's lifespan is a no-op), then the Telegram session and the
    background workers start. On shutdown the workers and session are stopped and
    the database pool disposed.
    """
    context: GatewayContext = app.state.context

    if not context._started:
        await _startup(context)
        context._started = True
        owns_lifecycle = True
    else:
        owns_lifecycle = False

    try:
        yield
    finally:
        if owns_lifecycle:
            await _shutdown(context)
            context._started = False


async def _startup(context: GatewayContext) -> None:
    """Connect the database, Telegram session, and launch background tasks.

    Order (Requirement 1): the database connect/migrate/staging-recovery runs
    first (Requirements 1.4/1.5/1.6) so no request is served against an
    unmigrated schema; the Telegram session connects next (Requirement 3.1); then
    the Upload Workers, cache evictor, and — when present — the checkpoint and GC
    workers start. The staging crash-recovery scan is injected so it uses the
    gateway's configured staging directory (design "Crash Recovery").
    """
    settings = context.settings
    context.staging_dir.mkdir(parents=True, exist_ok=True)
    context.cache_dir.mkdir(parents=True, exist_ok=True)

    # Run the connect/migrate/recover sequence against the factory already wired
    # into every collaborator (build_context opened the pool but did no I/O), so
    # the whole graph stays on one engine and nothing needs rebinding after
    # startup. Each step raises a typed error (DatabaseUnreachableError /
    # MigrationError) that main() lets propagate to a non-zero exit.
    await db_session.wait_for_database(context.session_factory.engine)
    await db_session.run_migrations(settings.DATABASE_URL)
    await _run_staging_recovery(context)

    # Connect the single Telegram session (Requirement 3.1). A disconnected
    # session (no credentials yet) leaves callers to receive 503 per 3.3.
    await context.telegram.start()

    _start_background_tasks(context)
    logger.info(
        "gateway started: S3 on :%d, admin on :%d",
        settings.S3_PORT,
        settings.ADMIN_PORT,
    )


async def _run_staging_recovery(context: GatewayContext) -> None:
    """Run the full crash-recovery sweep at startup (design "Crash Recovery").

    Opens one session and runs :func:`gateway.app.storage.staging.crash_recovery_scan`
    over the gateway's configured staging directory. The sweep (Requirement 14.2)
    quarantines staging files with no backing generation row and re-enqueues
    upload jobs for generations left ``STAGED`` / ``UPLOADING`` by a crash whose
    upload job is missing, skipping those that still have one. Runs after
    migrations so the ``generations`` and ``upload_jobs`` tables the scan queries
    exist, and before background workers start so an interrupted upload is queued
    before the first worker claims a job.
    """
    async with context.session_factory.session() as session:
        result = await staging_mod.crash_recovery_scan(
            session, staging_dir=context.staging_dir
        )
    if result.quarantined or result.reenqueued:
        logger.info(
            "staging crash recovery: quarantined %d orphaned file(s), "
            "re-enqueued %d interrupted upload(s)",
            len(result.quarantined),
            len(result.reenqueued),
        )


def _start_background_tasks(context: GatewayContext) -> None:
    """Launch the Upload Workers, cache evictor, and optional checkpoint/GC workers."""
    tasks: list[asyncio.Task[None]] = [
        asyncio.create_task(worker.run_forever(), name=f"upload-worker-{i}")
        for i, worker in enumerate(context.workers)
    ]
    tasks.append(
        asyncio.create_task(context.evictor.run_forever(), name="cache-evictor")
    )
    tasks.extend(_start_optional_workers(context))
    context._tasks = tasks


def _start_optional_workers(context: GatewayContext) -> list[asyncio.Task[None]]:
    """Start the checkpoint and GC workers if their modules exist (tasks 35/36).

    Each is imported defensively; a module that is not present yet is skipped
    with a logged note, leaving a clear hook for the task that adds it.
    """
    tasks: list[asyncio.Task[None]] = []
    for module_name, class_name, label in (
        ("gateway.app.jobs.checkpoint_worker", "CheckpointWorker", "checkpoint-worker"),
        ("gateway.app.jobs.gc_worker", "GCWorker", "gc-worker"),
    ):
        task = _maybe_start_worker(context, module_name, class_name, label)
        if task is not None:
            tasks.append(task)
    return tasks


def _maybe_start_worker(
    context: GatewayContext, module_name: str, class_name: str, label: str
) -> asyncio.Task[None] | None:
    """Instantiate and start one optional worker, or return ``None`` if absent.

    The worker class, when present, is expected to accept the gateway context and
    expose ``run_forever``/``stop`` like the Upload Worker and cache evictor. The
    exact constructor is finalised by the owning task; this wiring tries the
    common ``(context)`` shape and otherwise logs that manual wiring is needed.
    """
    import importlib

    try:
        module = importlib.import_module(module_name)
    except ImportError:
        logger.info("%s not available yet; skipping", label)
        return None

    worker_cls = getattr(module, class_name, None)
    runner = getattr(worker_cls, "run_forever", None) if worker_cls else None
    if worker_cls is None or runner is None:
        logger.info("%s present but not wireable; leaving as a hook", label)
        return None

    try:
        instance = worker_cls(context)
    except TypeError:
        logger.info(
            "%s has a constructor this wiring does not know; leaving as a hook",
            label,
        )
        return None
    return asyncio.create_task(instance.run_forever(), name=label)


async def _shutdown(context: GatewayContext) -> None:
    """Stop background tasks, the Telegram session, and the database pool.

    Requests each worker to stop, cancels and drains the background tasks, stops
    the Telegram session's monitor/reconnect loops, and disposes the database
    pool so in-flight work drains gracefully before the process exits.
    """
    for worker in context.workers:
        worker.stop()
    context.evictor.stop()

    for task in context._tasks:
        task.cancel()
    for task in context._tasks:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    context._tasks = []

    with contextlib.suppress(Exception):
        await context.telegram.stop()

    await db_session.shutdown(context.session_factory)
    logger.info("gateway shutdown complete")


# ---------------------------------------------------------------------------
# uvicorn entrypoint
# ---------------------------------------------------------------------------


async def _serve(context: GatewayContext) -> None:
    """Run the S3 and admin apps concurrently under uvicorn/uvloop.

    Two :class:`uvicorn.Server` instances share the one :class:`GatewayContext`
    (and therefore the one database pool, Telegram session, cache and worker
    pool); only the first app's lifespan runs the startup/shutdown sequence
    (guarded in :func:`lifespan`). Both servers are awaited together so the
    process stays up until both stop.
    """
    import uvicorn

    settings = context.settings
    s3_app = build_s3_app(context)
    admin_app = build_admin_app(context)

    s3_config = uvicorn.Config(
        s3_app,
        host="0.0.0.0",  # noqa: S104 - bind all interfaces inside the container
        port=settings.S3_PORT,
        loop="uvloop",
        log_level="info",
    )
    admin_config = uvicorn.Config(
        admin_app,
        host="0.0.0.0",  # noqa: S104 - bind all interfaces inside the container
        port=settings.ADMIN_PORT,
        loop="uvloop",
        log_level="info",
    )
    await asyncio.gather(
        uvicorn.Server(s3_config).serve(),
        uvicorn.Server(admin_config).serve(),
    )


def run(context: GatewayContext) -> None:
    """Run the gateway with uvloop until both servers stop."""
    import uvloop

    uvloop.run(_serve(context))


def main() -> None:
    """Process entrypoint: load config (fail-fast) then serve.

    :func:`gateway.app.config.load_settings` enforces Requirement 1.3 — missing
    required variables are printed to stderr, each on its own line, and the
    process exits non-zero *before* any port is bound. Only once settings
    validate is the collaborator graph built and the servers started.
    """
    logging.basicConfig(level=logging.INFO)
    settings = load_settings()
    context = build_context(settings)
    run(context)


if __name__ == "__main__":
    main()

"""Admin API router — the backend the React Console (Requirement 19) talks to.

This module builds the :class:`fastapi.APIRouter` mounted on the admin port by
:func:`gateway.app.main.build_admin_app` through its ``create_admin_router``
convention: ``_wire_optional_router`` imports this module, looks up
:func:`create_admin_router`, and calls it with ``context=context`` (the shared
:class:`~gateway.app.main.GatewayContext`). The router exposes the console-facing
surface the design's "Admin API" section lists:

* ``GET /admin/buckets`` — bucket list with per-bucket stats (object count,
  stored bytes, pending-write count) (Requirement 19.1).
* ``GET /admin/objects`` — prefix-filtered object browser with 100-per-page
  pagination (Requirement 19.2).
* ``GET /admin/jobs`` + ``POST /admin/jobs/{id}/retry`` — upload-job list and the
  retry action that transitions a FAILED job back to STAGED (Requirement 19.3).
* ``GET /admin/settings`` + ``PUT /admin/settings`` — runtime configuration read
  and update (Requirements 19.5–19.7).
* ``GET /admin/keys`` + ``POST /admin/keys`` + ``DELETE /admin/keys/{id}`` —
  S3 access-key management (Requirement 17; the create response returns the
  secret exactly once, 17.2).
* ``GET /admin/config`` — read-only Configuration Visibility: the non-secret
  Environment_Settings (name/value, requiring a redeploy or restart to change)
  plus a set/not-set flag for each secret Environment_Setting, never the secret
  value (admin-dashboard Requirement 6; 11.1).

Authentication and CSRF (Requirement 17.5–17.7)
-----------------------------------------------
Every admin request requires HTTP Basic credentials matching the admin password
configured at startup (17.5). The password is read from the ``TGOS3_ADMIN_PASSWORD``
environment variable at router-construction time; if it is absent the router
refuses to build and :func:`create_admin_router` raises
:class:`AdminAuthNotConfiguredError`, which the startup path surfaces as a
"admin credentials required" fatal error (17.6). A successful Basic auth both
authorises the request and establishes a lightweight session: the first
authenticated call mints a per-session CSRF token (bound to the Basic identity)
and returns it in the ``X-CSRF-Token`` response header. Every state-mutating
request (POST, PUT, DELETE, PATCH) must echo that token in the ``X-CSRF-Token``
request header; a missing or mismatched token is rejected with HTTP 403 (17.7).

Runtime settings (Requirement 19.5–19.7)
----------------------------------------
:class:`~gateway.app.config.Settings` is frozen, so runtime edits cannot mutate
it in place. Instead this router owns a small mutable :class:`RuntimeSettingsStore`
seeded from ``context.settings`` for exactly the five operator-editable fields
(staging size, cache size, cache half-life, read-ahead, upload concurrency). A
``PUT /admin/settings`` validates the new values against the same ranges
``Settings`` enforces and, crucially, refuses a staging-size reduction below the
amount currently reserved by in-flight uploads (19.6/19.7) by consulting the live
:class:`~gateway.app.storage.staging.StagingReservation`. The store is the single
place the rest of the gateway polls for these values, so a change is visible on
the next read — well inside the "effective within 5 s, no restart" bound (19.5) —
without rebuilding any collaborator. Components that still read the frozen
``Settings`` keep their startup value; wiring them to poll the store is a
follow-on, and the store is exposed on the router (``router.runtime_settings``)
so that wiring needs no change to this file's core.

Setup wizard seam (task 42.1)
-----------------------------
The ``POST /admin/setup/*`` wizard endpoints are a separate sibling task. To keep
that task from editing this module's core, :func:`create_admin_router` accepts an
optional ``setup_router``: when supplied it is included under the same auth/CSRF
dependencies, so task 42.1 can build its own :class:`fastapi.APIRouter` and hand
it in (or register it on the returned router) without touching the endpoints
here.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from gateway.app.db.models import AccessKey, Bucket, Generation, UploadJob
from gateway.app.db.session import AsyncSessionFactory
from gateway.app.format.key_wrap import derive_root_key, wrap
from gateway.app.storage import generations as generations_storage
from gateway.app.storage.staging import StagingReservation

if TYPE_CHECKING:
    from gateway.app.config import Settings
    from gateway.app.main import GatewayContext

__all__ = [
    "ADMIN_PASSWORD_ENV",
    "CSRF_HEADER",
    "JOB_STATE_FAILED",
    "AdminAuthNotConfiguredError",
    "ConfigVisibility",
    "RuntimeSettingsStore",
    "RuntimeSettingsValues",
    "create_admin_router",
]

#: Environment variable holding the admin Basic-auth password (Requirement 17.5).
#: Read once at router construction; its absence is fatal (17.6).
ADMIN_PASSWORD_ENV = "TGOS3_ADMIN_PASSWORD"

#: Fixed admin Basic-auth username. The design fixes a single admin identity, so
#: only the password is operator-configured; the username is a constant.
ADMIN_USERNAME = "admin"

#: Request/response header carrying the per-session CSRF token (Requirement 17.7).
CSRF_HEADER = "X-CSRF-Token"

#: HTTP methods that mutate state and therefore require a valid CSRF token.
_MUTATING_METHODS = frozenset({"POST", "PUT", "DELETE", "PATCH"})

#: Object-browser page size (Requirement 19.2: "100 objects per page").
OBJECTS_PAGE_SIZE = 100

#: The terminal ``upload_jobs.state`` a Console retry acts on (Requirement 19.3).
#: The literal matches :data:`gateway.app.jobs.upload_worker.JOB_STATE_FAILED`;
#: it is re-declared here so the admin surface stays independent of the worker
#: module (the admin API never imports the jobs package).
JOB_STATE_FAILED = "FAILED"

#: Generation states an object-browser / stats query treats as a live object.
#: Only COMMITTED generations are resolvable reads, so stored-bytes and object
#: counts are computed over them.
_LIVE_OBJECT_STATE = generations_storage.STATE_COMMITTED

#: Generation states that are "pending" — staged or in-flight but not yet durable
#: — counted as the per-bucket pending-write count (Requirement 19.1).
_PENDING_STATES = (
    generations_storage.STATE_STAGED,
    generations_storage.STATE_UPLOADING,
    generations_storage.STATE_RETRY_WAIT,
)


class AdminAuthNotConfiguredError(RuntimeError):
    """No admin password was configured at startup (Requirement 17.6).

    Raised by :func:`create_admin_router` when :data:`ADMIN_PASSWORD_ENV` is
    unset or empty. The startup path treats this as fatal and emits an error
    stating that admin credentials are required, refusing to serve the admin API
    without authentication.
    """


# ---------------------------------------------------------------------------
# Runtime settings store (Requirement 19.5–19.7)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RuntimeSettingsValues:
    """The five operator-editable runtime settings (Requirement 19.5).

    A frozen snapshot of the values the Console settings page can change. Kept
    separate from :class:`~gateway.app.config.Settings` because that model is
    frozen and env-sourced; this value object is what :class:`RuntimeSettingsStore`
    swaps atomically on a successful update.
    """

    staging_max_size: int
    cache_max_size: int
    cache_half_life: int
    read_ahead: int
    upload_concurrency: int


class RuntimeSettingsStore:
    """Holds the live, mutable copy of the operator-editable settings.

    Seeded from the frozen startup :class:`~gateway.app.config.Settings`, this is
    the single place the gateway reads the five runtime-tunable values so an
    admin update is visible on the next read without a restart (Requirement 19.5,
    "effective within 5 s"). Reads return an immutable
    :class:`RuntimeSettingsValues` snapshot; :meth:`apply` swaps the whole
    snapshot in one assignment, which is atomic with respect to the event loop
    (no ``await`` inside), so a concurrent reader always sees a consistent set.
    """

    def __init__(self, values: RuntimeSettingsValues) -> None:
        """Capture the initial snapshot (built by :meth:`from_settings`)."""
        self._values = values

    @classmethod
    def from_settings(cls, settings: Settings) -> RuntimeSettingsStore:
        """Build a store seeded from the startup configuration."""
        return cls(
            RuntimeSettingsValues(
                staging_max_size=settings.STAGING_MAX_SIZE,
                cache_max_size=settings.CACHE_MAX_SIZE,
                cache_half_life=settings.CACHE_HALF_LIFE,
                read_ahead=settings.READ_AHEAD,
                upload_concurrency=settings.UPLOAD_CONCURRENCY,
            )
        )

    @property
    def values(self) -> RuntimeSettingsValues:
        """Return the current immutable snapshot."""
        return self._values

    def apply(self, values: RuntimeSettingsValues) -> None:
        """Replace the current snapshot with ``values`` in one atomic assignment."""
        self._values = values


# ---------------------------------------------------------------------------
# CSRF / session management (Requirement 17.7)
# ---------------------------------------------------------------------------


class _CsrfSessions:
    """Maps an authenticated admin identity to its CSRF token.

    A deliberately small in-process session table: the design describes a single
    admin identity, so one token per Basic-auth username suffices. The first
    authenticated request mints a token (returned in the response header); every
    later mutating request must present the same token. Tokens are compared with
    :func:`secrets.compare_digest` to avoid leaking timing information.
    """

    def __init__(self) -> None:
        self._tokens: dict[str, str] = {}

    def ensure_token(self, identity: str) -> str:
        """Return the identity's token, minting one on first use."""
        token = self._tokens.get(identity)
        if token is None:
            token = secrets.token_urlsafe(32)
            self._tokens[identity] = token
        return token

    def verify(self, identity: str, presented: str | None) -> bool:
        """Return whether ``presented`` matches the identity's token."""
        expected = self._tokens.get(identity)
        if expected is None or presented is None:
            return False
        return secrets.compare_digest(expected, presented)


# ---------------------------------------------------------------------------
# Request / response bodies
# ---------------------------------------------------------------------------


class BucketStats(BaseModel):
    """Per-bucket statistics for the Console bucket list (Requirement 19.1)."""

    name: str
    object_count: int
    stored_bytes: int
    pending_write_count: int


class ObjectEntry(BaseModel):
    """One committed object in the object browser (Requirement 19.2)."""

    key: str
    size: int
    etag: str | None
    last_modified: datetime


class ObjectsPage(BaseModel):
    """A single page of the prefix-filtered object browser (Requirement 19.2)."""

    bucket: str
    prefix: str
    objects: list[ObjectEntry]
    #: Opaque offset to pass back as ``start_after`` for the next page, or
    #: ``None`` when this is the last page.
    next_start_after: str | None


class JobEntry(BaseModel):
    """One upload job for the Console jobs view (Requirement 19.3)."""

    id: str
    generation_id: str
    state: str
    attempts: int
    last_error: str | None


class SettingsBody(BaseModel):
    """The runtime-settings payload for GET/PUT ``/admin/settings`` (19.5).

    Field ranges mirror :class:`~gateway.app.config.Settings` so a PUT is
    rejected with the same bounds the startup validation enforces. The
    staging-reserved cross-check (19.6/19.7) is applied by the handler, not here,
    because it depends on live runtime state.
    """

    model_config = {"extra": "forbid"}

    staging_max_size: int = Field(ge=1)
    cache_max_size: int = Field(ge=1)
    cache_half_life: int = Field(ge=1)
    read_ahead: int = Field(ge=0, le=32)
    upload_concurrency: int = Field(ge=1)


class EnvSettingEntry(BaseModel):
    """One non-secret Environment_Setting for the Configuration Visibility panel.

    Mirrors the admin-dashboard glossary's Environment_Setting: a value
    materialised from the environment at startup by
    :class:`~gateway.app.config.Settings` that cannot be changed at runtime and
    requires a redeploy or restart to change (admin-dashboard Requirement 6.2).
    The value is rendered verbatim as a string so the Console shows it read-only;
    no editing control is offered (6.4). Only non-secret settings appear here —
    secret settings are reported through :class:`SecretEnvSettingEntry` as a
    set/not-set flag and never carry their value (6.3).
    """

    name: str
    value: str


class SecretEnvSettingEntry(BaseModel):
    """One secret Environment_Setting, reported as set/not-set only.

    Per admin-dashboard Requirements 6.3 and 11.1 the Dashboard must never
    display a secret Environment_Setting value (the Master_Key or the database
    URL's embedded credentials); it indicates only whether the value is
    configured. This entry therefore carries a boolean ``is_set`` and never the
    secret itself, so no secret material crosses the Admin API boundary.
    """

    name: str
    is_set: bool


class ConfigVisibility(BaseModel):
    """The read-only Configuration Visibility payload for ``GET /admin/config``.

    Groups the two kinds of Environment_Setting the Console's Configuration
    Visibility panel renders (admin-dashboard Requirement 6):

    * ``environment`` — the non-secret settings, each as a name/value pair shown
      read-only and labelled as requiring a redeploy or restart to change
      (6.2/6.4).
    * ``secrets`` — the secret settings, each reported only as set/not-set and
      never carrying its value (6.3/11.1).

    The five operator-editable Runtime_Settings are intentionally absent: they
    are served (and edited) through ``GET``/``PUT /admin/settings`` (Requirement
    4) rather than this read-only surface.
    """

    environment: list[EnvSettingEntry]
    secrets: list[SecretEnvSettingEntry]


class CreateKeyBody(BaseModel):
    """The create-access-key request body (Requirement 17)."""

    model_config = {"extra": "forbid"}

    label: str | None = None


class KeyEntry(BaseModel):
    """One access key in the key-management list (secret never included)."""

    access_key_id: str
    label: str | None
    disabled: bool


class CreatedKey(BaseModel):
    """The create-access-key response — the only time the secret is returned.

    Per Requirement 17.2 the plaintext secret is surfaced exactly once, in this
    response, and never stored or returned again.
    """

    access_key_id: str
    secret_access_key: str
    label: str | None


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def create_admin_router(
    *,
    context: GatewayContext,
    setup_router: APIRouter | None = None,
) -> APIRouter:
    """Build the admin API router for the admin port (Requirements 17, 19).

    Reads the admin password from :data:`ADMIN_PASSWORD_ENV`; its absence raises
    :class:`AdminAuthNotConfiguredError` so startup can refuse to serve an
    unauthenticated admin API (Requirement 17.6). Wires every admin endpoint
    behind a shared Basic-auth + CSRF dependency and returns the router ready for
    :func:`gateway.app.main.build_admin_app` to mount.

    Args:
        context: The shared gateway context providing the session factory,
            startup settings, the live staging reservation, and the master key.
        setup_router: Optional sibling router (task 42.1's ``/admin/setup/*``
            wizard). When supplied it is included under the same auth/CSRF
            dependencies so the wizard task needs no edit to this module's core.

    Returns:
        The configured :class:`fastapi.APIRouter`.

    Raises:
        AdminAuthNotConfiguredError: If no admin password is configured.
    """
    admin_password = os.environ.get(ADMIN_PASSWORD_ENV, "").strip()
    if not admin_password:
        raise AdminAuthNotConfiguredError(
            f"{ADMIN_PASSWORD_ENV} is not set; the admin API requires an admin "
            "password (Requirement 17.6)"
        )

    sessions = context.session_factory
    staging = context.staging
    runtime_settings = RuntimeSettingsStore.from_settings(context.settings)
    root_key = derive_root_key(context.settings.TGOS3_MASTER_KEY)
    csrf_sessions = _CsrfSessions()
    basic = HTTPBasic(auto_error=False)

    # These two dependency closures capture the per-router locals ``basic`` and
    # ``authenticate``. Under ``from __future__ import annotations`` every
    # annotation is a string, and FastAPI resolves parameter markers via
    # ``typing.get_type_hints``; evaluating an ``Annotated[..., Depends(basic)]``
    # string fails with ``NameError`` because ``basic`` is a closure local, not a
    # module global, which silently degrades the parameter to a query field.
    # The default-value marker form sidesteps that evaluation, so it is used here
    # (``# noqa: B008`` — this *is* the FastAPI idiom; the bugbear rule targets
    # ordinary functions, and the alternative ``Annotated`` form is broken for a
    # closure-captured dependency).
    def authenticate(
        response: Response,
        credentials: HTTPBasicCredentials | None = Depends(basic),  # noqa: B008
    ) -> str:
        """Validate Basic auth and emit the session CSRF token (17.5/17.7).

        Returns the authenticated identity (the Basic username). Rejects a
        missing or wrong credential with HTTP 401 ``WWW-Authenticate: Basic``.
        On success the identity's CSRF token is attached to the response header
        so the client can echo it on later mutating requests.
        """
        if credentials is None or not _check_basic(credentials, admin_password):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="admin authentication required",
                headers={"WWW-Authenticate": "Basic"},
            )
        identity = credentials.username
        response.headers[CSRF_HEADER] = csrf_sessions.ensure_token(identity)
        return identity

    def require_csrf(
        identity: str = Depends(authenticate),  # noqa: B008
        x_csrf_token: str | None = Header(default=None, alias=CSRF_HEADER),  # noqa: B008
    ) -> str:
        """Validate the CSRF token on a state-mutating request (17.7).

        Depends on :func:`authenticate` so Basic auth runs first; then checks the
        presented ``X-CSRF-Token`` against the session-bound token, rejecting a
        mismatch with HTTP 403.
        """
        if not csrf_sessions.verify(identity, x_csrf_token):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="missing or invalid CSRF token",
            )
        return identity

    read_dep = Depends(authenticate)
    write_dep = Depends(require_csrf)

    router = APIRouter(prefix="/admin", tags=["admin"])
    # Expose the runtime-settings store so components (and task 42.1) can read
    # the live values without reaching into this factory's closure.
    router.runtime_settings = runtime_settings  # type: ignore[attr-defined]

    _register_buckets(router, sessions, read_dep)
    _register_objects(router, sessions, read_dep)
    _register_jobs(router, sessions, read_dep, write_dep)
    _register_settings(router, runtime_settings, staging, read_dep, write_dep)
    _register_config(router, context.settings, read_dep)
    _register_keys(router, sessions, root_key, read_dep, write_dep)

    if setup_router is not None:
        # Mount the setup wizard (task 42.1) under the same auth/CSRF gate.
        router.include_router(
            setup_router,
            dependencies=[read_dep],
        )

    return router


def _check_basic(credentials: HTTPBasicCredentials, password: str) -> bool:
    """Constant-time check of admin Basic credentials (Requirement 17.5)."""
    user_ok = secrets.compare_digest(credentials.username, ADMIN_USERNAME)
    pass_ok = secrets.compare_digest(credentials.password, password)
    return user_ok and pass_ok


# ---------------------------------------------------------------------------
# Endpoint registration (grouped so the factory stays readable)
# ---------------------------------------------------------------------------


def _register_buckets(
    router: APIRouter,
    sessions: AsyncSessionFactory,
    read_dep: Any,
) -> None:
    """Register ``GET /admin/buckets`` (Requirement 19.1)."""

    @router.get("/buckets", dependencies=[read_dep])
    async def list_buckets() -> list[BucketStats]:
        """List every bucket with its object count, stored bytes, and pending count.

        Object count and stored bytes are computed over COMMITTED generations
        (the only resolvable reads); the pending-write count is the number of
        generations still staged / in-flight (Requirement 19.1).
        """
        async with sessions.session() as session:
            buckets = (
                await session.execute(select(Bucket).order_by(Bucket.name))
            ).scalars().all()

            stats: list[BucketStats] = []
            for bucket in buckets:
                committed = (
                    await session.execute(
                        select(
                            func.count(Generation.id),
                            func.coalesce(func.sum(Generation.size), 0),
                        ).where(
                            Generation.bucket_id == bucket.id,
                            Generation.state == _LIVE_OBJECT_STATE,
                        )
                    )
                ).one()
                pending = (
                    await session.execute(
                        select(func.count(Generation.id)).where(
                            Generation.bucket_id == bucket.id,
                            Generation.state.in_(_PENDING_STATES),
                        )
                    )
                ).scalar_one()
                stats.append(
                    BucketStats(
                        name=bucket.name,
                        object_count=int(committed[0]),
                        stored_bytes=int(committed[1]),
                        pending_write_count=int(pending),
                    )
                )
            return stats


def _register_objects(
    router: APIRouter,
    sessions: AsyncSessionFactory,
    read_dep: Any,
) -> None:
    """Register ``GET /admin/objects`` (Requirement 19.2)."""

    @router.get("/objects", dependencies=[read_dep])
    async def list_objects(
        bucket: Annotated[str, Query(min_length=1)],
        prefix: Annotated[str, Query()] = "",
        start_after: Annotated[str | None, Query()] = None,
    ) -> ObjectsPage:
        """Browse a bucket's committed objects, prefix-filtered and paginated.

        Returns up to :data:`OBJECTS_PAGE_SIZE` committed object keys in ascending
        key order, filtered to those sharing ``prefix`` and strictly greater than
        ``start_after`` (the previous page's ``next_start_after``). When a full
        page is returned, ``next_start_after`` carries the last key so the client
        can request the following page (Requirement 19.2).
        """
        async with sessions.session() as session:
            bucket_row = (
                await session.execute(
                    select(Bucket).where(Bucket.name == bucket)
                )
            ).scalar_one_or_none()
            if bucket_row is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"bucket {bucket!r} not found",
                )

            query = (
                select(Generation)
                .where(
                    Generation.bucket_id == bucket_row.id,
                    Generation.state == _LIVE_OBJECT_STATE,
                )
                .order_by(Generation.key)
                .limit(OBJECTS_PAGE_SIZE + 1)
            )
            if prefix:
                query = query.where(Generation.key.startswith(prefix))
            if start_after is not None:
                query = query.where(Generation.key > start_after)

            rows = list((await session.execute(query)).scalars().all())

        has_more = len(rows) > OBJECTS_PAGE_SIZE
        page_rows = rows[:OBJECTS_PAGE_SIZE]
        objects = [
            ObjectEntry(
                key=row.key,
                size=row.size,
                etag=row.etag,
                last_modified=row.last_modified,
            )
            for row in page_rows
        ]
        next_start_after = page_rows[-1].key if has_more and page_rows else None
        return ObjectsPage(
            bucket=bucket,
            prefix=prefix,
            objects=objects,
            next_start_after=next_start_after,
        )


def _register_jobs(
    router: APIRouter,
    sessions: AsyncSessionFactory,
    read_dep: Any,
    write_dep: Any,
) -> None:
    """Register ``GET /admin/jobs`` and ``POST /admin/jobs/{id}/retry`` (19.3)."""

    @router.get("/jobs", dependencies=[read_dep])
    async def list_jobs() -> list[JobEntry]:
        """List upload jobs with their state and last error (Requirement 19.3)."""
        async with sessions.session() as session:
            jobs = (
                await session.execute(
                    select(UploadJob).order_by(UploadJob.id)
                )
            ).scalars().all()
            return [
                JobEntry(
                    id=str(job.id),
                    generation_id=str(job.generation_id),
                    state=job.state,
                    attempts=job.attempts,
                    last_error=job.last_error,
                )
                for job in jobs
            ]

    @router.post("/jobs/{job_id}/retry", dependencies=[write_dep])
    async def retry_job(job_id: str) -> JobEntry:
        """Retry a FAILED upload job, transitioning it back to STAGED (19.3).

        Re-queues the job (``QUEUED`` with a cleared backoff) and returns its
        generation to ``STAGED`` so the Upload Worker claims it again. Only a
        FAILED job may be retried; any other state is rejected with HTTP 409, and
        an unknown id with HTTP 404.
        """
        async with sessions.session() as session:
            job = (
                await session.execute(
                    select(UploadJob).where(UploadJob.id == _as_uuid(job_id))
                )
            ).scalar_one_or_none()
            if job is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"upload job {job_id!r} not found",
                )
            if job.state != JOB_STATE_FAILED:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"only FAILED jobs may be retried; job is {job.state}",
                )

            generation = (
                await session.execute(
                    select(Generation).where(Generation.id == job.generation_id)
                )
            ).scalar_one_or_none()

            job.state = generations_storage.JOB_STATE_QUEUED
            job.next_attempt_at = None
            job.last_error = None
            if generation is not None:
                generation.state = generations_storage.STATE_STAGED

            return JobEntry(
                id=str(job.id),
                generation_id=str(job.generation_id),
                state=generations_storage.STATE_STAGED,
                attempts=job.attempts,
                last_error=None,
            )


def _register_settings(
    router: APIRouter,
    runtime_settings: RuntimeSettingsStore,
    staging: StagingReservation,
    read_dep: Any,
    write_dep: Any,
) -> None:
    """Register ``GET`` / ``PUT /admin/settings`` (Requirements 19.5–19.7)."""

    @router.get("/settings", dependencies=[read_dep])
    async def get_settings() -> SettingsBody:
        """Return the current runtime-editable settings (Requirement 19.5)."""
        values = runtime_settings.values
        return SettingsBody(
            staging_max_size=values.staging_max_size,
            cache_max_size=values.cache_max_size,
            cache_half_life=values.cache_half_life,
            read_ahead=values.read_ahead,
            upload_concurrency=values.upload_concurrency,
        )

    @router.put("/settings", dependencies=[write_dep])
    async def update_settings(body: SettingsBody) -> SettingsBody:
        """Apply a runtime-settings change (Requirements 19.5–19.7).

        Field ranges are validated by :class:`SettingsBody`. The one cross-field
        rule the model cannot express — a staging-size reduction must not drop
        below the bytes currently reserved by in-flight uploads — is enforced
        here against the live :class:`~gateway.app.storage.staging.StagingReservation`:
        a request that would reduce ``staging_max_size`` below the reserved amount
        is rejected with HTTP 409 and the minimum permitted value, and the change
        is not applied (19.6/19.7). On success the whole snapshot is swapped so
        the new values are effective on the next read (19.5).
        """
        reserved = staging.reserved_bytes
        if body.staging_max_size < reserved:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "staging_max_size cannot be reduced below the currently "
                    f"reserved {reserved} bytes; minimum permitted value is "
                    f"{reserved}"
                ),
            )

        runtime_settings.apply(
            RuntimeSettingsValues(
                staging_max_size=body.staging_max_size,
                cache_max_size=body.cache_max_size,
                cache_half_life=body.cache_half_life,
                read_ahead=body.read_ahead,
                upload_concurrency=body.upload_concurrency,
            )
        )
        return body


#: Secret Environment_Settings — their *values* must never cross the Admin API
#: boundary (admin-dashboard Requirements 6.3, 11.1). ``TGOS3_MASTER_KEY`` is the
#: root key itself; ``DATABASE_URL`` embeds the PostgreSQL credentials, so it is
#: treated as a secret and reported only as set/not-set, never echoed. Everything
#: else on :class:`~gateway.app.config.Settings` is a non-secret Environment_Setting.
_SECRET_ENV_SETTINGS: frozenset[str] = frozenset({"TGOS3_MASTER_KEY", "DATABASE_URL"})

#: The five operator-editable Runtime_Settings (admin-dashboard Requirement 4),
#: served and edited through ``GET``/``PUT /admin/settings`` rather than the
#: read-only Configuration Visibility surface. Excluded from ``GET /admin/config``
#: so the two surfaces stay disjoint (6.1).
_RUNTIME_SETTING_FIELDS: frozenset[str] = frozenset(
    {
        "STAGING_MAX_SIZE",
        "CACHE_MAX_SIZE",
        "CACHE_HALF_LIFE",
        "READ_AHEAD",
        "UPLOAD_CONCURRENCY",
    }
)


def _register_config(
    router: APIRouter,
    settings: Settings,
    read_dep: Any,
) -> None:
    """Register ``GET /admin/config`` (admin-dashboard Requirement 6).

    Exposes a read-only view of the deployment-fixed Environment_Settings so the
    Console's Configuration Visibility panel can show which configuration can
    only change via a redeploy or restart. Non-secret settings are returned as
    name/value pairs (6.2/6.4); secret settings (the master key, the database URL
    with its embedded credentials) are returned only as a set/not-set flag and
    never carry their value (6.3/11.1). The five operator-editable
    Runtime_Settings are omitted — they live on ``/admin/settings`` (6.1).
    """

    @router.get("/config", dependencies=[read_dep])
    async def get_config() -> ConfigVisibility:
        """Return the read-only environment configuration (Requirement 6).

        Iterates the frozen startup :class:`~gateway.app.config.Settings` model
        fields: each secret field becomes a set/not-set entry (its value never
        leaves the boundary, 6.3/11.1); each remaining non-runtime field becomes
        a read-only name/value entry (6.2/6.4). Values are stringified for
        display; booleans render as ``"true"``/``"false"`` and integers/strings
        verbatim. Field order follows the model's declaration order so the panel
        renders deterministically.
        """
        environment: list[EnvSettingEntry] = []
        secrets_out: list[SecretEnvSettingEntry] = []
        for name in type(settings).model_fields:
            value = getattr(settings, name)
            if name in _SECRET_ENV_SETTINGS:
                # A required secret is a non-empty string once Settings validates;
                # report only whether it is configured, never the value itself.
                secrets_out.append(
                    SecretEnvSettingEntry(
                        name=name,
                        is_set=bool(value) and str(value).strip() != "",
                    )
                )
                continue
            if name in _RUNTIME_SETTING_FIELDS:
                continue
            environment.append(EnvSettingEntry(name=name, value=_render_env_value(value)))
        return ConfigVisibility(environment=environment, secrets=secrets_out)


def _render_env_value(value: Any) -> str:
    """Render a non-secret Environment_Setting value for read-only display.

    Booleans become lower-case ``"true"``/``"false"`` so the panel reads
    naturally; every other scalar (int, str, float) is passed through ``str``.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _register_keys(
    router: APIRouter,
    sessions: AsyncSessionFactory,
    root_key: bytes,
    read_dep: Any,
    write_dep: Any,
) -> None:
    """Register the access-key management endpoints (Requirement 17)."""

    @router.get("/keys", dependencies=[read_dep])
    async def list_keys() -> list[KeyEntry]:
        """List access keys without ever returning any secret (Requirement 17.2)."""
        async with sessions.session() as session:
            keys = (
                await session.execute(
                    select(AccessKey).order_by(AccessKey.access_key_id)
                )
            ).scalars().all()
            return [
                KeyEntry(
                    access_key_id=key.access_key_id,
                    label=key.label,
                    disabled=key.disabled,
                )
                for key in keys
            ]

    @router.post("/keys", status_code=status.HTTP_201_CREATED, dependencies=[write_dep])
    async def create_key(body: CreateKeyBody) -> CreatedKey:
        """Create an access key, returning the secret exactly once (17.2).

        Generates a random access-key id and secret, stores the secret wrapped
        under the root key (AES Key Wrap, base64-encoded) so it is never persisted
        in plaintext yet can still be decrypted by
        :class:`~gateway.app.main.DbSecretProvider` to re-derive the SigV4 signing
        key, and returns the plaintext secret in this response alone — it is never
        retrievable again.
        """
        access_key_id = _generate_access_key_id()
        secret_access_key = _generate_secret()
        secret_enc = _wrap_secret(root_key, secret_access_key)

        async with sessions.session() as session:
            session.add(
                AccessKey(
                    access_key_id=access_key_id,
                    secret_enc=secret_enc,
                    label=body.label,
                    grants=None,
                    disabled=False,
                )
            )

        return CreatedKey(
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
            label=body.label,
        )

    @router.delete(
        "/keys/{access_key_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        dependencies=[write_dep],
    )
    async def delete_key(access_key_id: str) -> Response:
        """Delete an access key by id (Requirement 17).

        A missing key is reported with HTTP 404; a successful delete returns 204
        with no body. Deleting the key immediately revokes it, since the SigV4
        secret provider resolves an unknown id to an authentication failure.
        """
        async with sessions.session() as session:
            key = (
                await session.execute(
                    select(AccessKey).where(
                        AccessKey.access_key_id == access_key_id
                    )
                )
            ).scalar_one_or_none()
            if key is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"access key {access_key_id!r} not found",
                )
            await session.delete(key)
        return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Access-key generation / secret wrapping
# ---------------------------------------------------------------------------

#: Length (characters) of a generated access-key id. 20 upper-case alnum chars
#: mirrors the shape of an AWS access-key id.
_ACCESS_KEY_ID_LENGTH = 20

#: Alphabet for access-key ids: AWS-style upper-case letters and digits.
_ACCESS_KEY_ID_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"


def _generate_access_key_id() -> str:
    """Return a random AWS-style access-key id (``AKIA`` + base32-ish body)."""
    body = "".join(
        secrets.choice(_ACCESS_KEY_ID_ALPHABET)
        for _ in range(_ACCESS_KEY_ID_LENGTH - 4)
    )
    return f"AKIA{body}"


def _generate_secret() -> str:
    """Return a random SigV4 secret whose UTF-8 length suits AES Key Wrap.

    ``secrets.token_hex(20)`` yields 40 hex characters. AES Key Wrap (RFC 3394)
    requires the wrapped plaintext to be a multiple of 8 bytes and at least 16;
    40 ASCII bytes satisfies both, so the secret can be wrapped under the root
    key and later unwrapped and UTF-8 decoded by the SigV4 secret provider
    without padding.
    """
    return secrets.token_hex(20)


def _wrap_secret(root_key: bytes, secret: str) -> str:
    """Wrap a plaintext secret under the root key and base64-encode it.

    Mirrors :func:`gateway.app.main._default_secret_decryptor`: the secret's
    UTF-8 bytes are wrapped with AES Key Wrap under the derived root key and the
    result is base64-encoded, so the stored ``secret_enc`` round-trips through
    that decryptor to the plaintext the SigV4 verifier needs (Requirement 17.2).
    """
    import base64

    wrapped = wrap(root_key, secret.encode("utf-8"))
    return base64.b64encode(wrapped).decode("ascii")


def _as_uuid(value: str) -> Any:
    """Parse a path parameter into a UUID, 404-ing on a malformed id."""
    import uuid

    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"invalid id {value!r}",
        ) from exc

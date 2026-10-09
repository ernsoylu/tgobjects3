"""Guided first-run setup wizard — the ``POST /admin/setup/*`` steps (Requirement 2).

This module implements the backend of the first-run setup wizard the Console
drives (Requirement 2). It is a *sibling* of the admin router (task 41.1): rather
than editing that router's core, :func:`create_setup_router` builds its own
:class:`fastapi.APIRouter` (prefix ``/setup``) which the admin factory includes
under the same Basic-auth + CSRF dependencies via its ``setup_router`` seam, so
the resulting endpoints live at ``/admin/setup/*``.

The wizard collects its inputs across several POSTs, holding them in an
in-process :class:`SetupSession` until the operator finalises. The steps mirror
Requirement 2's acceptance criteria:

* ``POST /admin/setup/status`` — report whether setup is required (2.1): setup is
  required exactly while no S3 access key exists.
* ``POST /admin/setup/telegram`` — collect Telegram API id/hash and login (phone
  or bot token) and initiate login; when Telegram asks for a two-factor code the
  response says so and the operator re-POSTs with the ``code`` set (2.2, 2.3).
  Each wrong code consumes one of **3** attempts; the 3rd failure aborts the
  login step, returns an authentication-failed error, and resets the step so the
  operator starts credential entry again (2.4).
* ``POST /admin/setup/buckets`` — record bucket→channel assignments; a channel
  that already carries bucket-manifest messages triggers an adopt-vs-reinitialise
  choice, with a warning returned on the reinitialise path (2.7).
* ``POST /admin/setup/config`` — collect staging/cache paths and sizes and the
  master key, validated against Requirement 2.2's ranges.
* ``POST /admin/setup/finalize`` — validate everything is present, generate the
  downloadable Recovery Kit (master-key verifier, Telegram session credentials,
  and bucket channel assignments; 2.5), and create the first S3 access key,
  returning its secret exactly once (2.6).

Login seam (keeps Telethon out of this layer)
----------------------------------------------
The :class:`TelegramPort` protocol (Requirement 3.4) has no login surface — it
assumes an already-authenticated session. Interactive login (send-code, sign-in,
2FA) is therefore modelled here as its own small :class:`TelegramLoginPort`
``Protocol`` so the wizard's 2FA retry logic depends only on an interface. The
Telethon-backed implementation lives behind the same ports-and-adapters boundary
as the rest of Telegram access (``gateway/app/telegram``); a fake drives the
retry/abort paths in tests. :func:`create_setup_router` accepts an injectable
``login_factory`` so a deployment wires the real adapter while tests inject a
fake, with no change to this module.

Secret encoding
---------------
The first access key's secret is stored exactly as the admin router's
``_wrap_secret`` encodes it — AES Key Wrap (RFC 3394) under the derived root key,
base64-encoded — so the created key round-trips through
:class:`gateway.app.main.DbSecretProvider`'s default
:data:`~gateway.app.main.SecretDecryptor` and authenticates immediately against
the S3 SigV4 verifier (Requirement 17.2, 2.6).
"""

from __future__ import annotations

import base64
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from gateway.app.db.models import AccessKey
from gateway.app.db.session import AsyncSessionFactory
from gateway.app.format import bucket_manifest
from gateway.app.format.key_wrap import derive_root_key, wrap
from gateway.app.format.types import FormatVersionError

if TYPE_CHECKING:
    from gateway.app.config import Settings
    from gateway.app.main import GatewayContext
    from gateway.app.telegram.port import TelegramPort

__all__ = [
    "MASTER_KEY_MIN_LENGTH",
    "MAX_TWO_FACTOR_ATTEMPTS",
    "RecoveryKit",
    "SetupSession",
    "TelegramLoginPort",
    "TelegramLoginResult",
    "create_setup_router",
]

# ---------------------------------------------------------------------------
# Constants (Requirement 2 bounds)
# ---------------------------------------------------------------------------

#: Minimum master-key length in characters (Requirement 2.2; mirrors
#: :data:`gateway.app.config.MASTER_KEY_MIN_LENGTH`).
MASTER_KEY_MIN_LENGTH = 16

#: Lower/upper bounds for the staging volume maximum size, in bytes
#: (Requirement 2.2: "between 1 MiB and 1 TiB").
STAGING_MIN_SIZE = 1 * 1024**2
STAGING_MAX_SIZE = 1 * 1024**4

#: Lower/upper bounds for the record-cache maximum size, in bytes
#: (Requirement 2.2: "between 1 MiB and 100 GiB").
CACHE_MIN_SIZE = 1 * 1024**2
CACHE_MAX_SIZE = 100 * 1024**3

#: Number of two-factor code attempts the wizard allows before aborting the
#: Telegram login step (Requirement 2.3: "up to 3 attempts"; 2.4: exceeding 3
#: failures aborts and returns to credential entry).
MAX_TWO_FACTOR_ATTEMPTS = 3

#: HKDF/verifier label wrapped under the root key to produce the Recovery Kit's
#: master-key verifier. Wrapping a fixed, known plaintext under the root key
#: lets a recovering operator confirm they hold the correct master key (the
#: unwrap succeeds) without the kit ever carrying the master key itself.
_MASTER_KEY_VERIFIER_PLAINTEXT = b"tgobjects3-master-key-verifier-0"


# ---------------------------------------------------------------------------
# Telegram login seam (Requirement 2.2–2.4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TelegramLoginResult:
    """Outcome of one login step driven through :class:`TelegramLoginPort`.

    Attributes:
        authenticated: ``True`` when the session is now fully authenticated and
            no further input is needed.
        needs_code: ``True`` when Telegram has sent a login code and is waiting
            for the operator to supply it (either an SMS/app login code or the
            two-factor password), i.e. the wizard must prompt and re-POST with
            ``code`` set (Requirement 2.3).
        session_credentials: Opaque session string to persist for the single
            MTProto session once authenticated; ``None`` until then. Carried into
            the Recovery Kit (Requirement 2.5).
    """

    authenticated: bool
    needs_code: bool = False
    session_credentials: str | None = None


@runtime_checkable
class TelegramLoginPort(Protocol):
    """Interactive first-run login — the seam the wizard drives (Requirement 2.2).

    The steady-state :class:`~gateway.app.telegram.port.TelegramPort` assumes an
    already-authenticated session, so first-run login (send-code, sign-in, and
    the two-factor step) is modelled separately here. The production adapter is
    Telethon-backed and lives behind the Telegram package boundary; tests inject
    a fake. All methods are coroutines and raise
    :class:`~gateway.app.telegram.errors.TelegramError` subclasses rather than
    leaking library exceptions.
    """

    async def start_login(
        self, api_id: int, api_hash: str, login: str
    ) -> TelegramLoginResult:
        """Begin login for ``login`` (a phone number or bot token).

        Returns a result whose ``needs_code`` is ``True`` when Telegram requires
        a code/two-factor password before the session authenticates, or whose
        ``authenticated`` is ``True`` when login completed outright (e.g. a bot
        token).
        """
        ...

    async def submit_code(self, code: str) -> TelegramLoginResult:
        """Submit a login or two-factor code for the in-progress login.

        Returns an ``authenticated`` result on success. On a wrong code it raises
        :class:`TwoFactorFailedError` so the wizard can count the attempt
        (Requirement 2.3/2.4).
        """
        ...

    async def reset(self) -> None:
        """Discard any in-progress login so credential entry can restart (2.4)."""
        ...


class TwoFactorFailedError(Exception):
    """A submitted login/two-factor code was rejected (Requirement 2.3).

    Raised by :meth:`TelegramLoginPort.submit_code` for a wrong code so the
    wizard counts the failed attempt and, after :data:`MAX_TWO_FACTOR_ATTEMPTS`
    failures, aborts the step (Requirement 2.4).
    """


#: Builds the interactive login port for a deployment. Injected so the Telethon
#: adapter is wired in production while tests supply a fake; ``None`` means the
#: Telegram login step is unavailable (the endpoint returns HTTP 503).
TelegramLoginFactory = Callable[["Settings"], TelegramLoginPort]


# ---------------------------------------------------------------------------
# Wizard session state
# ---------------------------------------------------------------------------


@dataclass
class _TelegramCredentials:
    """The Telegram API credentials and resolved session for the wizard."""

    api_id: int
    api_hash: str
    login: str
    session_credentials: str | None = None
    authenticated: bool = False


@dataclass
class _BucketAssignment:
    """One bucket→channel assignment collected during the buckets step (2.7)."""

    bucket_name: str
    channel_id: int
    #: ``True`` when the operator chose to reinitialise a channel that already
    #: held bucket-manifest messages (its existing data becomes inaccessible).
    reinitialised: bool = False


@dataclass
class _StorageConfig:
    """Staging/cache paths and sizes plus the master key (Requirement 2.2)."""

    staging_path: str
    staging_max_size: int
    cache_path: str
    cache_max_size: int
    master_key: str


@dataclass
class SetupSession:
    """In-process accumulator for the wizard's multi-step inputs (Requirement 2).

    One wizard runs at a time on a freshly-deployed gateway, so a single
    process-held session suffices. Each step populates its slice; the login step
    also tracks the running count of failed two-factor attempts so the 4th
    failed attempt (exceeding the limit of 3) aborts and resets (Requirement
    2.4).
    """

    telegram: _TelegramCredentials | None = None
    buckets: list[_BucketAssignment] = field(default_factory=list)
    storage: _StorageConfig | None = None
    two_factor_failures: int = 0
    login_in_progress: bool = False

    def reset_login(self) -> None:
        """Clear all Telegram-login state so credential entry restarts (2.4)."""
        self.telegram = None
        self.two_factor_failures = 0
        self.login_in_progress = False


# ---------------------------------------------------------------------------
# Recovery Kit (Requirement 2.5)
# ---------------------------------------------------------------------------


class RecoveryKitBucket(BaseModel):
    """One bucket channel assignment in the Recovery Kit (Requirement 2.5)."""

    bucket_name: str
    channel_id: int


class RecoveryKit(BaseModel):
    """The downloadable Recovery Kit presented on successful setup (2.5).

    Carries the master-key *verifier* (never the master key itself), the Telegram
    session credentials, and the bucket channel assignments — the inputs an
    operator needs to re-establish the single session and point the Recovery CLI
    at the right channels. Returned inline in the finalize response and intended
    to be offered to the operator as a downloadable bundle by the Console.
    """

    #: Base64 AES-Key-Wrap of a fixed known plaintext under the root key. A
    #: recovering operator confirms possession of the correct master key when
    #: the unwrap succeeds; the master key itself never appears in the kit.
    master_key_verifier: str
    telegram_session: str
    buckets: list[RecoveryKitBucket]


# ---------------------------------------------------------------------------
# Request / response bodies
# ---------------------------------------------------------------------------


class SetupStatus(BaseModel):
    """Whether first-run setup is required (Requirement 2.1)."""

    setup_required: bool


class TelegramStepBody(BaseModel):
    """``POST /admin/setup/telegram`` body (Requirement 2.2/2.3).

    The operator first POSTs ``api_id``/``api_hash``/``login`` to begin login,
    then re-POSTs with ``code`` set to answer a two-factor prompt. Both are
    accepted on the same endpoint so the wizard can loop on the credential step.
    """

    model_config = {"extra": "forbid"}

    api_id: int = Field(gt=0)
    api_hash: str = Field(min_length=1)
    login: str = Field(min_length=1)
    code: str | None = None


class TelegramStepResult(BaseModel):
    """Outcome of a Telegram login step.

    ``needs_code`` asks the Console to prompt for a two-factor code and re-POST;
    ``authenticated`` signals the login step is complete. On the 3rd failed code
    (exceeding the limit) ``aborted`` is set and the step resets to credential
    entry (Requirement 2.4).
    """

    authenticated: bool
    needs_code: bool
    attempts_remaining: int


class BucketAssignmentBody(BaseModel):
    """One bucket→channel assignment for ``POST /admin/setup/buckets`` (2.7)."""

    model_config = {"extra": "forbid"}

    bucket_name: str = Field(min_length=1)
    channel_id: int
    #: When a channel already holds bucket-manifest messages the operator must
    #: choose: ``"adopt"`` keeps the existing bucket, ``"reinitialise"`` discards
    #: it (with a warning). Ignored for an empty channel.
    on_existing: str | None = None


class BucketsStepBody(BaseModel):
    """``POST /admin/setup/buckets`` body: the full set of assignments (2.7)."""

    model_config = {"extra": "forbid"}

    assignments: list[BucketAssignmentBody] = Field(min_length=1)


class BucketStepEntry(BaseModel):
    """Per-bucket outcome of the buckets step."""

    bucket_name: str
    channel_id: int
    #: ``True`` when the channel already contained bucket-manifest messages.
    existing_manifest: bool
    #: Set when ``existing_manifest`` is true and no choice was supplied: the
    #: operator must re-POST with ``on_existing`` as ``adopt`` or ``reinitialise``.
    requires_choice: bool
    #: A warning the Console must surface before proceeding on reinitialise (2.7).
    warning: str | None = None


class BucketsStepResult(BaseModel):
    """Outcome of the buckets step across all assignments (2.7)."""

    buckets: list[BucketStepEntry]
    #: ``True`` when at least one bucket needs an adopt/reinitialise choice; the
    #: wizard cannot proceed until the operator re-POSTs with the choices made.
    requires_choice: bool


class ConfigStepBody(BaseModel):
    """``POST /admin/setup/config`` body (Requirement 2.2).

    Field ranges mirror Requirement 2.2 directly: staging 1 MiB–1 TiB, cache
    1 MiB–100 GiB, master key at least 16 characters.
    """

    model_config = {"extra": "forbid"}

    staging_path: str = Field(min_length=1)
    staging_max_size: int = Field(ge=STAGING_MIN_SIZE, le=STAGING_MAX_SIZE)
    cache_path: str = Field(min_length=1)
    cache_max_size: int = Field(ge=CACHE_MIN_SIZE, le=CACHE_MAX_SIZE)
    master_key: str = Field(min_length=MASTER_KEY_MIN_LENGTH)


class ConfigStepResult(BaseModel):
    """Acknowledges a stored storage/config step."""

    accepted: bool


class FinalizeResult(BaseModel):
    """``POST /admin/setup/finalize`` response (Requirements 2.5, 2.6).

    Carries the downloadable Recovery Kit and the first access key — the only
    time the plaintext secret is ever returned (Requirement 2.6).
    """

    access_key_id: str
    secret_access_key: str
    recovery_kit: RecoveryKit


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def create_setup_router(
    *,
    context: GatewayContext,
    login_factory: TelegramLoginFactory | None = None,
) -> APIRouter:
    """Build the ``/admin/setup/*`` wizard router (Requirement 2).

    The returned router carries the ``/setup`` prefix; the admin factory
    (:func:`gateway.app.admin.router.create_admin_router`) includes it under its
    own ``/admin`` prefix and shared Basic-auth + CSRF dependencies, so the
    endpoints resolve at ``/admin/setup/*`` without this module touching the
    admin router's core.

    Args:
        context: The shared gateway context providing the session factory, the
            startup settings, and the single Telegram port/session.
        login_factory: Optional factory for the interactive
            :class:`TelegramLoginPort`. When ``None`` the Telegram login step is
            unavailable and returns HTTP 503, which is the correct state before a
            deployment wires its Telethon login adapter.

    Returns:
        The configured :class:`fastapi.APIRouter`.
    """
    sessions = context.session_factory
    settings = context.settings
    telegram_port: TelegramPort = context.telegram

    # One wizard runs at a time on a fresh deployment; the login port and the
    # accumulated session are created lazily on first use and held in the closure.
    wizard = SetupSession()
    login_state: dict[str, TelegramLoginPort] = {}

    router = APIRouter(prefix="/setup", tags=["admin", "setup"])

    _register_status(router, sessions)
    _register_telegram(router, wizard, login_state, settings, login_factory)
    _register_buckets(router, wizard, telegram_port)
    _register_config(router, wizard)
    _register_finalize(router, wizard, sessions, settings)

    return router


# ---------------------------------------------------------------------------
# Status step (Requirement 2.1)
# ---------------------------------------------------------------------------


async def _no_access_key_exists(sessions: AsyncSessionFactory) -> bool:
    """Return whether the gateway has zero access keys (setup is required; 2.1)."""
    async with sessions.session() as session:
        count = (
            await session.execute(select(func.count()).select_from(AccessKey))
        ).scalar_one()
    return int(count) == 0


def _register_status(router: APIRouter, sessions: AsyncSessionFactory) -> None:
    """Register ``POST /admin/setup/status`` (Requirement 2.1)."""

    @router.post("/status")
    async def setup_status() -> SetupStatus:
        """Report whether first-run setup is required (Requirement 2.1).

        Setup is required exactly while no S3 access key exists; the Console uses
        this to redirect the operator into the wizard on any request. Once the
        wizard's finalize step creates the first key, this reports
        ``setup_required = False`` and the redirect stops.
        """
        return SetupStatus(setup_required=await _no_access_key_exists(sessions))


# ---------------------------------------------------------------------------
# Telegram login step (Requirements 2.2–2.4)
# ---------------------------------------------------------------------------


def _register_telegram(
    router: APIRouter,
    wizard: SetupSession,
    login_state: dict[str, TelegramLoginPort],
    settings: Settings,
    login_factory: TelegramLoginFactory | None,
) -> None:
    """Register ``POST /admin/setup/telegram`` (Requirements 2.2–2.4)."""

    def _login_port() -> TelegramLoginPort:
        """Return the interactive login port, building it once per wizard run."""
        if login_factory is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Telegram login is not available on this deployment",
            )
        port = login_state.get("port")
        if port is None:
            port = login_factory(settings)
            login_state["port"] = port
        return port

    @router.post("/telegram")
    async def telegram_step(body: TelegramStepBody) -> TelegramStepResult:
        """Collect Telegram credentials and drive login, including 2FA (2.2–2.4).

        First call (no ``code``) records the API id/hash/login and begins login:
        a ``needs_code`` result asks the Console to prompt for a two-factor code.
        A follow-up call with ``code`` submits it; each wrong code consumes one of
        :data:`MAX_TWO_FACTOR_ATTEMPTS` attempts (2.3). The attempt that exceeds
        the limit (the 4th submission, i.e. the 3rd failure) aborts the login
        step, resets it to credential entry, and returns HTTP 401 so the operator
        starts over (2.4).
        """
        port = _login_port()

        if body.code is None:
            return await _begin_login(wizard, port, body)
        return await _submit_two_factor(wizard, port, body.code)


async def _begin_login(
    wizard: SetupSession, port: TelegramLoginPort, body: TelegramStepBody
) -> TelegramStepResult:
    """Start a fresh login from the submitted credentials (Requirement 2.2)."""
    wizard.telegram = _TelegramCredentials(
        api_id=body.api_id, api_hash=body.api_hash, login=body.login
    )
    wizard.two_factor_failures = 0
    result = await port.start_login(body.api_id, body.api_hash, body.login)
    wizard.login_in_progress = result.needs_code

    if result.authenticated:
        wizard.telegram.authenticated = True
        wizard.telegram.session_credentials = result.session_credentials
    return TelegramStepResult(
        authenticated=result.authenticated,
        needs_code=result.needs_code,
        attempts_remaining=MAX_TWO_FACTOR_ATTEMPTS,
    )


async def _submit_two_factor(
    wizard: SetupSession, port: TelegramLoginPort, code: str
) -> TelegramStepResult:
    """Submit a two-factor code, counting failures and aborting past 3 (2.3/2.4).

    A correct code authenticates and records the session credentials. A wrong
    code increments the failure count; the 3rd failure exceeds the limit, so the
    login step is reset to credential entry and HTTP 401 is returned (2.4).
    """
    if wizard.telegram is None or not wizard.login_in_progress:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="no Telegram login is awaiting a code; submit credentials first",
        )

    try:
        result = await port.submit_code(code)
    except TwoFactorFailedError:
        wizard.two_factor_failures += 1
        if wizard.two_factor_failures >= MAX_TWO_FACTOR_ATTEMPTS:
            await _abort_login(wizard, port)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=(
                    "Telegram authentication failed after "
                    f"{MAX_TWO_FACTOR_ATTEMPTS} attempts; re-enter the Telegram "
                    "credentials to try again"
                ),
            ) from None
        return TelegramStepResult(
            authenticated=False,
            needs_code=True,
            attempts_remaining=MAX_TWO_FACTOR_ATTEMPTS - wizard.two_factor_failures,
        )

    wizard.telegram.authenticated = result.authenticated
    wizard.telegram.session_credentials = result.session_credentials
    wizard.login_in_progress = False
    return TelegramStepResult(
        authenticated=result.authenticated,
        needs_code=False,
        attempts_remaining=MAX_TWO_FACTOR_ATTEMPTS - wizard.two_factor_failures,
    )


async def _abort_login(wizard: SetupSession, port: TelegramLoginPort) -> None:
    """Discard the in-progress login and reset the wizard's login step (2.4)."""
    import contextlib

    with contextlib.suppress(Exception):
        await port.reset()
    wizard.reset_login()


# ---------------------------------------------------------------------------
# Bucket channel assignment step (Requirement 2.7)
# ---------------------------------------------------------------------------

#: Caption prefix of a bucket-manifest message (``tgo3-k-<version>``); a channel
#: carrying one is already an initialised bucket (design "Telegram Message
#: Format"; :mod:`gateway.app.format.bucket_manifest`).
_BUCKET_MANIFEST_CAPTION_PREFIX = "tgo3-k-"

#: The adopt/reinitialise choices the operator may supply for an existing channel.
_CHOICE_ADOPT = "adopt"
_CHOICE_REINITIALISE = "reinitialise"

#: Warning surfaced before a reinitialise proceeds (Requirement 2.7).
_REINIT_WARNING = (
    "Reinitialising this channel will make its existing bucket data "
    "inaccessible."
)

#: How many of a channel's leading messages the wizard scans for a bucket
#: manifest. A manifest is written at bucket creation, so it sits among the
#: earliest messages; scanning a small prefix is enough to detect adoption.
_MANIFEST_SCAN_LIMIT = 20


def _register_buckets(
    router: APIRouter,
    wizard: SetupSession,
    telegram_port: TelegramPort,
) -> None:
    """Register ``POST /admin/setup/buckets`` (Requirement 2.7)."""

    @router.post("/buckets")
    async def buckets_step(body: BucketsStepBody) -> BucketsStepResult:
        """Assign channels to buckets, detecting existing manifests (2.7).

        For each assignment the channel is probed for an existing bucket manifest.
        A channel with no manifest is assigned directly. A channel that already
        holds one requires the operator to choose ``adopt`` or ``reinitialise``:
        when no choice is supplied the entry is flagged ``requires_choice`` and
        the step does not record it; a ``reinitialise`` choice is recorded with a
        warning the Console must surface before proceeding (2.7). Re-POSTing the
        full set with the choices made commits the assignments.
        """
        entries: list[BucketStepEntry] = []
        resolved: list[_BucketAssignment] = []
        needs_choice = False

        for assignment in body.assignments:
            has_manifest = await _channel_has_bucket_manifest(
                telegram_port, assignment.channel_id
            )
            entry, record = _resolve_assignment(assignment, has_manifest)
            entries.append(entry)
            if entry.requires_choice:
                needs_choice = True
            elif record is not None:
                resolved.append(record)

        if not needs_choice:
            wizard.buckets = resolved
        return BucketsStepResult(buckets=entries, requires_choice=needs_choice)


def _resolve_assignment(
    assignment: BucketAssignmentBody, has_manifest: bool
) -> tuple[BucketStepEntry, _BucketAssignment | None]:
    """Classify one assignment into its step entry and recorded value (2.7).

    An empty channel is recorded as a plain assignment. A channel with an
    existing manifest and no choice is flagged ``requires_choice`` (not recorded);
    an ``adopt`` choice records the existing bucket; a ``reinitialise`` choice
    records it flagged for reinitialisation and carries the warning.
    """
    if not has_manifest:
        return (
            BucketStepEntry(
                bucket_name=assignment.bucket_name,
                channel_id=assignment.channel_id,
                existing_manifest=False,
                requires_choice=False,
            ),
            _BucketAssignment(
                bucket_name=assignment.bucket_name,
                channel_id=assignment.channel_id,
            ),
        )

    choice = assignment.on_existing
    if choice == _CHOICE_ADOPT:
        return (
            BucketStepEntry(
                bucket_name=assignment.bucket_name,
                channel_id=assignment.channel_id,
                existing_manifest=True,
                requires_choice=False,
            ),
            _BucketAssignment(
                bucket_name=assignment.bucket_name,
                channel_id=assignment.channel_id,
            ),
        )
    if choice == _CHOICE_REINITIALISE:
        return (
            BucketStepEntry(
                bucket_name=assignment.bucket_name,
                channel_id=assignment.channel_id,
                existing_manifest=True,
                requires_choice=False,
                warning=_REINIT_WARNING,
            ),
            _BucketAssignment(
                bucket_name=assignment.bucket_name,
                channel_id=assignment.channel_id,
                reinitialised=True,
            ),
        )

    # Existing manifest but no (or an unrecognised) choice: ask the operator.
    return (
        BucketStepEntry(
            bucket_name=assignment.bucket_name,
            channel_id=assignment.channel_id,
            existing_manifest=True,
            requires_choice=True,
            warning=_REINIT_WARNING,
        ),
        None,
    )


async def _channel_has_bucket_manifest(
    telegram_port: TelegramPort, channel_id: int
) -> bool:
    """Return whether ``channel_id`` already carries a bucket-manifest message (2.7).

    Scans the channel's leading messages for a ``tgo3-k-`` captioned document and
    confirms it decodes as a :class:`~gateway.app.format.types.BucketManifest`.
    A Telegram error (unreachable/unauthorised) is surfaced as HTTP 503 so the
    operator retries rather than silently treating the channel as empty.
    """
    from gateway.app.telegram.errors import TelegramError

    try:
        messages = await telegram_port.get_messages(
            channel_id, list(range(1, _MANIFEST_SCAN_LIMIT + 1))
        )
    except TelegramError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"could not read channel {channel_id}: {exc}",
        ) from exc

    for message in messages:
        if not message.caption.startswith(_BUCKET_MANIFEST_CAPTION_PREFIX):
            continue
        if message.document_id is None:
            continue
        try:
            payload = await telegram_port.download_document(
                channel_id, message.message_id
            )
            bucket_manifest.deserialise(payload)
        except (TelegramError, FormatVersionError, ValueError, KeyError):
            continue
        return True
    return False


# ---------------------------------------------------------------------------
# Storage / master-key config step (Requirement 2.2)
# ---------------------------------------------------------------------------


def _register_config(router: APIRouter, wizard: SetupSession) -> None:
    """Register ``POST /admin/setup/config`` (Requirement 2.2)."""

    @router.post("/config")
    async def config_step(body: ConfigStepBody) -> ConfigStepResult:
        """Collect staging/cache paths and sizes and the master key (2.2).

        Field ranges are enforced by :class:`ConfigStepBody` exactly as
        Requirement 2.2 states (staging 1 MiB–1 TiB, cache 1 MiB–100 GiB, master
        key ≥ 16 characters), so an out-of-range value is rejected with HTTP 422
        before it is stored.
        """
        wizard.storage = _StorageConfig(
            staging_path=body.staging_path,
            staging_max_size=body.staging_max_size,
            cache_path=body.cache_path,
            cache_max_size=body.cache_max_size,
            master_key=body.master_key,
        )
        return ConfigStepResult(accepted=True)


# ---------------------------------------------------------------------------
# Finalize step (Requirements 2.5, 2.6)
# ---------------------------------------------------------------------------


def _register_finalize(
    router: APIRouter,
    wizard: SetupSession,
    sessions: AsyncSessionFactory,
    settings: Settings,
) -> None:
    """Register ``POST /admin/setup/finalize`` (Requirements 2.5, 2.6)."""

    @router.post("/finalize", status_code=status.HTTP_201_CREATED)
    async def finalize_step() -> FinalizeResult:
        """Complete setup: generate the Recovery Kit and the first key (2.5, 2.6).

        Validates that every prior step is complete (authenticated Telegram
        session, at least one bucket assignment, stored storage/master-key
        config); a missing step is rejected with HTTP 409. On success it derives
        the root key from the collected master key, builds the Recovery Kit
        (master-key verifier + Telegram session credentials + bucket channel
        assignments; 2.5), and creates the first S3 access key — storing its
        secret wrapped under the root key and returning the plaintext secret
        exactly once in this response (2.6).
        """
        telegram, buckets, storage = _require_complete(wizard)

        root_key = derive_root_key(storage.master_key)
        recovery_kit = _build_recovery_kit(root_key, telegram, buckets)

        access_key_id = _generate_access_key_id()
        secret_access_key = _generate_secret()
        secret_enc = _wrap_secret(root_key, secret_access_key)

        async with sessions.session() as session:
            if int(
                (
                    await session.execute(
                        select(func.count()).select_from(AccessKey)
                    )
                ).scalar_one()
            ) != 0:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="setup already completed; an access key already exists",
                )
            session.add(
                AccessKey(
                    access_key_id=access_key_id,
                    secret_enc=secret_enc,
                    label="setup-wizard",
                    grants=None,
                    disabled=False,
                )
            )

        return FinalizeResult(
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
            recovery_kit=recovery_kit,
        )


def _require_complete(
    wizard: SetupSession,
) -> tuple[_TelegramCredentials, list[_BucketAssignment], _StorageConfig]:
    """Return the collected steps, rejecting an incomplete wizard with 409.

    Enforces that finalize runs only after an authenticated Telegram session, at
    least one bucket assignment, and the storage/master-key config have all been
    collected (Requirement 2.2 requires every field before proceeding).
    """
    if wizard.telegram is None or not wizard.telegram.authenticated:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Telegram login is not complete",
        )
    if not wizard.buckets:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="no bucket channel assignments have been made",
        )
    if wizard.storage is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="staging/cache configuration and master key are not set",
        )
    return wizard.telegram, wizard.buckets, wizard.storage


def _build_recovery_kit(
    root_key: bytes,
    telegram: _TelegramCredentials,
    buckets: list[_BucketAssignment],
) -> RecoveryKit:
    """Assemble the Recovery Kit from the collected setup state (Requirement 2.5).

    The master-key *verifier* is a fixed known plaintext wrapped under the root
    key, so a recovering operator can confirm they hold the correct master key
    (the unwrap succeeds) without the kit ever carrying the key itself. The
    Telegram session credentials and the bucket channel assignments are copied
    through verbatim.
    """
    verifier = base64.b64encode(
        wrap(root_key, _MASTER_KEY_VERIFIER_PLAINTEXT)
    ).decode("ascii")
    return RecoveryKit(
        master_key_verifier=verifier,
        telegram_session=telegram.session_credentials or "",
        buckets=[
            RecoveryKitBucket(
                bucket_name=assignment.bucket_name,
                channel_id=assignment.channel_id,
            )
            for assignment in buckets
        ],
    )


# ---------------------------------------------------------------------------
# Access-key generation / secret wrapping
# ---------------------------------------------------------------------------
#
# These mirror the admin router's helpers (``gateway.app.admin.router``) exactly
# so a key created by the wizard is indistinguishable from one created by the
# admin API and round-trips through the same SigV4 secret provider.

#: Length (characters) of a generated access-key id body (AWS-style).
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

    ``secrets.token_hex(20)`` yields 40 hex characters; 40 ASCII bytes is a
    multiple of 8 and ≥ 16, so it wraps and unwraps cleanly under the root key
    (RFC 3394) and the SigV4 verifier recovers the exact secret (Requirement
    17.2).
    """
    return secrets.token_hex(20)


def _wrap_secret(root_key: bytes, secret: str) -> str:
    """Wrap a plaintext secret under the root key and base64-encode it.

    Matches :func:`gateway.app.main._default_secret_decryptor` and the admin
    router's ``_wrap_secret``: the secret's UTF-8 bytes are AES-Key-Wrapped under
    the derived root key and base64-encoded, so the stored ``secret_enc``
    round-trips through :class:`gateway.app.main.DbSecretProvider` to the
    plaintext the SigV4 verifier needs (Requirement 2.6, 17.2).
    """
    wrapped = wrap(root_key, secret.encode("utf-8"))
    return base64.b64encode(wrapped).decode("ascii")

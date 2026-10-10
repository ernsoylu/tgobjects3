"""Interactive first-run Telegram login backed by Telethon (Requirement 2.2–2.7).

The steady-state :class:`~gateway.app.telegram.port.TelegramPort` assumes an
already-authenticated session, so the one-time, interactive login the first-run
setup wizard drives (send-code → sign-in → two-factor) is modelled separately as
the :class:`~gateway.app.admin.setup.TelegramLoginPort` Protocol. This module is
the Telethon-backed implementation of that port.

Consistent with the ports-and-adapters rule for the Telegram boundary, this is
the *second and only other* module (after
:mod:`gateway.app.telegram.adapter`) permitted to import Telethon. Everything
else depends on the port Protocol so the transport stays fakeable in tests and
swappable in production.

The adapter owns one in-progress :class:`telethon.TelegramClient` built on a
:class:`telethon.sessions.StringSession` — nothing touches disk — and keeps it
across :meth:`start_login` → :meth:`submit_code` so the send-code, sign-in, and
2FA legs run against a single live session. Raw ``telethon.errors.*`` faults are
translated onto the gateway's typed errors per the design's error table: a wrong
or expired code (and a bad 2FA password) becomes
:class:`~gateway.app.admin.setup.TwoFactorFailedError` so the wizard counts the
attempt (Requirement 2.3/2.4); rate limiting becomes
:class:`gateway.app.telegram.errors.FloodWaitError`; and any other RPC, transport,
or OS fault becomes :class:`gateway.app.telegram.errors.TelegramUnavailableError`
(Requirement 2.7). No Telethon exception leaks past this class.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from telethon import TelegramClient
from telethon import errors as telethon_errors
from telethon.sessions import StringSession

from gateway.app.admin.setup import TelegramLoginResult, TwoFactorFailedError

from .errors import FloodWaitError, TelegramUnavailableError

if TYPE_CHECKING:
    from gateway.app.config import Settings

#: Telethon faults that mean the operator-supplied code or two-factor password
#: was wrong or no longer usable. All map onto
#: :class:`~gateway.app.admin.setup.TwoFactorFailedError` so the wizard counts a
#: failed attempt rather than aborting the whole login (Requirement 2.3/2.4).
_CODE_REJECTED_ERRORS: tuple[type[Exception], ...] = (
    telethon_errors.PhoneCodeInvalidError,
    telethon_errors.PhoneCodeExpiredError,
    telethon_errors.PasswordHashInvalidError,
    telethon_errors.CodeInvalidError,
)


class TelethonLoginAdapter:
    """Interactive first-run Telegram login backed by Telethon.

    Holds one in-progress :class:`telethon.TelegramClient` between
    :meth:`start_login` and :meth:`submit_code` so the send-code → sign-in → 2FA
    exchange runs against a single live session. The client is created on a
    :class:`telethon.sessions.StringSession`, so no session data is ever written
    to disk.

    A :class:`TelethonLoginAdapter` instance is created once per wizard run by
    the ``TelegramLoginFactory`` (the setup router caches the port in its
    ``login_state`` dict), so successive calls reuse the same client. Raw
    Telethon exceptions are mapped onto the gateway's typed errors and never leak
    past this boundary (Requirement 2.7).
    """

    def __init__(self, settings: Settings) -> None:
        """Store the deployment settings and start with no in-progress client.

        Args:
            settings: The constructed gateway :class:`~gateway.app.config.Settings`.
                Retained so a deployment can carry proxy/device configuration into
                the login client without this layer reading the environment
                directly; the Telegram API id/hash themselves arrive per login via
                :meth:`start_login`.
        """
        self._settings = settings
        self._client: TelegramClient | None = None
        #: The ``phone_code_hash`` returned by ``send_code_request``; needed to
        #: complete ``sign_in`` on the phone path. ``None`` on the bot-token path.
        self._phone_code_hash: str | None = None
        #: The phone number the code was requested for, carried into ``sign_in``.
        self._phone: str | None = None
        #: ``True`` once Telegram has asked for the 2FA password, so the next
        #: :meth:`submit_code` treats the submitted value as that password.
        self._awaiting_password: bool = False

    # -- port methods ------------------------------------------------------

    async def start_login(
        self, api_id: int, api_hash: str, login: str
    ) -> TelegramLoginResult:
        """Begin login for ``login`` (a phone number or a bot token).

        Builds a fresh in-memory client, connects, and branches on the login
        shape:

        * a bot token (``<digits>:<rest>``) is signed in directly and the result
          is :attr:`TelegramLoginResult.authenticated` with the serialised
          session credentials (Requirement 2.6);
        * a phone number triggers ``send_code_request``; the returned
          ``phone_code_hash`` is stored and ``needs_code=True`` is returned so the
          wizard prompts for the code (Requirement 2.3).

        Any prior in-progress login is discarded first so a restart begins clean.

        Raises:
            FloodWaitError: Telegram rate-limited the request.
            TelegramUnavailableError: The session could not connect or the request
                failed at the RPC/transport level.
        """
        await self.reset()
        client = TelegramClient(StringSession(), api_id, api_hash)
        self._client = client
        try:
            await client.connect()
            if _is_bot_token(login):
                await client.sign_in(bot_token=login)
                return TelegramLoginResult(
                    authenticated=True,
                    needs_code=False,
                    session_credentials=self._save_session(client),
                )
            sent = await client.send_code_request(phone=login)
            self._phone = login
            self._phone_code_hash = sent.phone_code_hash
            return TelegramLoginResult(authenticated=False, needs_code=True)
        except telethon_errors.FloodWaitError as exc:
            raise self._flood(exc) from exc
        except Exception as exc:
            raise self._unavailable("start_login", exc) from exc

    async def submit_code(self, code: str) -> TelegramLoginResult:
        """Submit a login code or a two-factor password for the in-progress login.

        On the first call ``code`` is the login code: ``sign_in`` is attempted
        with the stored ``phone_code_hash``. If Telegram then demands the account
        password, this returns ``needs_code=True`` and remembers that the next
        submission is the 2FA password (which is signed in with ``password=code``).

        On success the live :class:`telethon.sessions.StringSession` is serialised
        into :attr:`TelegramLoginResult.session_credentials` and
        ``authenticated=True`` is returned (Requirement 2.6).

        Raises:
            TwoFactorFailedError: The code or password was invalid or expired, so
                the wizard counts the failed attempt (Requirement 2.3/2.4).
            FloodWaitError: Telegram rate-limited the request.
            TelegramUnavailableError: No login is in progress, or the request
                failed at the RPC/transport level.
        """
        client = self._client
        if client is None:
            raise TelegramUnavailableError(
                "no Telegram login is in progress; call start_login first"
            )
        try:
            if self._awaiting_password:
                await client.sign_in(password=code)
            else:
                await client.sign_in(
                    phone=self._phone,
                    code=code,
                    phone_code_hash=self._phone_code_hash,
                )
        except telethon_errors.SessionPasswordNeededError:
            self._awaiting_password = True
            return TelegramLoginResult(authenticated=False, needs_code=True)
        except _CODE_REJECTED_ERRORS as exc:
            raise TwoFactorFailedError(str(exc)) from exc
        except telethon_errors.FloodWaitError as exc:
            raise self._flood(exc) from exc
        except Exception as exc:
            raise self._unavailable("submit_code", exc) from exc
        return TelegramLoginResult(
            authenticated=True,
            needs_code=False,
            session_credentials=self._save_session(client),
        )

    async def reset(self) -> None:
        """Discard any in-progress login so credential entry can restart (2.4).

        Disconnects and drops the in-progress client and all per-login state.
        Idempotent — safe to call when no login is in progress — and swallows any
        disconnect error so a reset never fails.
        """
        client = self._client
        self._client = None
        self._phone_code_hash = None
        self._phone = None
        self._awaiting_password = False
        if client is None:
            return
        try:
            result = client.disconnect()
            if result is not None:
                await result
        except Exception:
            # A reset must always succeed; a failed disconnect is irrelevant once
            # the client is discarded.
            return

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _save_session(client: TelegramClient) -> str:
        """Serialise the client's live :class:`StringSession` to a string (2.6)."""
        saved: object = StringSession.save(client.session)
        return str(saved)

    @staticmethod
    def _flood(exc: telethon_errors.FloodWaitError) -> FloodWaitError:
        """Translate Telethon's flood-wait into the gateway-typed error."""
        retry_after = int(getattr(exc, "seconds", 0) or 0)
        return FloodWaitError(retry_after=retry_after, message=str(exc))

    @staticmethod
    def _unavailable(operation: str, exc: Exception) -> TelegramUnavailableError:
        """Build a :class:`TelegramUnavailableError` for a failed operation."""
        return TelegramUnavailableError(f"Telegram {operation} failed: {exc}")


def _is_bot_token(login: str) -> bool:
    """Return whether ``login`` looks like a bot token rather than a phone number.

    A bot token has the shape ``<numeric id>:<secret>`` (e.g.
    ``123456:ABC-DEF...``), so it contains a ``:`` with a run of digits before it.
    A phone number never contains ``:``.
    """
    head, sep, _rest = login.partition(":")
    return bool(sep) and head.isdigit()

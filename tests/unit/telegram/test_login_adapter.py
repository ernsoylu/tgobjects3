"""Unit tests for the interactive Telegram login adapter.

Covers :mod:`gateway.app.telegram.login_adapter`.

The :class:`~gateway.app.telegram.login_adapter.TelethonLoginAdapter` is the
Telethon-backed implementation of the
:class:`~gateway.app.admin.setup.TelegramLoginPort` the first-run setup wizard
drives (Requirement 2.2-2.7). It is one of only two modules allowed to touch
Telethon, and its contract is that *no* raw Telethon exception ever leaks past
it: a wrong/expired code (or bad 2FA password) becomes
:class:`~gateway.app.admin.setup.TwoFactorFailedError`, rate limiting becomes
:class:`gateway.app.telegram.errors.FloodWaitError`, and every other RPC,
transport, or OS fault becomes
:class:`gateway.app.telegram.errors.TelegramUnavailableError` (Requirement 2.7).

These tests never touch real Telegram. The adapter builds its client via the
module-level ``TelegramClient`` name, so each test monkeypatches that name to a
:class:`FakeTelegramClient` whose per-method outcomes are scripted up front. The
fake raises the same ``telethon.errors.*`` the real client would, so the adapter
exercises its real translation logic against them.

This module is shared across two sibling tasks:

* **Property 1 (this task, 1.2)** — the ``test_no_telethon_exception_escapes``
  property test drives the adapter with arbitrary fault sequences and asserts the
  only exceptions that escape are ``TwoFactorFailedError`` or a
  :class:`gateway.app.telegram.errors.TelegramError` subclass.
* **Happy paths (sibling task 1.3)** — example tests for the bot-token and
  phone+code+2FA success paths are appended in the clearly delimited
  "Happy-path example tests" section below and reuse :class:`FakeTelegramClient`
  and the :func:`install_fake_client` helper defined here.

Keep the shared fake and helpers in the "Shared fakes & fixtures" section so the
happy-path tests can rely on them without redefining the Telethon seam.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
from gateway.app.admin.setup import TwoFactorFailedError
from gateway.app.config import Settings
from gateway.app.telegram import login_adapter
from gateway.app.telegram.errors import TelegramError
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from telethon import errors as telethon_errors
from telethon.crypto import AuthKey
from telethon.sessions import StringSession

if TYPE_CHECKING:
    from collections.abc import Sequence

# ===========================================================================
# Shared fakes & fixtures (reused by Property 1 and the happy-path tests)
# ===========================================================================


def _make_settings() -> Settings:
    """Build a minimal valid :class:`Settings` for constructing the adapter.

    The adapter only retains the settings object; it reads nothing from it along
    the login paths, so the required env-derived fields are given throwaway but
    valid values.
    """
    return Settings(
        TGOS3_MASTER_KEY="unit-test-master-key-0123456789",
        DATABASE_URL="postgresql+asyncpg://user:pass@localhost/db",
    )


def _populated_string_session() -> StringSession:
    """Return a :class:`StringSession` that serialises to a non-empty string.

    The adapter serialises the live session via ``StringSession.save(...)`` on
    success; a fresh unpopulated session saves to the empty string, so the fake
    seeds a DC and a random auth key to mimic an authenticated session. This lets
    the happy-path tests (task 1.3) assert ``session_credentials`` is non-empty.
    """
    session = StringSession()
    session.set_dc(2, "149.154.167.51", 443)
    session.auth_key = AuthKey(os.urandom(256))
    return session


# -- scripted outcomes -------------------------------------------------------

#: Sentinel outcome meaning "this call succeeds" (as opposed to raising).
_OK = "ok"


@dataclass
class FakeClientScript:
    """A scripted set of outcomes for one :class:`FakeTelegramClient` run.

    Each field is the outcome of the correspondingly-named client call the
    adapter may make. An outcome is either :data:`_OK` (the call succeeds) or an
    ``Exception`` instance the fake raises in place of the real Telethon fault.

    Attributes:
        connect: Outcome of ``client.connect()``.
        send_code: Outcome of ``client.send_code_request(phone=...)``.
        bot_sign_in: Outcome of ``client.sign_in(bot_token=...)``.
        code_sign_in: Outcome of ``client.sign_in(phone=, code=, phone_code_hash=)``.
        password_sign_in: Outcome of ``client.sign_in(password=...)``.
    """

    connect: str | Exception = _OK
    send_code: str | Exception = _OK
    bot_sign_in: str | Exception = _OK
    code_sign_in: str | Exception = _OK
    password_sign_in: str | Exception = _OK


@dataclass
class _SentCode:
    """Stand-in for Telethon's ``send_code_request`` return value."""

    phone_code_hash: str = "fake-phone-code-hash"


class FakeTelegramClient:
    """A controllable stand-in for :class:`telethon.TelegramClient`.

    Constructed with the same positional signature the adapter uses
    (``TelegramClient(session, api_id, api_hash)``) so it drops in via a
    monkeypatch of the ``TelegramClient`` name in
    :mod:`gateway.app.telegram.login_adapter`. Every method the adapter calls is
    implemented to either succeed or raise the exception scripted for it in the
    shared :class:`FakeClientScript`, so the adapter runs its real
    error-translation logic.

    The ``session`` attribute is a real (optionally populated)
    :class:`StringSession` so the adapter's ``StringSession.save(client.session)``
    on the success path returns a well-formed credential string.
    """

    #: The script every instance built during a test run shares. Set by
    #: :func:`install_fake_client` before the adapter constructs its client.
    script: FakeClientScript = FakeClientScript()
    #: The session every instance exposes. Set by :func:`install_fake_client`.
    session_template: StringSession | None = None

    def __init__(self, session: object, api_id: int, api_hash: str) -> None:
        # The adapter passes a fresh ``StringSession()``; we ignore it and expose
        # the test-controlled session so ``save`` yields deterministic output.
        self.api_id = api_id
        self.api_hash = api_hash
        template = type(self).session_template
        self.session: StringSession = (
            template if template is not None else StringSession()
        )
        self.connected = False
        self.disconnected = False

    @staticmethod
    def _resolve(outcome: str | Exception) -> None:
        """Raise ``outcome`` if it is an exception; otherwise return (success)."""
        if isinstance(outcome, Exception):
            raise outcome

    async def connect(self) -> None:
        self._resolve(type(self).script.connect)
        self.connected = True

    async def send_code_request(self, phone: str) -> _SentCode:
        self._resolve(type(self).script.send_code)
        return _SentCode()

    async def sign_in(
        self,
        phone: str | None = None,
        code: str | None = None,
        *,
        bot_token: str | None = None,
        password: str | None = None,
        phone_code_hash: str | None = None,
    ) -> object:
        """Dispatch to the scripted outcome for the sign-in flavour invoked.

        The adapter calls ``sign_in`` three distinct ways; the keyword actually
        supplied selects which scripted outcome applies.
        """
        if bot_token is not None:
            self._resolve(type(self).script.bot_sign_in)
        elif password is not None:
            self._resolve(type(self).script.password_sign_in)
        else:
            self._resolve(type(self).script.code_sign_in)
        return object()

    def disconnect(self) -> None:
        # Telethon's ``disconnect`` may return an awaitable or ``None``; the
        # adapter handles both. Returning ``None`` exercises the sync branch.
        self.disconnected = True
        return None


def install_fake_client(
    monkeypatch: pytest.MonkeyPatch,
    script: FakeClientScript,
    *,
    session: StringSession | None = None,
) -> None:
    """Point the adapter's ``TelegramClient`` at a scripted :class:`FakeTelegramClient`.

    Sets the shared script (and optional session) the fake instances read, then
    monkeypatches the ``TelegramClient`` name in the adapter module so the next
    ``start_login`` builds the fake instead of a real client.
    """
    FakeTelegramClient.script = script
    FakeTelegramClient.session_template = session
    monkeypatch.setattr(login_adapter, "TelegramClient", FakeTelegramClient)


# ===========================================================================
# Property 1: No Telethon exception escapes the login adapter (task 1.2)
# ===========================================================================
#
# Validates: Requirements 2.7

def _flood_error() -> Exception:
    """A Telethon ``FloodWaitError`` carrying a wait, like real rate limiting."""
    exc: Exception = telethon_errors.FloodWaitError(request=None)
    exc.seconds = 7  # type: ignore[attr-defined]
    return exc


#: The full fault vocabulary the fake may inject, covering every row of the
#: design's error-mapping table: a success sentinel, each code-rejected fault,
#: the 2FA-needed signal, flood wait, an RPC error, and transport/OS errors.
#: ``"flood"`` is a token rematerialised into a fresh ``FloodWaitError`` per use
#: (the exception carries mutable ``seconds`` state); everything else is a
#: reusable immutable sentinel or exception instance.
_FAULTS: list[str | Exception] = [
    _OK,
    telethon_errors.PhoneCodeInvalidError(request=None),
    telethon_errors.PhoneCodeExpiredError(request=None),
    telethon_errors.PasswordHashInvalidError(request=None),
    telethon_errors.CodeInvalidError(request=None),
    telethon_errors.SessionPasswordNeededError(request=None),
    "flood",
    telethon_errors.RPCError(request=None, message="boom", code=500),
    OSError("transport down"),
    ConnectionError("connection reset"),
    TimeoutError("read timed out"),
]


def _materialise(outcome: str | Exception) -> str | Exception:
    """Turn a strategy-chosen outcome token into a success sentinel or exception.

    Flood waits are rebuilt fresh each time; every other token is already an
    immutable sentinel or a reusable exception instance.
    """
    if outcome == "flood":
        return _flood_error()
    return outcome


_outcomes = st.sampled_from(_FAULTS)


@st.composite
def _scripts(draw: st.DrawFn) -> FakeClientScript:
    """Draw an arbitrary fault sequence across all five client call sites."""
    return FakeClientScript(
        connect=_materialise(draw(_outcomes)),
        send_code=_materialise(draw(_outcomes)),
        bot_sign_in=_materialise(draw(_outcomes)),
        code_sign_in=_materialise(draw(_outcomes)),
        password_sign_in=_materialise(draw(_outcomes)),
    )


#: Logins the adapter can be driven with: a bot token (``<id>:<secret>``) takes
#: the direct-sign-in path, a phone number takes the send-code path.
_logins = st.sampled_from(["+15551234567", "123456:ABCDEF-bot-token-secret"])

#: Codes/passwords fed to ``submit_code``; shape is irrelevant since the fake's
#: scripted outcome, not the value, decides success or failure.
_codes = st.text(min_size=0, max_size=8)


def _assert_only_typed(exc: BaseException) -> None:
    """Fail unless ``exc`` is one of the adapter's two permitted escape types."""
    assert isinstance(exc, TwoFactorFailedError | TelegramError), (
        "login adapter leaked a non-typed exception: "
        f"{type(exc).__module__}.{type(exc).__name__}: {exc}"
    )


@settings(max_examples=200, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(script=_scripts(), login=_logins, codes=st.lists(_codes, min_size=0, max_size=3))
async def test_no_telethon_exception_escapes(
    monkeypatch: pytest.MonkeyPatch,
    script: FakeClientScript,
    login: str,
    codes: Sequence[str],
) -> None:
    """Property 1: no raw Telethon/OS/transport exception escapes the adapter.

    For any fault sequence injected across ``connect``/``send_code``/``sign_in``
    during ``start_login`` and a run of ``submit_code`` calls, the only
    exceptions the adapter is allowed to raise are
    :class:`~gateway.app.admin.setup.TwoFactorFailedError` or a
    :class:`gateway.app.telegram.errors.TelegramError` subclass — never a raw
    ``telethon.errors.*``, ``OSError``, ``ConnectionError``, or ``TimeoutError``.

    Validates: Requirements 2.7
    """
    install_fake_client(monkeypatch, script, session=_populated_string_session())
    adapter = login_adapter.TelethonLoginAdapter(_make_settings())

    try:
        await adapter.start_login(api_id=12345, api_hash="feedface", login=login)
    except (TwoFactorFailedError, TelegramError):
        pass
    except Exception as exc:  # noqa: BLE001 - the whole point is to catch leaks
        _assert_only_typed(exc)

    for code in codes:
        try:
            await adapter.submit_code(code)
        except (TwoFactorFailedError, TelegramError):
            pass
        except Exception as exc:  # noqa: BLE001 - the whole point is to catch leaks
            _assert_only_typed(exc)

    # reset() must never raise regardless of client state (idempotent cleanup).
    await adapter.reset()


# ===========================================================================
# Happy-path example tests (sibling task 1.3) — append below this banner.
# Reuse FakeTelegramClient / FakeClientScript / install_fake_client above.
# ===========================================================================
#
# Validates: Requirements 2.2, 2.6 (Property 4: session credentials produced on
# success). These example tests pin the two success flows the setup wizard
# depends on: the bot-token direct sign-in and the phone + code + 2FA exchange.
# Both assert the adapter reports ``authenticated=True`` and hands back non-empty
# ``session_credentials`` serialised from the live session.


async def test_bot_token_login_succeeds_with_session_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bot-token login signs in directly and yields non-empty credentials.

    ``start_login`` with a ``<id>:<secret>`` bot token takes the direct-sign-in
    path: ``connect`` then ``sign_in(bot_token=...)`` both succeed, so the adapter
    returns ``authenticated=True``, ``needs_code=False``, and serialises the live
    (populated) session into a non-empty ``session_credentials`` string.

    Validates: Requirements 2.2, 2.6
    """
    install_fake_client(
        monkeypatch, FakeClientScript(), session=_populated_string_session()
    )
    adapter = login_adapter.TelethonLoginAdapter(_make_settings())

    result = await adapter.start_login(
        api_id=12345, api_hash="feedface", login="123456:ABCDEF-bot-token-secret"
    )

    assert result.authenticated is True
    assert result.needs_code is False
    assert result.session_credentials
    await adapter.reset()


async def test_phone_code_two_factor_login_succeeds_with_session_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The phone + code + 2FA exchange ends authenticated with credentials.

    Walks the full three-leg phone flow against a single live session:

    1. ``start_login`` with a phone number sends the code and returns
       ``needs_code=True`` (not yet authenticated, no credentials).
    2. The first ``submit_code`` (the login code) hits
       ``SessionPasswordNeededError`` from ``sign_in``, so the adapter asks for
       the 2FA password with another ``needs_code=True``.
    3. The second ``submit_code`` (the password) signs in via
       ``sign_in(password=...)`` and returns ``authenticated=True`` with the
       serialised, non-empty ``session_credentials``.

    Validates: Requirements 2.2, 2.6
    """
    script = FakeClientScript(
        code_sign_in=telethon_errors.SessionPasswordNeededError(request=None),
    )
    install_fake_client(monkeypatch, script, session=_populated_string_session())
    adapter = login_adapter.TelethonLoginAdapter(_make_settings())

    started = await adapter.start_login(
        api_id=12345, api_hash="feedface", login="+15551234567"
    )
    assert started.needs_code is True
    assert started.authenticated is False
    assert not started.session_credentials

    needs_password = await adapter.submit_code("12345")
    assert needs_password.needs_code is True
    assert needs_password.authenticated is False
    assert not needs_password.session_credentials

    authenticated = await adapter.submit_code("account-2fa-password")
    assert authenticated.authenticated is True
    assert authenticated.needs_code is False
    assert authenticated.session_credentials
    await adapter.reset()

"""Deterministic fault-injection harness for integration tests.

This module provides reusable *support* code — not tests — for the fault
injection suite (task 38). It lets integration tests drive the gateway against
a fake :class:`~gateway.app.telegram.port.TelegramPort` whose every Telegram
operation can be made to fail at a chosen point, so the crash points named in
the design's "Crash Recovery" and "Partial Commit Record Recovery" sections and
in Requirement 14 (durable commit semantics / crash safety) can be exercised
reproducibly. It also supports the reconnection behaviour of Requirement 3 by
letting a test flip the session between CONNECTED and RECONNECTING and raise
:class:`~gateway.app.telegram.errors.TelegramUnavailableError` while reconnecting.

Design intent
-------------
Requirement 14.1 fixes the Telegram write order for a generation — all Record
documents first, then the Blob Manifest, then the Commit Record — and says a
write counts as confirmed only once Telegram returns a ``message_id``. The
interesting crash points therefore sit *between* those confirmations:

- a crash mid-Record upload (STAGED/UPLOADING retained at restart — design
  "Crash Recovery", Requirement 14.2);
- a crash after the Blob Manifest is confirmed but before the Commit Record
  (design "Partial Commit Record Recovery", Requirement 14.3);
- a Telegram ``FLOOD_WAIT`` that back-off must honour before the generation
  eventually commits (design "Upload Retry");
- a disconnect / reconnection window during which Telegram is unavailable
  (Requirement 3.3).

:class:`FaultInjectingTelegramPort` wraps an in-memory message store (the same
role the Telethon adapter plays for a real channel) and exposes a small,
declarative configuration API — :meth:`FaultInjectingTelegramPort.fail_after`,
:meth:`FaultInjectingTelegramPort.fail_on_caption_prefix`,
:meth:`FaultInjectingTelegramPort.flood_wait_on_caption_prefix`,
:meth:`FaultInjectingTelegramPort.drop_after_send`,
:meth:`FaultInjectingTelegramPort.simulate_crash_after_manifest`,
:meth:`FaultInjectingTelegramPort.set_session_state` — plus call counters a
test can assert on. The port structurally conforms to :class:`TelegramPort`;
it is cast to the protocol by callers (the gateway seams accept the protocol).

The :class:`CaptionKind` enum and :data:`CAPTION_PREFIXES` mirror the design's
"Telegram Message Format" table so a test can target a fault at a specific
message role (data chunk vs. blob manifest vs. commit record vs. tombstone,
etc.) without hard-coding the raw ``tgo3-*`` strings.

This file deliberately defines no ``test_`` functions and must import and
collect cleanly as plain support code.
"""

from __future__ import annotations

import asyncio
import enum
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from gateway.app.telegram.errors import (
    FloodWaitError,
    TelegramUnavailableError,
)
from gateway.app.telegram.port import SessionState, TelegramMessage

__all__ = [
    "CAPTION_PREFIXES",
    "CaptionKind",
    "FaultInjectingTelegramPort",
    "StoredMessage",
    "classify_caption",
]


class CaptionKind(enum.Enum):
    """Logical role of a Telegram message, keyed by its caption prefix.

    Mirrors the design's "Telegram Message Format" table so tests can target a
    fault at a specific message role without embedding the raw ``tgo3-*``
    prefixes. :data:`CAPTION_PREFIXES` maps each kind to the exact prefix the
    gateway writes (see the ``_PREFIX_*`` constants in the S3 operation
    handlers and the format-layer module docstrings).
    """

    DATA_CHUNK = "data_chunk"
    BLOB_MANIFEST = "blob_manifest"
    COMMIT_RECORD = "commit_record"
    TOMBSTONE = "tombstone"
    BUCKET_MANIFEST = "bucket_manifest"
    CHECKPOINT = "checkpoint"


#: Caption prefix the gateway writes for each :class:`CaptionKind`, mirroring
#: the design's "Telegram Message Format" table:
#: ``tgo3-d-<blob_id>-<chunk_idx>`` (data chunk), ``tgo3-b-<blob_id>`` (blob
#: manifest), ``tgo3-c-<generation_id>`` (commit record),
#: ``tgo3-t-<generation_id>`` (tombstone), ``tgo3-k-<version>`` (bucket
#: manifest) and ``tgo3-p-<timestamp>`` (checkpoint).
CAPTION_PREFIXES: dict[CaptionKind, str] = {
    CaptionKind.DATA_CHUNK: "tgo3-d-",
    CaptionKind.BLOB_MANIFEST: "tgo3-b-",
    CaptionKind.COMMIT_RECORD: "tgo3-c-",
    CaptionKind.TOMBSTONE: "tgo3-t-",
    CaptionKind.BUCKET_MANIFEST: "tgo3-k-",
    CaptionKind.CHECKPOINT: "tgo3-p-",
}


def classify_caption(caption: str) -> CaptionKind | None:
    """Return the :class:`CaptionKind` a caption denotes, or ``None``.

    The data-chunk prefix (``tgo3-d-``) is a strict superstring of none of the
    others, and every prefix is mutually exclusive, so a simple longest-first
    match is unambiguous. Returns ``None`` for a caption that matches no known
    prefix (e.g. a free-form caption a test writes directly).

    Args:
        caption: The message caption to classify.

    Returns:
        The matching :class:`CaptionKind`, or ``None`` when nothing matches.
    """
    for kind, prefix in CAPTION_PREFIXES.items():
        if caption.startswith(prefix):
            return kind
    return None


@dataclass(frozen=True)
class StoredMessage:
    """A document retained in the fake port's in-memory channel store.

    Captures everything a test (or the gateway's read path) needs to read a
    message back: its identity and caption plus the raw document body. The
    ``dropped`` flag models a message whose ``message_id`` was returned to the
    caller but which never became durably readable — the "crash between manifest
    and commit" case where the gateway believes a write succeeded but the
    message is absent on restart.

    Attributes:
        message_id: Monotonic per-channel id, as a real channel assigns.
        channel_id: Channel the message belongs to.
        caption: The caption the gateway wrote; classify it with
            :func:`classify_caption`.
        file_name: Attached document file name.
        data: Raw document body as uploaded.
        dropped: ``True`` when the message was acknowledged to the caller but
            is not durably present (so :meth:`FaultInjectingTelegramPort.get_messages`
            and :meth:`FaultInjectingTelegramPort.download_document` behave as if
            it never landed).
    """

    message_id: int
    channel_id: int
    caption: str
    file_name: str
    data: bytes
    dropped: bool = False


@dataclass
class _SendFaultRule:
    """One configured failure for :meth:`FaultInjectingTelegramPort.send_document`.

    A rule fires when all of its (optional) predicates match a ``send_document``
    call. ``remaining`` bounds how many times it may fire (``None`` = unbounded)
    so a test can inject a transient fault that clears on retry — the pattern
    behind "FLOOD_WAIT honoured then upload commits".
    """

    error_factory: Callable[[], BaseException]
    caption_kind: CaptionKind | None = None
    at_call: int | None = None
    remaining: int | None = None


class FaultInjectingTelegramPort:
    """In-memory :class:`TelegramPort` fake that injects faults on demand.

    Backs each channel with a list of :class:`StoredMessage` objects and
    assigns monotonically increasing ``message_id`` values, exactly as a real
    channel does, so recovery-oriented reads (:meth:`get_messages`,
    :meth:`download_document`) see the same ordering the Recovery CLI relies on.
    By default every operation succeeds; a test layers faults on with the
    configuration methods below and then drives the gateway as usual.

    The fake conforms structurally to :class:`TelegramPort` (and additionally
    offers :meth:`delete_messages`, matching the Telethon adapter's GC surface).
    Callers pass it where the protocol is expected, casting as the gateway seams
    already do for other fakes.

    Fault configuration (all are additive and may be combined):

    - :meth:`fail_after` — raise on the Nth ``send_document`` call overall.
    - :meth:`fail_on_caption_prefix` / :meth:`fail_on_kind` — raise on sends of
      a given message role (data chunk, manifest, commit, …).
    - :meth:`flood_wait_on_caption_prefix` — raise :class:`FloodWaitError`,
      optionally only for the first ``times`` matching sends so back-off can be
      shown to recover.
    - :meth:`drop_after_send` / :meth:`simulate_crash_after_manifest` — return a
      ``message_id`` to the caller but keep the message out of the durable
      store, modelling a crash after an acknowledged write.
    - :meth:`set_session_state` — flip the reported session state; while
      :attr:`SessionState.RECONNECTING` or :attr:`SessionState.DISCONNECTED`,
      operations raise :class:`TelegramUnavailableError` (Requirement 3.3).

    Counters (:attr:`send_calls`, :attr:`sends_by_kind`, :attr:`download_calls`,
    :attr:`get_messages_calls`) let tests assert on observable behaviour, e.g.
    "exactly one Telegram upload for a deduplicated SHA-256".
    """

    def __init__(self, *, next_channel_id: int = 1000) -> None:
        """Create an empty fake with no faults configured.

        Args:
            next_channel_id: First channel id handed out by
                :meth:`create_channel`; incremented on each creation.
        """
        self._channels: dict[int, list[StoredMessage]] = {}
        self._next_channel_id = next_channel_id
        self._next_message_id = 1
        self._session_state: SessionState = SessionState.CONNECTED
        self._send_rules: list[_SendFaultRule] = []
        self._drop_next_kinds: set[CaptionKind] = set()
        self._drop_next_prefixes: list[str] = []

        # Observable counters for test assertions.
        self.send_calls: int = 0
        self.download_calls: int = 0
        self.get_messages_calls: int = 0
        self.sends_by_kind: Counter[CaptionKind] = Counter()
        self.sends_by_caption: list[str] = []

    # -- fault configuration ----------------------------------------------

    def fail_after(
        self,
        call_number: int,
        *,
        error: BaseException | None = None,
    ) -> None:
        """Fail the ``call_number``-th ``send_document`` call (1-indexed).

        Models a disconnect that lands mid-upload: with records sent one chunk
        per call, failing on call *N* leaves the generation STAGED/UPLOADING for
        the restart path to resume (design "Crash Recovery", Requirement 14.2).

        Args:
            call_number: 1-indexed send call that should raise.
            error: Exception to raise; defaults to
                :class:`TelegramUnavailableError`.
        """
        factory = _as_factory(error, _default_unavailable)
        self._send_rules.append(
            _SendFaultRule(error_factory=factory, at_call=call_number, remaining=1)
        )

    def fail_on_kind(
        self,
        kind: CaptionKind,
        *,
        error: BaseException | None = None,
        times: int | None = None,
    ) -> None:
        """Fail ``send_document`` for a given message role.

        Args:
            kind: Message role to fail on (e.g. :attr:`CaptionKind.COMMIT_RECORD`
                to exercise the partial-commit path of Requirement 14.3).
            error: Exception to raise; defaults to
                :class:`TelegramUnavailableError`.
            times: Fire at most this many times (``None`` = every matching send),
                letting a transient fault clear on retry.
        """
        factory = _as_factory(error, _default_unavailable)
        self._send_rules.append(
            _SendFaultRule(error_factory=factory, caption_kind=kind, remaining=times)
        )

    def fail_on_caption_prefix(
        self,
        prefix: str,
        *,
        error: BaseException | None = None,
        times: int | None = None,
    ) -> None:
        """Fail ``send_document`` for captions starting with ``prefix``.

        A thin wrapper over :meth:`fail_on_kind` that accepts the raw
        ``tgo3-*`` prefix a test may already hold. Unknown prefixes never match.

        Args:
            prefix: Caption prefix to match (e.g. ``"tgo3-c-"``).
            error: Exception to raise; defaults to
                :class:`TelegramUnavailableError`.
            times: Fire at most this many times (``None`` = every matching send).
        """
        kind = _kind_for_prefix(prefix)
        if kind is None:
            # Prefix matches no known role: register a never-firing rule so the
            # call is still well-defined and counters stay honest.
            return
        self.fail_on_kind(kind, error=error, times=times)

    def flood_wait_on_caption_prefix(
        self,
        prefix: str,
        *,
        retry_after: int = 1,
        times: int | None = 1,
    ) -> None:
        """Raise :class:`FloodWaitError` for sends matching ``prefix``.

        Defaults to firing once so a test can show back-off honouring the
        requested wait and the upload then committing (design "Upload Retry",
        Requirement 14.1).

        Args:
            prefix: Caption prefix to match (e.g. ``"tgo3-d-"``).
            retry_after: Seconds carried on the raised :class:`FloodWaitError`.
            times: Fire at most this many times (``None`` = every matching send).
        """
        kind = _kind_for_prefix(prefix)
        if kind is None:
            return
        self._send_rules.append(
            _SendFaultRule(
                error_factory=lambda: FloodWaitError(retry_after=retry_after),
                caption_kind=kind,
                remaining=times,
            )
        )

    def drop_after_send(self, kind: CaptionKind) -> None:
        """Acknowledge the next send of ``kind`` but keep it out of the store.

        The caller receives a ``message_id`` as if the write succeeded, but the
        message is not durably readable afterwards — the fake's model of a crash
        immediately after an acknowledged write. Fires once per registration.

        Args:
            kind: Message role whose next send should be acknowledged yet
                dropped.
        """
        self._drop_next_kinds.add(kind)

    def drop_after_send_prefix(self, prefix: str) -> None:
        """Acknowledge the next send whose caption starts with ``prefix``.

        The caption-prefix counterpart to :meth:`drop_after_send`, for tests
        that hold a raw ``tgo3-*`` prefix (or a fuller concrete caption prefix).
        The matching send returns a ``message_id`` but is not durably stored.
        Fires once per registration.

        Args:
            prefix: Caption prefix whose next matching send should be dropped.
        """
        self._drop_next_prefixes.append(prefix)

    def simulate_crash_after_manifest(self) -> None:
        """Model a crash *after* the Blob Manifest, *before* the Commit Record.

        Convenience for the design's "Partial Commit Record Recovery" point: the
        Blob Manifest is confirmed (its ``message_id`` returned and stored) while
        the subsequent Commit Record send is made to fail, so recovery sees a
        manifest with no matching commit (Requirement 14.3).
        """
        self.fail_on_kind(CaptionKind.COMMIT_RECORD, error=_default_unavailable())

    def set_session_state(self, state: SessionState) -> None:
        """Set the session state the port reports and enforces.

        While :attr:`SessionState.RECONNECTING` or
        :attr:`SessionState.DISCONNECTED`, every Telegram operation raises
        :class:`TelegramUnavailableError` so a test can assert the gateway
        surfaces a 503 without mutating committed state (Requirement 3.3). Flip
        back to :attr:`SessionState.CONNECTED` to let operations resume.

        Args:
            state: The session state to report from :meth:`get_session_state`
                and :meth:`is_connected`.
        """
        self._session_state = state

    def reset_counters(self) -> None:
        """Zero all observable counters without touching stored messages."""
        self.send_calls = 0
        self.download_calls = 0
        self.get_messages_calls = 0
        self.sends_by_kind = Counter()
        self.sends_by_caption = []

    # -- store inspection helpers -----------------------------------------

    def channel_exists(self, channel_id: int) -> bool:
        """Return whether a channel is present in the store.

        A channel is present once :meth:`create_channel` has handed out its id
        and absent after :meth:`delete_channel` removes it. Unlike inspecting
        :meth:`messages` (which is empty for both an *empty* and a *deleted*
        channel), this distinguishes a torn-down channel from a merely empty one
        — the signal a bucket-delete test needs to confirm the channel was
        actually removed.

        Args:
            channel_id: Channel id to probe.

        Returns:
            ``True`` while the channel exists, ``False`` once it is deleted or
            was never created.
        """
        return channel_id in self._channels

    def messages(self, channel_id: int, *, include_dropped: bool = False) -> list[StoredMessage]:
        """Return a channel's stored messages in ``message_id`` order.

        Args:
            channel_id: Channel to inspect.
            include_dropped: When ``True``, include messages that were
                acknowledged but dropped (see :meth:`drop_after_send`); by
                default they are hidden, matching what a reader would observe.

        Returns:
            The retained messages, ascending by ``message_id``.
        """
        stored = self._channels.get(channel_id, [])
        if include_dropped:
            return list(stored)
        return [m for m in stored if not m.dropped]

    def seed_message(
        self,
        channel_id: int,
        *,
        caption: str,
        data: bytes = b"",
        file_name: str = "seed.bin",
    ) -> int:
        """Insert a message directly, bypassing fault rules.

        Lets a test pre-populate a channel to a desired state (e.g. a manifest
        present with no commit) before driving the gateway's recovery path.

        Args:
            channel_id: Channel to insert into (created implicitly if absent).
            caption: Caption to record.
            data: Document body.
            file_name: Document file name.

        Returns:
            The assigned ``message_id``.
        """
        return self._store(channel_id, data=data, file_name=file_name, caption=caption)

    # -- TelegramPort surface ---------------------------------------------

    async def send_document(
        self, channel_id: int, data: bytes, file_name: str, caption: str
    ) -> int:
        """Upload ``data`` as a document, applying any configured faults.

        Honours the single-session invariant (raises while reconnecting),
        evaluates fault rules in registration order, and otherwise stores the
        message — unless a drop is pending for its role, in which case a
        ``message_id`` is returned but the message is not durably stored.
        """
        self._require_available()
        self.send_calls += 1
        kind = classify_caption(caption)
        self.sends_by_caption.append(caption)
        if kind is not None:
            self.sends_by_kind[kind] += 1

        self._maybe_raise_send_fault(kind)

        drop = self._should_drop(kind, caption)
        return self._store(
            channel_id, data=data, file_name=file_name, caption=caption, dropped=drop
        )

    async def get_messages(
        self, channel_id: int, ids: list[int]
    ) -> list[TelegramMessage]:
        """Return decoded messages for the requested ids, skipping dropped ones."""
        self._require_available()
        self.get_messages_calls += 1
        wanted = set(ids)
        result: list[TelegramMessage] = []
        for stored in self._channels.get(channel_id, []):
            if stored.message_id in wanted and not stored.dropped:
                result.append(_to_port_message(stored))
        return result

    async def download_document(self, channel_id: int, message_id: int) -> bytes:
        """Return a message's document body, or raise if it is absent/dropped."""
        self._require_available()
        self.download_calls += 1
        for stored in self._channels.get(channel_id, []):
            if stored.message_id == message_id and not stored.dropped:
                return stored.data
        raise TelegramUnavailableError(
            f"No document on message {message_id} in channel {channel_id}"
        )

    async def delete_messages(self, channel_id: int, message_ids: list[int]) -> None:
        """Delete messages from a channel (mirrors the adapter's GC surface)."""
        self._require_available()
        if not message_ids:
            return
        drop = set(message_ids)
        stored = self._channels.get(channel_id)
        if stored is None:
            return
        self._channels[channel_id] = [m for m in stored if m.message_id not in drop]

    async def create_channel(self, title: str) -> int:
        """Create a new empty channel and return its id."""
        self._require_available()
        channel_id = self._next_channel_id
        self._next_channel_id += 1
        self._channels.setdefault(channel_id, [])
        return channel_id

    async def delete_channel(self, channel_id: int) -> None:
        """Delete a channel and all its messages."""
        self._require_available()
        self._channels.pop(channel_id, None)

    async def get_session_state(self) -> SessionState:
        """Return the session state last set via :meth:`set_session_state`."""
        return self._session_state

    async def is_connected(self) -> bool:
        """Return whether the session is reported CONNECTED."""
        return self._session_state is SessionState.CONNECTED

    # -- internals ---------------------------------------------------------

    def _require_available(self) -> None:
        """Raise while the session is not CONNECTED (Requirement 3.3)."""
        if self._session_state is not SessionState.CONNECTED:
            raise TelegramUnavailableError(
                f"Telegram session unavailable: {self._session_state.value}"
            )

    def _maybe_raise_send_fault(self, kind: CaptionKind | None) -> None:
        """Fire the first matching, non-exhausted send-fault rule, if any."""
        for rule in self._send_rules:
            if rule.remaining is not None and rule.remaining <= 0:
                continue
            if rule.at_call is not None and rule.at_call != self.send_calls:
                continue
            if rule.caption_kind is not None and rule.caption_kind is not kind:
                continue
            if rule.remaining is not None:
                rule.remaining -= 1
            raise rule.error_factory()

    def _should_drop(self, kind: CaptionKind | None, caption: str) -> bool:
        """Return whether this send should be acknowledged but not stored."""
        if kind is not None and kind in self._drop_next_kinds:
            self._drop_next_kinds.discard(kind)
            return True
        for prefix in list(self._drop_next_prefixes):
            if caption.startswith(prefix):
                self._drop_next_prefixes.remove(prefix)
                return True
        return False

    def _store(
        self,
        channel_id: int,
        *,
        data: bytes,
        file_name: str,
        caption: str,
        dropped: bool = False,
    ) -> int:
        """Append a message to a channel and return its new ``message_id``."""
        message_id = self._next_message_id
        self._next_message_id += 1
        self._channels.setdefault(channel_id, []).append(
            StoredMessage(
                message_id=message_id,
                channel_id=channel_id,
                caption=caption,
                file_name=file_name,
                data=data,
                dropped=dropped,
            )
        )
        return message_id


def _to_port_message(stored: StoredMessage) -> TelegramMessage:
    """Project a :class:`StoredMessage` onto the port's value type.

    A document id is synthesised deterministically from the message id so reads
    carry stable, non-``None`` document metadata when a body is present, mirroring
    what the real adapter surfaces for the Recovery CLI.
    """
    has_document = bool(stored.data)
    return TelegramMessage(
        message_id=stored.message_id,
        caption=stored.caption,
        document_id=stored.message_id if has_document else None,
        file_reference=(f"ref-{stored.message_id}".encode() if has_document else None),
        dc_id=(2 if has_document else None),
    )


def _kind_for_prefix(prefix: str) -> CaptionKind | None:
    """Return the :class:`CaptionKind` whose caption prefix equals ``prefix``."""
    for kind, known in CAPTION_PREFIXES.items():
        if known == prefix:
            return kind
    # Fall back to classifying an example caption, so a longer concrete prefix
    # (e.g. a full ``tgo3-c-<id>``) still resolves to its role.
    return classify_caption(prefix)


def _default_unavailable() -> TelegramUnavailableError:
    """Build the default injected error for the send-fault paths."""
    return TelegramUnavailableError("Injected Telegram fault")


def _as_factory(
    error: BaseException | None,
    default: Callable[[], BaseException],
) -> Callable[[], BaseException]:
    """Return a zero-arg factory that yields ``error`` (or the default).

    A fixed instance is re-raised each time the rule fires; that is intentional
    for the test harness, where the identity of the error does not matter.
    """
    if error is None:
        return default
    return lambda: error


async def drive_with_timeout(
    coro: Awaitable[None],
    *,
    timeout: float = 5.0,
) -> None:
    """Await ``coro`` under a timeout so a wedged fault scenario fails fast.

    A convenience for tests that drive the gateway through a fault path which
    *should* make progress (e.g. back-off then commit); a hang surfaces as a
    :class:`asyncio.TimeoutError` instead of blocking the suite.

    Args:
        coro: The coroutine to await.
        timeout: Seconds to wait before giving up.
    """
    await asyncio.wait_for(coro, timeout=timeout)

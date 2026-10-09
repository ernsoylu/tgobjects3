"""Pydantic v2 models for the Telegram message format layer.

This module defines the typed, validated data models that are serialised to and
deserialised from msgpack by the per-type codec modules (``blob_manifest``,
``commit_record``, ``tombstone``, ``bucket_manifest``, ``checkpoint`` — task
7.2). The models themselves only describe *shape* and *validation*; the wire
encoding lives in the codec modules.

Every message type embeds a ``format_version`` field pinned to
:data:`FORMAT_VERSION` (currently ``1``). Models reject construction with any
other version at validation time, and codecs raise :class:`FormatVersionError`
when a deserialised payload carries an unsupported version so the gateway and
recovery CLI can report the exact value found without processing the content
(requirements 16.1–16.4).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Format version contract
# ---------------------------------------------------------------------------

#: The one storage format version this build understands. The format is frozen
#: at M1; any message whose ``format_version`` differs is rejected rather than
#: interpreted, so a future version can never be silently misread.
FORMAT_VERSION = 1


class FormatVersionError(ValueError):
    """Raised by codecs when a payload carries an unsupported format version.

    Carries the offending value so callers (gateway responses, Recovery CLI
    logs) can report the exact unsupported version found alongside the message
    identity (requirements 16.2, 16.4).
    """

    def __init__(self, found: int) -> None:
        self.found = found
        super().__init__(
            f"unsupported format_version {found!r}; this build only supports "
            f"version {FORMAT_VERSION}"
        )


def validate_format_version(found: int) -> int:
    """Validate a format version read from a deserialised payload.

    Returns the value unchanged when it equals :data:`FORMAT_VERSION`; raises
    :class:`FormatVersionError` otherwise. Codec ``deserialise`` functions call
    this before constructing a model so the rejection reports the raw value
    rather than surfacing an opaque validation error.
    """

    if found != FORMAT_VERSION:
        raise FormatVersionError(found)
    return found


# A ``format_version`` field pinned to the supported version. Declared with a
# frozen literal default and a range constraint so model construction with any
# other value fails validation (requirement 16.1).
FormatVersion = Annotated[int, Field(default=FORMAT_VERSION, ge=FORMAT_VERSION, le=FORMAT_VERSION)]


class _FormatModel(BaseModel):
    """Common configuration for every format-layer model.

    - ``frozen`` — messages are immutable once built; the format is a durable
      record, not mutable state.
    - ``extra="forbid"`` — unknown fields are a format error, not silently
      dropped, which keeps the round-trip contract tight.
    - ``strict`` — no implicit coercion (e.g. ``str`` into ``int``); values must
      already be of their declared type.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    format_version: FormatVersion


class BlobManifest(_FormatModel):
    """Describes the sealed records that make up one content-addressed blob.

    Written to the bucket channel (caption ``tgo3-b-<blob_id>``) after every
    data chunk is confirmed. ``blob_id`` is the 16-byte identifier bound into
    each record's AAD; ``key_wrapped`` is the per-blob AES key wrapped by the
    bucket KEK.
    """

    blob_id: bytes
    bucket_id: UUID
    sha256: bytes
    size: int = Field(ge=0)
    chunk_records: int = Field(ge=1)
    chunk_message_ids: list[int]
    key_wrapped: bytes


class CommitRecord(_FormatModel):
    """Durability boundary for a single object generation.

    Written last (caption ``tgo3-c-<generation_id>``); its presence in the
    channel is what makes a generation recoverable. Points at the BlobManifest
    message that holds the content for this generation.
    """

    generation_id: UUID
    bucket_id: UUID
    key: str
    generation_number: int = Field(ge=0)
    blob_manifest_message_id: int
    etag: str
    checksums: dict[str, str]
    content_type: str
    user_meta: dict[str, str]
    last_modified: datetime


class Tombstone(_FormatModel):
    """Marks an object generation as deleted.

    Written to the bucket channel (caption ``tgo3-t-<generation_id>``) on
    DeleteObject; during recovery it marks the referenced generation DELETED.
    """

    generation_id: UUID
    bucket_id: UUID
    key: str
    generation_number: int = Field(ge=0)


class BucketManifest(_FormatModel):
    """Carries a bucket's wrapped key-encryption key.

    Written on bucket creation and key rotation (caption ``tgo3-k-<version>``).
    ``kek_wrapped`` is the bucket KEK wrapped by the root key; ``kek_version``
    identifies the rotation generation.
    """

    bucket_name: str
    kek_wrapped: bytes
    kek_version: int = Field(ge=0)


class Checkpoint(_FormatModel):
    """Encrypted index snapshot written periodically to the system channel.

    Caption ``tgo3-p-<timestamp>``. ``pg_dump_enc`` is the encrypted PostgreSQL
    dump envelope; ``high_water`` records the per-channel scan high-water marks
    the Recovery CLI uses as its start point.
    """

    pg_dump_enc: bytes
    high_water: dict[str, int]

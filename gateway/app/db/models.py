"""SQLAlchemy 2 async ORM models for the tgobjects3 PostgreSQL index.

Column definitions mirror the PostgreSQL Schema ER diagram in the design
document exactly (types, nullability, constraints). All tables share the
declarative ``Base`` defined here. A ``pg_advisory_lock`` helper is provided
for the dedup path described in Requirement 6.10/6.11.

The index is a *cache* of durable state that lives in Telegram: it can be
rebuilt from the channel contents by the recovery CLI. These models therefore
describe the authoritative column layout but hold no business logic.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base for every tgobjects3 ORM model."""


class Bucket(Base):
    """An S3 namespace backed by a private Telegram channel."""

    __tablename__ = "buckets"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kek_wrapped: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    kek_version: Mapped[int] = mapped_column(Integer, nullable=False)
    durability: Mapped[str] = mapped_column(Text, nullable=False)
    created_by_us: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)


class ObjectHead(Base):
    """The current committed generation for a (bucket, key) pair."""

    __tablename__ = "object_heads"

    bucket_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("buckets.id"), nullable=False
    )
    key: Mapped[str] = mapped_column(Text, nullable=False)
    generation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("generations.id"), nullable=False
    )

    __table_args__ = (
        PrimaryKeyConstraint("bucket_id", "key", name="pk_object_heads"),
    )


class Generation(Base):
    """One logical write to a key: PutObject, completed multipart, or delete."""

    __tablename__ = "generations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    bucket_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("buckets.id"), nullable=False
    )
    key: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    blob_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("blobs.id"), nullable=True
    )
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    etag: Mapped[str | None] = mapped_column(Text, nullable=True)
    checksums: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    content_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_meta: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    last_modified: Mapped[datetime] = mapped_column(nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    commit_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    staging_path: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_generations_bucket_key", "bucket_id", "key"),
        Index("ix_generations_state", "state"),
    )


class Blob(Base):
    """The encrypted, deduplicated binary payload of one or more generations."""

    __tablename__ = "blobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    bucket_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("buckets.id"), nullable=False
    )
    sha256: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    key_wrapped: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    chunk_records: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    refcount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    manifest_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    __table_args__ = (
        # Dedup within a bucket is keyed on the content digest. The upload
        # worker takes a pg_advisory_lock on the sha256 before claiming, but
        # the unique index is the hard guarantee against duplicate blob rows.
        UniqueConstraint("bucket_id", "sha256", name="uq_blobs_bucket_sha256"),
    )


class Chunk(Base):
    """One Telegram document message holding a contiguous run of records."""

    __tablename__ = "chunks"

    blob_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("blobs.id"), nullable=False
    )
    idx: Mapped[int] = mapped_column(Integer, nullable=False)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    records: Mapped[int] = mapped_column(Integer, nullable=False)
    document_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    file_reference: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    dc_id: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("blob_id", "idx", name="pk_chunks"),
    )


class UploadJob(Base):
    """Tracks the Upload Worker's progress for a single generation."""

    __tablename__ = "upload_jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    generation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("generations.id"), nullable=False
    )
    state: Mapped[str] = mapped_column(Text, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_upload_jobs_state_next", "state", "next_attempt_at"),
    )


class MultipartUpload(Base):
    """An in-progress S3 multipart upload session."""

    __tablename__ = "multipart_uploads"

    upload_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    bucket_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("buckets.id"), nullable=False
    )
    key: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(nullable=False)


class MultipartPart(Base):
    """A single staged part of a multipart upload."""

    __tablename__ = "multipart_parts"

    upload_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("multipart_uploads.upload_id"), nullable=False
    )
    part_no: Mapped[int] = mapped_column(Integer, nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    etag: Mapped[str] = mapped_column(Text, nullable=False)
    checksum: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    staging_path: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("upload_id", "part_no", name="pk_multipart_parts"),
    )


class AccessKey(Base):
    """An S3 access key id with its encrypted secret and grants."""

    __tablename__ = "access_keys"

    access_key_id: Mapped[str] = mapped_column(Text, primary_key=True)
    secret_enc: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str | None] = mapped_column(Text, nullable=True)
    grants: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    disabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class Checkpoint(Base):
    """A recorded checkpoint message in the system channel."""

    __tablename__ = "checkpoints"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now()
    )
    high_water: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)


class AccessStat(Base):
    """Per-record cache ranking score used by the eviction policy."""

    __tablename__ = "access_stats"

    blob_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("blobs.id"), nullable=False
    )
    record_index: Mapped[int] = mapped_column(Integer, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    scored_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("blob_id", "record_index", name="pk_access_stats"),
    )


# Composite unique index on blobs(bucket_id, sha256) — declared explicitly in
# addition to the table-level UniqueConstraint so autogenerate and the recovery
# CLI both see a named index object. Postgres backs a UniqueConstraint with an
# index already; this named Index gives migrations a stable identifier.
Index(
    "ix_blobs_bucket_sha256",
    Blob.bucket_id,
    Blob.sha256,
    unique=True,
)


def _sha256_to_lock_key(sha256: bytes) -> int:
    """Map a 32-byte SHA-256 digest to a signed 64-bit advisory-lock key.

    PostgreSQL ``pg_advisory_lock`` takes a ``bigint``. We fold the first eight
    bytes of the digest into a signed 64-bit integer so that identical content
    always maps to the same lock key. Collisions across different digests only
    cost extra serialisation, never correctness, because the ``blobs``
    unique constraint remains the hard guarantee.
    """
    unsigned = int.from_bytes(sha256[:8], "big", signed=False)
    # Wrap into the signed bigint range expected by pg_advisory_lock.
    return unsigned - (1 << 64) if unsigned >= (1 << 63) else unsigned


async def pg_advisory_lock(session: AsyncSession, sha256: bytes) -> None:
    """Acquire a session-level PostgreSQL advisory lock for a content digest.

    Used by the dedup path (Requirement 6.10/6.11): the first worker to claim a
    SHA-256 holds the lock while it uploads; concurrent workers for the same
    content block here until it is released. The lock is tied to the database
    *session*, so it must be released with :func:`pg_advisory_unlock` (or by
    closing the connection).
    """
    from sqlalchemy import text

    await session.execute(
        text("SELECT pg_advisory_lock(:k)"),
        {"k": _sha256_to_lock_key(sha256)},
    )


async def pg_advisory_unlock(session: AsyncSession, sha256: bytes) -> bool:
    """Release the advisory lock taken by :func:`pg_advisory_lock`.

    Returns ``True`` if a lock held by this session was released, ``False`` if
    no matching lock was held.
    """
    from sqlalchemy import text

    result = await session.execute(
        text("SELECT pg_advisory_unlock(:k)"),
        {"k": _sha256_to_lock_key(sha256)},
    )
    return bool(result.scalar_one())


__all__ = [
    "Base",
    "Bucket",
    "ObjectHead",
    "Generation",
    "Blob",
    "Chunk",
    "UploadJob",
    "MultipartUpload",
    "MultipartPart",
    "AccessKey",
    "Checkpoint",
    "AccessStat",
    "pg_advisory_lock",
    "pg_advisory_unlock",
]

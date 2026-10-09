"""initial_schema

Create the full tgobjects3 PostgreSQL index: all 11 tables from the ER diagram
in the design document, with every foreign key and index declared explicitly.

The schema was first produced with ``alembic revision --autogenerate`` against
the SQLAlchemy 2 models in ``gateway/app/db/models.py`` and then refined so that
every foreign-key constraint carries a stable, explicit name (autogenerate
emits anonymous FKs). Named constraints matter here because the index is a
*rebuildable cache*: the recovery CLI, operational tooling, and later migrations
all need deterministic identifiers to reference, drop, or recreate constraints.

Foreign keys (named ``fk_<table>_<column>_<referenced-table>``):

* object_heads.bucket_id      -> buckets.id
* object_heads.generation_id  -> generations.id
* generations.bucket_id       -> buckets.id
* generations.blob_id         -> blobs.id          (nullable: delete markers)
* blobs.bucket_id             -> buckets.id
* chunks.blob_id              -> blobs.id
* upload_jobs.generation_id   -> generations.id
* multipart_uploads.bucket_id -> buckets.id
* multipart_parts.upload_id   -> multipart_uploads.upload_id
* access_stats.blob_id        -> blobs.id

Indexes (declared in the models, created explicitly here):

* uq_blobs_bucket_sha256   UNIQUE constraint on blobs(bucket_id, sha256)
* ix_blobs_bucket_sha256   UNIQUE index   on blobs(bucket_id, sha256)
* ix_generations_bucket_key   on generations(bucket_id, key)
* ix_generations_state        on generations(state)
* ix_upload_jobs_state_next   on upload_jobs(state, next_attempt_at)

Revision ID: 21748135401f
Revises:
Create Date: 2026-10-09 15:06:39.233721+00:00

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "21748135401f"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- buckets: the root namespace table; everything else references it. ---
    op.create_table(
        "buckets",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("kek_wrapped", sa.LargeBinary(), nullable=False),
        sa.Column("kek_version", sa.Integer(), nullable=False),
        sa.Column("durability", sa.Text(), nullable=False),
        sa.Column("created_by_us", sa.Boolean(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_buckets"),
        sa.UniqueConstraint("name", name="uq_buckets_name"),
    )

    # --- blobs: deduplicated encrypted payloads, owned by a bucket. ---
    op.create_table(
        "blobs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("bucket_id", sa.UUID(), nullable=False),
        sa.Column("sha256", sa.LargeBinary(), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("key_wrapped", sa.LargeBinary(), nullable=False),
        sa.Column("chunk_records", sa.Integer(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("refcount", sa.Integer(), nullable=False),
        sa.Column("manifest_message_id", sa.BigInteger(), nullable=True),
        sa.ForeignKeyConstraint(
            ["bucket_id"], ["buckets.id"], name="fk_blobs_bucket_id_buckets"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_blobs"),
        sa.UniqueConstraint("bucket_id", "sha256", name="uq_blobs_bucket_sha256"),
    )
    op.create_index(
        "ix_blobs_bucket_sha256", "blobs", ["bucket_id", "sha256"], unique=True
    )

    # --- generations: one logical write to a key (put / multipart / delete). ---
    op.create_table(
        "generations",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("bucket_id", sa.UUID(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("blob_id", sa.UUID(), nullable=True),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("etag", sa.Text(), nullable=True),
        sa.Column("checksums", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("content_type", sa.Text(), nullable=True),
        sa.Column("user_meta", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("last_modified", sa.DateTime(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("commit_message_id", sa.BigInteger(), nullable=True),
        sa.Column("staging_path", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["bucket_id"], ["buckets.id"], name="fk_generations_bucket_id_buckets"
        ),
        sa.ForeignKeyConstraint(
            ["blob_id"], ["blobs.id"], name="fk_generations_blob_id_blobs"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_generations"),
    )
    op.create_index(
        "ix_generations_bucket_key", "generations", ["bucket_id", "key"], unique=False
    )
    op.create_index("ix_generations_state", "generations", ["state"], unique=False)

    # --- object_heads: current committed generation for a (bucket, key). ---
    op.create_table(
        "object_heads",
        sa.Column("bucket_id", sa.UUID(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("generation_id", sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(
            ["bucket_id"], ["buckets.id"], name="fk_object_heads_bucket_id_buckets"
        ),
        sa.ForeignKeyConstraint(
            ["generation_id"],
            ["generations.id"],
            name="fk_object_heads_generation_id_generations",
        ),
        sa.PrimaryKeyConstraint("bucket_id", "key", name="pk_object_heads"),
    )

    # --- chunks: one Telegram document per contiguous run of records. ---
    op.create_table(
        "chunks",
        sa.Column("blob_id", sa.UUID(), nullable=False),
        sa.Column("idx", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("records", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.BigInteger(), nullable=False),
        sa.Column("file_reference", sa.LargeBinary(), nullable=False),
        sa.Column("dc_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["blob_id"], ["blobs.id"], name="fk_chunks_blob_id_blobs"
        ),
        sa.PrimaryKeyConstraint("blob_id", "idx", name="pk_chunks"),
    )

    # --- upload_jobs: Upload Worker progress for a single generation. ---
    op.create_table(
        "upload_jobs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("generation_id", sa.UUID(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["generation_id"],
            ["generations.id"],
            name="fk_upload_jobs_generation_id_generations",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_upload_jobs"),
    )
    op.create_index(
        "ix_upload_jobs_state_next",
        "upload_jobs",
        ["state", "next_attempt_at"],
        unique=False,
    )

    # --- multipart_uploads: in-progress S3 multipart sessions. ---
    op.create_table(
        "multipart_uploads",
        sa.Column("upload_id", sa.UUID(), nullable=False),
        sa.Column("bucket_id", sa.UUID(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["bucket_id"],
            ["buckets.id"],
            name="fk_multipart_uploads_bucket_id_buckets",
        ),
        sa.PrimaryKeyConstraint("upload_id", name="pk_multipart_uploads"),
    )

    # --- multipart_parts: staged parts of a multipart upload. ---
    op.create_table(
        "multipart_parts",
        sa.Column("upload_id", sa.UUID(), nullable=False),
        sa.Column("part_no", sa.Integer(), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("etag", sa.Text(), nullable=False),
        sa.Column("checksum", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("staging_path", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["upload_id"],
            ["multipart_uploads.upload_id"],
            name="fk_multipart_parts_upload_id_multipart_uploads",
        ),
        sa.PrimaryKeyConstraint("upload_id", "part_no", name="pk_multipart_parts"),
    )

    # --- access_keys: S3 credentials with encrypted secrets. ---
    op.create_table(
        "access_keys",
        sa.Column("access_key_id", sa.Text(), nullable=False),
        sa.Column("secret_enc", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column("grants", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("disabled", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("access_key_id", name="pk_access_keys"),
    )

    # --- checkpoints: recorded checkpoint messages in the system channel. ---
    op.create_table(
        "checkpoints",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("high_water", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_checkpoints"),
    )

    # --- access_stats: per-record cache ranking scores. ---
    op.create_table(
        "access_stats",
        sa.Column("blob_id", sa.UUID(), nullable=False),
        sa.Column("record_index", sa.Integer(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("scored_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["blob_id"], ["blobs.id"], name="fk_access_stats_blob_id_blobs"
        ),
        sa.PrimaryKeyConstraint("blob_id", "record_index", name="pk_access_stats"),
    )


def downgrade() -> None:
    # Drop in reverse dependency order so foreign keys never block a drop.
    op.drop_table("access_stats")
    op.drop_table("checkpoints")
    op.drop_table("access_keys")
    op.drop_table("multipart_parts")
    op.drop_table("multipart_uploads")
    op.drop_index("ix_upload_jobs_state_next", table_name="upload_jobs")
    op.drop_table("upload_jobs")
    op.drop_table("chunks")
    op.drop_table("object_heads")
    op.drop_index("ix_generations_state", table_name="generations")
    op.drop_index("ix_generations_bucket_key", table_name="generations")
    op.drop_table("generations")
    op.drop_index("ix_blobs_bucket_sha256", table_name="blobs")
    op.drop_table("blobs")
    op.drop_table("buckets")

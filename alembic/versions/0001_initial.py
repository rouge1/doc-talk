"""initial truth store: files + jobs ledger

Revision ID: 0001
Revises:
Create Date: 2026-06-06
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "files",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("path", sa.String(length=1024), nullable=False),
        sa.Column("filename", sa.String(length=512), nullable=False),
        sa.Column("format", sa.String(length=32), nullable=False),
        sa.Column("mime", sa.String(length=128), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("content_hash"),
    )
    op.create_index("ix_files_content_hash", "files", ["content_hash"], unique=True)
    op.create_index("ix_files_format_size", "files", ["format", "byte_size"])
    op.create_index("ix_files_mime", "files", ["mime"])

    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("stage", sa.String(length=64), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.Enum("pending", "running", "done", "error", native_enum=False, length=16),
            nullable=False,
        ),
        sa.Column("model_version", sa.String(length=64), nullable=False),
        sa.Column("params", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("input_hash"),
    )
    op.create_index("ix_jobs_content_hash", "jobs", ["content_hash"])
    op.create_index("ix_jobs_input_hash", "jobs", ["input_hash"], unique=True)
    op.create_index("ix_jobs_hash_stage", "jobs", ["content_hash", "stage"])


def downgrade() -> None:
    op.drop_index("ix_jobs_hash_stage", table_name="jobs")
    op.drop_index("ix_jobs_input_hash", table_name="jobs")
    op.drop_index("ix_jobs_content_hash", table_name="jobs")
    op.drop_table("jobs")
    op.drop_index("ix_files_mime", table_name="files")
    op.drop_index("ix_files_format_size", table_name="files")
    op.drop_index("ix_files_content_hash", table_name="files")
    op.drop_table("files")

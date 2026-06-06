"""phase 1 document structure: chapters + chunks + links

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-06
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chapters",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("file_id", sa.Integer(), nullable=False),
        sa.Column("parent_id", sa.Integer(), nullable=True),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("ord", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=1024), nullable=False),
        sa.Column("page_start", sa.Integer(), nullable=False),
        sa.Column("page_end", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.ForeignKeyConstraint(["file_id"], ["files.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["parent_id"], ["chapters.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_chapters_file_id", "chapters", ["file_id"])
    op.create_index("ix_chapters_file_ord", "chapters", ["file_id", "ord"])

    op.create_table(
        "chunks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("file_id", sa.Integer(), nullable=False),
        sa.Column("chapter_id", sa.Integer(), nullable=True),
        sa.Column("page", sa.Integer(), nullable=False),
        sa.Column("ord", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("char_count", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["file_id"], ["files.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["chapter_id"], ["chapters.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_chunks_file_id", "chunks", ["file_id"])
    op.create_index("ix_chunks_chapter_id", "chunks", ["chapter_id"])
    op.create_index("ix_chunks_file_page", "chunks", ["file_id", "page"])

    op.create_table(
        "links",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("file_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("src_page", sa.Integer(), nullable=False),
        sa.Column("dst_page", sa.Integer(), nullable=False),
        sa.Column("src_chapter_id", sa.Integer(), nullable=True),
        sa.Column("dst_chapter_id", sa.Integer(), nullable=True),
        sa.Column("target_label", sa.String(length=1024), nullable=True),
        sa.Column("score", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["file_id"], ["files.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["src_chapter_id"], ["chapters.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["dst_chapter_id"], ["chapters.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_links_file_id", "links", ["file_id"])
    op.create_index("ix_links_file_kind", "links", ["file_id", "kind"])


def downgrade() -> None:
    op.drop_index("ix_links_file_kind", table_name="links")
    op.drop_index("ix_links_file_id", table_name="links")
    op.drop_table("links")
    op.drop_index("ix_chunks_file_page", table_name="chunks")
    op.drop_index("ix_chunks_chapter_id", table_name="chunks")
    op.drop_index("ix_chunks_file_id", table_name="chunks")
    op.drop_table("chunks")
    op.drop_index("ix_chapters_file_ord", table_name="chapters")
    op.drop_index("ix_chapters_file_id", table_name="chapters")
    op.drop_table("chapters")

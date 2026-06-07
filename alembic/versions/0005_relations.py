"""phase 2: semantic relations (cross-corpus links)

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-06
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "relations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("src_chapter_id", sa.Integer(), nullable=True),
        sa.Column("src_image_id", sa.Integer(), nullable=True),
        sa.Column("dst_chapter_id", sa.Integer(), nullable=False),
        sa.Column("src_file_id", sa.Integer(), nullable=False),
        sa.Column("dst_file_id", sa.Integer(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["src_chapter_id"], ["chapters.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["dst_chapter_id"], ["chapters.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["src_image_id"], ["files.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["src_file_id"], ["files.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["dst_file_id"], ["files.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_relations_src_chapter_id", "relations", ["src_chapter_id"])
    op.create_index("ix_relations_dst_chapter_id", "relations", ["dst_chapter_id"])
    op.create_index("ix_relations_src_file", "relations", ["src_file_id"])


def downgrade() -> None:
    op.drop_index("ix_relations_src_file", table_name="relations")
    op.drop_index("ix_relations_dst_chapter_id", table_name="relations")
    op.drop_index("ix_relations_src_chapter_id", table_name="relations")
    op.drop_table("relations")

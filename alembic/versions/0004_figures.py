"""phase 1: figures/tables table

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-06
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "figures",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("file_id", sa.Integer(), nullable=False),
        sa.Column("page", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("ord", sa.Integer(), nullable=False),
        sa.Column("bbox", sa.String(length=64), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("image_path", sa.String(length=1024), nullable=True),
        sa.Column("table_md", sa.Text(), nullable=True),
        sa.Column("caption", sa.String(length=1024), nullable=True),
        sa.Column("vlm_description", sa.Text(), nullable=True),
        sa.Column("ocr_text", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["file_id"], ["files.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_figures_file_id", "figures", ["file_id"])
    op.create_index("ix_figures_file_page", "figures", ["file_id", "page"])


def downgrade() -> None:
    op.drop_index("ix_figures_file_page", table_name="figures")
    op.drop_index("ix_figures_file_id", table_name="figures")
    op.drop_table("figures")

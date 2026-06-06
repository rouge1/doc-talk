"""phase 1 image half: images table

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-06
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "images",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("file_id", sa.Integer(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("vlm_description", sa.Text(), nullable=True),
        sa.Column("ocr_text", sa.Text(), nullable=True),
        sa.Column("exif_datetime", sa.DateTime(), nullable=True),
        sa.Column("gps_lat", sa.Float(), nullable=True),
        sa.Column("gps_lon", sa.Float(), nullable=True),
        sa.Column("geo_country", sa.String(length=64), nullable=True),
        sa.Column("geo_place", sa.String(length=256), nullable=True),
        sa.Column("is_floorplan", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("cluster_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["file_id"], ["files.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("file_id"),
    )
    op.create_index("ix_images_file_id", "images", ["file_id"], unique=True)
    op.create_index("ix_images_geo_time", "images", ["geo_country", "exif_datetime"])


def downgrade() -> None:
    op.drop_index("ix_images_geo_time", table_name="images")
    op.drop_index("ix_images_file_id", table_name="images")
    op.drop_table("images")

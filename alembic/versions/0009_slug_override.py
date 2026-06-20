"""phase 4: per-entity slug override (disambiguate genuinely-distinct slug collisions)

Revision ID: 0009
Revises: 0008
Create Date: 2026-06-20
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Explicit page slug for an entity whose derived slug would collide with a *distinct* sibling.
    # Nullable — only the disambiguated loser carries one; everyone else derives the slug from
    # norm_key as before (no backfill, no churn to existing pages).
    op.add_column("entities", sa.Column("slug", sa.String(length=512), nullable=True))


def downgrade() -> None:
    op.drop_column("entities", "slug")

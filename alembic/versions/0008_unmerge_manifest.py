"""phase 4: undo manifest for entity merges (the moved claims/mentions an unmerge repoints back)

Revision ID: 0008
Revises: 0007
Create Date: 2026-06-20
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The manifest a merge writes so it can be reversed exactly: which claim/mention ids it repointed
    # into the survivor and which aliases/acronyms it contributed. Nullable — existing merge rows
    # predate undo tracking and stay un-reversible (the undo path refuses them, rather than guessing).
    op.add_column("entity_merges", sa.Column("moved", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("entity_merges", "moved")

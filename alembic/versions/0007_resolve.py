"""phase 4: entity resolution (status/acronyms/embedding, review queue, merges)

Revision ID: 0007
Revises: 0006
Create Date: 2026-06-07
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("entities", sa.Column("acronyms", sa.JSON(), nullable=True))
    op.add_column(
        "entities",
        sa.Column("glossary_defined", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column("entities", sa.Column("name_embedding_id", sa.Integer(), nullable=True))
    op.add_column(
        "entities",
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
    )

    op.add_column("mentions", sa.Column("score", sa.Float(), nullable=True))
    op.add_column("mentions", sa.Column("decision", sa.String(length=16), nullable=True))
    op.add_column("mentions", sa.Column("signals", sa.JSON(), nullable=True))

    op.create_table(
        "entity_review",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("mention_surface", sa.String(length=512), nullable=False),
        sa.Column("mention_type", sa.String(length=32), nullable=False),
        sa.Column("file_id", sa.Integer(), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("llm_verdict", sa.String(length=16), nullable=True),
        sa.Column("human_verdict", sa.String(length=16), nullable=True),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["file_id"], ["files.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["entity_id"], ["entities.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_entity_review_file_id", "entity_review", ["file_id"])

    op.create_table(
        "entity_merges",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("from_id", sa.Integer(), nullable=False),
        sa.Column("into_id", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=256), nullable=False),
        sa.Column("committed_sha", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_entity_merges_from_id", "entity_merges", ["from_id"])
    op.create_index("ix_entity_merges_into_id", "entity_merges", ["into_id"])


def downgrade() -> None:
    op.drop_table("entity_merges")
    op.drop_index("ix_entity_review_file_id", table_name="entity_review")
    op.drop_table("entity_review")
    op.drop_column("mentions", "signals")
    op.drop_column("mentions", "decision")
    op.drop_column("mentions", "score")
    op.drop_column("entities", "status")
    op.drop_column("entities", "name_embedding_id")
    op.drop_column("entities", "glossary_defined")
    op.drop_column("entities", "acronyms")

"""phase 4: synthesis layer (entities, wiki_pages, claims, claim_sources, mentions)

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-07
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "entities",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=512), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("norm_key", sa.String(length=512), nullable=False),
        sa.Column("aliases", sa.JSON(), nullable=True),
        sa.Column("wiki_path", sa.String(length=1024), nullable=True),
        sa.Column("embedding_id", sa.Integer(), nullable=True),
        sa.Column("source_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_entities_name_type", "entities", ["name", "type"], unique=True)
    op.create_index("ix_entities_normkey_type", "entities", ["norm_key", "type"])

    op.create_table(
        "wiki_pages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("path", sa.String(length=1024), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=True),
        sa.Column("source_count", sa.Integer(), nullable=False),
        sa.Column("last_synth_at", sa.DateTime(), nullable=True),
        sa.Column("md_hash", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["entity_id"], ["entities.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("path"),
    )

    op.create_table(
        "claims",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("wiki_page_id", sa.Integer(), nullable=True),
        sa.Column("file_id", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["entity_id"], ["entities.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["wiki_page_id"], ["wiki_pages.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["file_id"], ["files.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_claims_entity_id", "claims", ["entity_id"])
    op.create_index("ix_claims_file_id", "claims", ["file_id"])

    op.create_table(
        "claim_sources",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("claim_id", sa.Integer(), nullable=False),
        sa.Column("chunk_id", sa.Integer(), nullable=True),
        sa.Column("file_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["claim_id"], ["claims.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["chunk_id"], ["chunks.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["file_id"], ["files.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_claim_sources_claim_id", "claim_sources", ["claim_id"])
    op.create_index("ix_claim_sources_file_id", "claim_sources", ["file_id"])

    op.create_table(
        "mentions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("file_id", sa.Integer(), nullable=False),
        sa.Column("chunk_id", sa.Integer(), nullable=True),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["file_id"], ["files.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["chunk_id"], ["chunks.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["entity_id"], ["entities.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_mentions_file_id", "mentions", ["file_id"])
    op.create_index("ix_mentions_entity_id", "mentions", ["entity_id"])


def downgrade() -> None:
    op.drop_table("mentions")
    op.drop_table("claim_sources")
    op.drop_table("claims")
    op.drop_table("wiki_pages")
    op.drop_index("ix_entities_normkey_type", table_name="entities")
    op.drop_index("ix_entities_name_type", table_name="entities")
    op.drop_table("entities")

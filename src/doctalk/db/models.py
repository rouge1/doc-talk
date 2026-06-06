"""SQLAlchemy 2.0 models for the metadata truth store.

Phase 0 defines the two foundational tables: ``files`` (the authoritative source row, keyed by
the blake3 ``content_hash``) and ``jobs`` (the resumability ledger). Phase 1 adds chapters,
figures, images, chunks, wiki_nodes, and links — all of which join back to ``files`` by stable
key. Types are kept portable (no MySQL-only columns yet) so the same schema runs on SQLite for
fast idempotency tests while MySQL remains the production store.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    Index,
    JSON,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# blake3 hex digests are 256-bit -> 64 hex chars.
HASH_LEN = 64


def utcnow() -> datetime:
    """Naive UTC timestamp for application-set columns (matches the naive DateTime columns)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class File(Base):
    """An ingested source file. ``content_hash`` UNIQUE makes a re-drop a no-op."""

    __tablename__ = "files"

    id: Mapped[int] = mapped_column(primary_key=True)
    content_hash: Mapped[str] = mapped_column(String(HASH_LEN), unique=True, index=True)
    path: Mapped[str] = mapped_column(String(1024))      # last-seen absolute path
    filename: Mapped[str] = mapped_column(String(512))
    format: Mapped[str] = mapped_column(String(32))      # e.g. "pdf", "png", "docx"
    mime: Mapped[str] = mapped_column(String(128))
    byte_size: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_files_format_size", "format", "byte_size"),
        Index("ix_files_mime", "mime"),
    )


class JobStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    done = "done"
    error = "error"


class Job(Base):
    """One row per (source, stage, model_version, params) — the idempotency ledger.

    ``input_hash`` already encodes content_hash + stage + model_version + params, so it is
    globally unique; ``(content_hash, stage)`` is indexed for resumability queries. A ``done``
    row means "skip"; anything else (missing/running/error) is re-runnable.
    """

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    content_hash: Mapped[str] = mapped_column(String(HASH_LEN), index=True)
    stage: Mapped[str] = mapped_column(String(64))
    input_hash: Mapped[str] = mapped_column(String(HASH_LEN), unique=True, index=True)
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, native_enum=False, length=16), default=JobStatus.pending
    )
    model_version: Mapped[str] = mapped_column(String(64), default="")
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (Index("ix_jobs_hash_stage", "content_hash", "stage"),)

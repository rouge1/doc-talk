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
    Float,
    ForeignKey,
    Index,
    Integer,
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


# --- Phase 1: document structure (all join back to files by file_id) -------


class Chapter(Base):
    """A node in a document's outline tree. ``parent_id`` gives the hierarchy; ``ord`` is the
    document (TOC) order; ``page_start``/``page_end`` bound the section for navigation."""

    __tablename__ = "chapters"

    id: Mapped[int] = mapped_column(primary_key=True)
    file_id: Mapped[int] = mapped_column(ForeignKey("files.id", ondelete="CASCADE"), index=True)
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("chapters.id", ondelete="CASCADE"), nullable=True
    )
    level: Mapped[int] = mapped_column(Integer)
    ord: Mapped[int] = mapped_column(Integer)          # position in TOC / document order
    title: Mapped[str] = mapped_column(String(1024))
    page_start: Mapped[int] = mapped_column(Integer)   # 1-based, human/citation facing
    page_end: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(16), default="outline")  # outline|heading_detect

    __table_args__ = (Index("ix_chapters_file_ord", "file_id", "ord"),)


class Chunk(Base):
    """Retrieval unit for chat. Carries file/chapter/page so an answer can cite a real location."""

    __tablename__ = "chunks"

    id: Mapped[int] = mapped_column(primary_key=True)
    file_id: Mapped[int] = mapped_column(ForeignKey("files.id", ondelete="CASCADE"), index=True)
    chapter_id: Mapped[int | None] = mapped_column(
        ForeignKey("chapters.id", ondelete="SET NULL"), nullable=True, index=True
    )
    page: Mapped[int] = mapped_column(Integer)         # 1-based
    ord: Mapped[int] = mapped_column(Integer)          # global chunk order within the file
    text: Mapped[str] = mapped_column(Text)
    char_count: Mapped[int] = mapped_column(Integer)

    __table_args__ = (Index("ix_chunks_file_page", "file_id", "page"),)


class Link(Base):
    """A cross-reference. Phase 1 fills ``internal_pdf`` (resolved PDF GOTO links); Phase 2 adds
    semantic / shared-geo / shared-time / figure_ref kinds."""

    __tablename__ = "links"

    id: Mapped[int] = mapped_column(primary_key=True)
    file_id: Mapped[int] = mapped_column(ForeignKey("files.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    src_page: Mapped[int] = mapped_column(Integer)
    dst_page: Mapped[int] = mapped_column(Integer)
    src_chapter_id: Mapped[int | None] = mapped_column(
        ForeignKey("chapters.id", ondelete="SET NULL"), nullable=True
    )
    dst_chapter_id: Mapped[int | None] = mapped_column(
        ForeignKey("chapters.id", ondelete="SET NULL"), nullable=True
    )
    target_label: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    score: Mapped[float] = mapped_column(Float, default=1.0)

    __table_args__ = (Index("ix_links_file_kind", "file_id", "kind"),)


class Figure(Base):
    """A figure or table extracted from a document page. Tables carry ``table_md`` (PyMuPDF
    markdown); figures carry an ``image_path`` to the raster on disk (under ``figures_dir``).
    Both can gain a ``vlm_description`` (later batch) and ``ocr_text`` (Tesseract). Joins back to
    ``files`` by ``file_id`` and to a page; the chapter is derivable from the page via the outline."""

    __tablename__ = "figures"

    id: Mapped[int] = mapped_column(primary_key=True)
    file_id: Mapped[int] = mapped_column(ForeignKey("files.id", ondelete="CASCADE"), index=True)
    page: Mapped[int] = mapped_column(Integer)          # 1-based
    kind: Mapped[str] = mapped_column(String(16))       # "figure" | "table"
    ord: Mapped[int] = mapped_column(Integer)           # extraction order within the file
    bbox: Mapped[str | None] = mapped_column(String(64), nullable=True)  # "x0,y0,x1,y1"
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    image_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)  # figures only
    table_md: Mapped[str | None] = mapped_column(Text, nullable=True)            # tables only
    caption: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    vlm_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    ocr_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (Index("ix_figures_file_page", "file_id", "page"),)


class Image(Base):
    """Per-image derived metadata for a standalone photo (or, later, a figure extracted from a
    PDF). One row per image file, joined to ``files`` by ``file_id``. Format/byte_size live on the
    file; this holds the image-specific signals the gallery + hybrid search need."""

    __tablename__ = "images"

    id: Mapped[int] = mapped_column(primary_key=True)
    file_id: Mapped[int] = mapped_column(
        ForeignKey("files.id", ondelete="CASCADE"), unique=True, index=True
    )
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    vlm_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    ocr_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    exif_datetime: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    gps_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    gps_lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    geo_country: Mapped[str | None] = mapped_column(String(64), nullable=True)
    geo_place: Mapped[str | None] = mapped_column(String(256), nullable=True)
    is_floorplan: Mapped[bool] = mapped_column(default=False)
    cluster_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # Phase 2

    __table_args__ = (Index("ix_images_geo_time", "geo_country", "exif_datetime"),)

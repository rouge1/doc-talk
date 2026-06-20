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


class Relation(Base):
    """A semantic edge across the corpus: a source (a chapter, or an image via its VLM
    description) relates to a target document section (chapter), scored by embedding similarity.
    Unlike ``Link`` (PDF-internal, page→page), relations connect *different* documents and attach
    images to relevant sections — the Phase-2 cross-linking layer. Directed src→dst; the UI reads
    both directions. ``src_file_id`` makes "clear this file's relations" a cheap delete."""

    __tablename__ = "relations"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), default="semantic")
    src_chapter_id: Mapped[int | None] = mapped_column(
        ForeignKey("chapters.id", ondelete="CASCADE"), nullable=True, index=True
    )
    src_image_id: Mapped[int | None] = mapped_column(  # an image's file_id (no chapters)
        ForeignKey("files.id", ondelete="CASCADE"), nullable=True
    )
    dst_chapter_id: Mapped[int] = mapped_column(
        ForeignKey("chapters.id", ondelete="CASCADE"), index=True
    )
    src_file_id: Mapped[int] = mapped_column(ForeignKey("files.id", ondelete="CASCADE"), index=True)
    dst_file_id: Mapped[int] = mapped_column(ForeignKey("files.id", ondelete="CASCADE"))
    score: Mapped[float] = mapped_column(Float)

    __table_args__ = (Index("ix_relations_src_file", "src_file_id"),)


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


# --- Phase 4: synthesis layer (the compounding wiki) -----------------------
# These break the "everything but MySQL is derived" rule on the disk side: the wiki *prose* lives
# in the ``wiki/`` git repo, not here. These rows are the index/catalog + the provenance graph that
# keeps that prose auditable against the truth store (every claim -> chunk via claim_sources).


class Entity(Base):
    """A canonical thing the wiki has a page (or will have a page) about — a concept, component,
    protocol, person, org… ``norm_key`` is the normalized blocking key the resolver matches on;
    ``aliases`` keeps the surface variants. ``wiki_path``/``embedding_id`` are filled by later
    synthesis sub-stages (integrate / resolve). ``source_count`` = distinct files that mention it."""

    __tablename__ = "entities"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(512))           # canonical surface form
    type: Mapped[str] = mapped_column(String(32))            # concept|component|protocol|person|org|…
    norm_key: Mapped[str] = mapped_column(String(512))       # normalized blocking key (resolver)
    aliases: Mapped[list] = mapped_column(JSON, default=list)
    acronyms: Mapped[list] = mapped_column(JSON, default=list)   # short<->long forms for blocking
    glossary_defined: Mapped[bool] = mapped_column(default=False)  # seeded from a definitions section
    wiki_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    embedding_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # legacy/reserved
    name_embedding_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # entity_names row
    # active = a real page; unresolved = provisionally-new from a DEFER (wiki-lint surfaces it);
    # merged_into = folded into another entity by a merge (kept as a redirect, never hard-deleted);
    # pruned = failed the pageworthiness gate retroactively (wiki-prune; reversible, claims kept).
    status: Mapped[str] = mapped_column(String(16), default="active")
    source_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_entities_name_type", "name", "type", unique=True),
        Index("ix_entities_normkey_type", "norm_key", "type"),
    )


class WikiPage(Base):
    """Catalog row for one authored markdown page. The body is on disk in the git repo (``path``);
    this is the index synthesis + lint query. ``kind`` is entity|concept|topic|overview|query;
    ``md_hash`` lets a re-synth detect an unchanged page."""

    __tablename__ = "wiki_pages"

    id: Mapped[int] = mapped_column(primary_key=True)
    path: Mapped[str] = mapped_column(String(1024), unique=True)   # relative to wiki_dir
    title: Mapped[str] = mapped_column(String(512))
    kind: Mapped[str] = mapped_column(String(16))
    entity_id: Mapped[int | None] = mapped_column(
        ForeignKey("entities.id", ondelete="SET NULL"), nullable=True
    )
    source_count: Mapped[int] = mapped_column(Integer, default=0)
    last_synth_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    md_hash: Mapped[str | None] = mapped_column(String(HASH_LEN), nullable=True)


class Claim(Base):
    """A single asserted fact about an entity, extracted from one source. ``file_id`` is the
    asserting source (makes "clear this file's claims" a cheap delete + idempotent re-synth);
    ``wiki_page_id`` is filled when ``synth_integrate`` places the claim on a page. ``status``
    tracks contradiction/supersession so the wiki flags conflicts instead of overwriting them."""

    __tablename__ = "claims"

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_id: Mapped[int] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), index=True
    )
    wiki_page_id: Mapped[int | None] = mapped_column(
        ForeignKey("wiki_pages.id", ondelete="SET NULL"), nullable=True
    )
    file_id: Mapped[int] = mapped_column(ForeignKey("files.id", ondelete="CASCADE"), index=True)
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active|contradicted|superseded
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ClaimSource(Base):
    """Provenance: a claim down to the chunk(s) (and file) it came from. This is what makes the
    wiki auditable against the truth store — ``wiki-audit`` checks every cited chunk still exists."""

    __tablename__ = "claim_sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    claim_id: Mapped[int] = mapped_column(ForeignKey("claims.id", ondelete="CASCADE"), index=True)
    chunk_id: Mapped[int | None] = mapped_column(
        ForeignKey("chunks.id", ondelete="SET NULL"), nullable=True
    )
    file_id: Mapped[int] = mapped_column(ForeignKey("files.id", ondelete="CASCADE"), index=True)


class Mention(Base):
    """A source touched an entity here. Lets a re-synth (or contradiction check) know exactly which
    entity pages a given source affects, without re-running extraction."""

    __tablename__ = "mentions"

    id: Mapped[int] = mapped_column(primary_key=True)
    file_id: Mapped[int] = mapped_column(ForeignKey("files.id", ondelete="CASCADE"), index=True)
    chunk_id: Mapped[int | None] = mapped_column(
        ForeignKey("chunks.id", ondelete="SET NULL"), nullable=True
    )
    entity_id: Mapped[int] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), index=True
    )
    # The resolver's decision for this mention — auditable, and training data for a learned scorer.
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    decision: Mapped[str | None] = mapped_column(String(16), nullable=True)  # MATCH|NEW|DEFER
    signals: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class EntityReview(Base):
    """The human review queue for ambiguous resolutions (DEFER that the LLM couldn't settle). Holds
    the mention payload + the candidate ids/scores so a reviewer (or a later merge) has the full
    picture. The provisional ``entity_id`` is the #unresolved page created in the meantime."""

    __tablename__ = "entity_review"

    id: Mapped[int] = mapped_column(primary_key=True)
    mention_surface: Mapped[str] = mapped_column(String(512))
    mention_type: Mapped[str] = mapped_column(String(32))
    file_id: Mapped[int] = mapped_column(ForeignKey("files.id", ondelete="CASCADE"), index=True)
    entity_id: Mapped[int | None] = mapped_column(
        ForeignKey("entities.id", ondelete="SET NULL"), nullable=True
    )
    payload: Mapped[dict] = mapped_column(JSON, default=dict)  # candidates, scores, context
    llm_verdict: Mapped[str | None] = mapped_column(String(16), nullable=True)  # same|different|can't-tell
    human_verdict: Mapped[str | None] = mapped_column(String(16), nullable=True)
    state: Mapped[str] = mapped_column(String(16), default="open")  # open|resolved
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class EntityMerge(Base):
    """Audit + reversibility record for ``Merge(from→into)``. Keeps merges undoable and ties each to
    the wiki git commit that enacted it. ``moved`` is the undo manifest: once src's claims/mentions
    are repointed into dst they're indistinguishable from dst's own, so unmerge can only repoint the
    *right* rows back if the merge recorded exactly which ones it moved (and which aliases/acronyms it
    contributed to the survivor). Null on pre-manifest rows — those merges can't be auto-reversed."""

    __tablename__ = "entity_merges"

    id: Mapped[int] = mapped_column(primary_key=True)
    from_id: Mapped[int] = mapped_column(Integer, index=True)
    into_id: Mapped[int] = mapped_column(Integer, index=True)
    reason: Mapped[str] = mapped_column(String(256), default="")
    committed_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    moved: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


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

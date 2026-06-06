"""The ONLY metadata writer (per ``CLAUDE.md``).

Every mutation of the truth store funnels through here; everything else reads. Callers own the
transaction (they pass in a ``Session``); these functions never commit, so they compose inside
the DAG's per-stage ``session_scope``.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, insert, select
from sqlalchemy.orm import Session

from doctalk.db.models import Chapter, Chunk, Figure, File, Image, Job, JobStatus, Link, utcnow


# --- files -----------------------------------------------------------------


def get_file(session: Session, content_hash: str) -> File | None:
    return session.scalar(select(File).where(File.content_hash == content_hash))


def get_file_id(session: Session, content_hash: str) -> int | None:
    return session.scalar(select(File.id).where(File.content_hash == content_hash))


def upsert_file(
    session: Session,
    *,
    content_hash: str,
    path: str,
    filename: str,
    format: str,
    mime: str,
    byte_size: int,
) -> File:
    """Insert the source row, or refresh the mutable last-seen fields if the bytes already exist.

    Identity is the content hash, so a re-drop from a new path updates ``path`` but creates no
    duplicate row.
    """
    file = get_file(session, content_hash)
    if file is None:
        file = File(
            content_hash=content_hash,
            path=path,
            filename=filename,
            format=format,
            mime=mime,
            byte_size=byte_size,
        )
        session.add(file)
    else:
        file.path = path
        file.filename = filename
        file.format = format
        file.mime = mime
        file.byte_size = byte_size
    return file


# --- jobs ledger -----------------------------------------------------------


def get_job(session: Session, input_hash: str) -> Job | None:
    return session.scalar(select(Job).where(Job.input_hash == input_hash))


def is_stage_done(session: Session, input_hash: str) -> bool:
    """True only when a committed ``done`` row exists for this exact (source, stage, model, params)."""
    return (
        session.scalar(select(Job.status).where(Job.input_hash == input_hash))
        == JobStatus.done
    )


def begin_job(
    session: Session,
    *,
    content_hash: str,
    stage: str,
    input_hash: str,
    model_version: str = "",
    params: dict[str, Any] | None = None,
) -> Job:
    """Mark a stage ``running`` — upserting so a prior ``error``/``running`` row is reused (the
    ``input_hash`` is unique, so we never insert a duplicate)."""
    job = get_job(session, input_hash)
    if job is None:
        job = Job(
            content_hash=content_hash,
            stage=stage,
            input_hash=input_hash,
            model_version=model_version,
            params=params or {},
        )
        session.add(job)
    job.status = JobStatus.running
    job.error = None
    job.started_at = utcnow()
    job.finished_at = None
    return job


def complete_job(session: Session, input_hash: str) -> None:
    job = get_job(session, input_hash)
    if job is None:  # pragma: no cover - defensive
        raise ValueError(f"complete_job: no job row for input_hash={input_hash}")
    job.status = JobStatus.done
    job.error = None
    job.finished_at = utcnow()


def fail_job(session: Session, input_hash: str, error: str) -> None:
    job = get_job(session, input_hash)
    if job is None:  # pragma: no cover - defensive
        raise ValueError(f"fail_job: no job row for input_hash={input_hash}")
    job.status = JobStatus.error
    job.error = error[:4000]
    job.finished_at = utcnow()


# --- document structure (chapters / chunks / links) ------------------------
# Each stage clears its own prior output for a file before writing, so a re-run (e.g. after a
# model/param upgrade) never duplicates rows — "never process processed data" stays honest.


def clear_chapters_for_file(session: Session, file_id: int) -> None:
    # Chunks reference chapters; delete them first to avoid dangling rows.
    session.execute(delete(Chunk).where(Chunk.file_id == file_id))
    session.execute(delete(Chapter).where(Chapter.file_id == file_id))


def clear_links_for_file(session: Session, file_id: int) -> None:
    session.execute(delete(Link).where(Link.file_id == file_id))


def insert_chapters(
    session: Session, file_id: int, rows: list[dict[str, Any]]
) -> list[Chapter]:
    """Insert outline rows and resolve the tree. Each row carries ``parent_ord`` (the ``ord`` of
    its parent, or None); parents are linked after the flush assigns ids. Returns the persisted
    Chapter objects (with ids), in input order."""
    chapters = [
        Chapter(
            file_id=file_id,
            level=r["level"],
            ord=r["ord"],
            title=r["title"],
            page_start=r["page_start"],
            page_end=r["page_end"],
            source=r.get("source", "outline"),
        )
        for r in rows
    ]
    session.add_all(chapters)
    session.flush()  # assign ids
    ord_to_id = {c.ord: c.id for c in chapters}
    for row, chapter in zip(rows, chapters):
        parent_ord = row.get("parent_ord")
        if parent_ord is not None:
            chapter.parent_id = ord_to_id.get(parent_ord)
    session.flush()
    return chapters


def insert_chunks(session: Session, file_id: int, rows: list[dict[str, Any]]) -> None:
    if rows:
        session.execute(insert(Chunk), [{"file_id": file_id, **r} for r in rows])


def insert_links(session: Session, file_id: int, rows: list[dict[str, Any]]) -> None:
    if rows:
        session.execute(insert(Link), [{"file_id": file_id, **r} for r in rows])


def get_chapters(session: Session, file_id: int) -> list[Chapter]:
    return list(
        session.scalars(
            select(Chapter).where(Chapter.file_id == file_id).order_by(Chapter.ord)
        )
    )


def get_chunks(session: Session, file_id: int) -> list[Chunk]:
    return list(
        session.scalars(select(Chunk).where(Chunk.file_id == file_id).order_by(Chunk.ord))
    )


def get_all_file_ids(session: Session) -> list[int]:
    return list(session.scalars(select(File.id).order_by(File.id)))


# --- figures / tables ------------------------------------------------------


def clear_figures_for_file(session: Session, file_id: int) -> None:
    session.execute(delete(Figure).where(Figure.file_id == file_id))


def insert_figures(session: Session, file_id: int, rows: list[dict[str, Any]]) -> None:
    if rows:
        session.execute(insert(Figure), [{"file_id": file_id, **r} for r in rows])


def get_figures(session: Session, file_id: int) -> list[Figure]:
    return list(
        session.scalars(select(Figure).where(Figure.file_id == file_id).order_by(Figure.ord))
    )


def get_figures_for_page(session: Session, file_id: int, page: int) -> list[Figure]:
    return list(
        session.scalars(
            select(Figure)
            .where(Figure.file_id == file_id, Figure.page == page)
            .order_by(Figure.ord)
        )
    )


def get_figure(session: Session, figure_id: int) -> Figure | None:
    return session.get(Figure, figure_id)


def figures_needing_ocr(session: Session, file_id: int) -> list[Figure]:
    """Figure rasters (have an ``image_path``) whose ``ocr_text`` has not been set yet."""
    return list(
        session.scalars(
            select(Figure).where(
                Figure.file_id == file_id,
                Figure.image_path.is_not(None),
                Figure.ocr_text.is_(None),
            )
        )
    )


def set_figure_fields(session: Session, figure_id: int, **fields: Any) -> None:
    figure = session.get(Figure, figure_id)
    if figure is None:  # pragma: no cover - defensive
        raise ValueError(f"set_figure_fields: no figure row id={figure_id}")
    for key, value in fields.items():
        setattr(figure, key, value)


# --- images ----------------------------------------------------------------


def upsert_image(session: Session, file_id: int, **fields: Any) -> Image:
    """Create-or-update the images row for a file, setting only the provided fields. Lets the
    image stages (extract -> exif_geo -> vlm_describe) each contribute their slice idempotently."""
    image = session.scalar(select(Image).where(Image.file_id == file_id))
    if image is None:
        image = Image(file_id=file_id)
        session.add(image)
    for key, value in fields.items():
        setattr(image, key, value)
    return image


def get_image(session: Session, file_id: int) -> Image | None:
    return session.scalar(select(Image).where(Image.file_id == file_id))


def get_all_image_file_ids(session: Session) -> list[int]:
    return list(session.scalars(select(Image.file_id).order_by(Image.file_id)))

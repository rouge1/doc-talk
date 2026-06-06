"""Hybrid image search: structured metadata prefilter + CLIP semantic ranking.

A query has two halves — a typed ``ImageFilter`` (format / size / geo / time, pushed into LanceDB
as a prefilter) and an optional semantic string (CLIP text->image ANN within the filtered set).
With no semantic string it degrades to a pure metadata listing straight from MySQL. The filter is
a typed structure built from explicit CLI flags — Phase 2 adds the natural-language planner that
turns "dogs >100kb png" into one of these automatically.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select

from doctalk.db.models import File, Image
from doctalk.db.session import session_scope


@dataclass
class ImageFilter:
    format: str | None = None
    min_bytes: int | None = None
    max_bytes: int | None = None
    geo_country: str | None = None
    ts_from: int | None = None  # epoch seconds, inclusive
    ts_to: int | None = None


@dataclass
class ImageHit:
    file_id: int
    filename: str
    score: float | None  # CLIP similarity; None for pure-metadata listings
    format: str
    byte_size: int
    geo_country: str | None
    exif_datetime: datetime | None
    description: str | None


def month_range(year: int, month: int | None = None) -> tuple[int, int]:
    """Inclusive epoch-second bounds for a whole year or a single month."""
    start = datetime(year, month or 1, 1, tzinfo=timezone.utc)
    if month is None:
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    elif month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(year, month + 1, 1, tzinfo=timezone.utc)
    return int(start.timestamp()), int(end.timestamp()) - 1


def build_where(f: ImageFilter) -> str | None:
    """Render an ImageFilter into a LanceDB predicate. Values are app-controlled (CLI flags);
    the format string is sanitized defensively."""
    clauses: list[str] = []
    if f.format:
        fmt = re.sub(r"[^a-z0-9]", "", f.format.lower())
        clauses.append(f"format = '{fmt}'")
    if f.min_bytes is not None:
        clauses.append(f"byte_size > {int(f.min_bytes)}")
    if f.max_bytes is not None:
        clauses.append(f"byte_size < {int(f.max_bytes)}")
    if f.geo_country:
        cc = re.sub(r"[^A-Za-z]", "", f.geo_country)
        clauses.append(f"geo_country = '{cc}'")
    if f.ts_from is not None:
        clauses.append(f"exif_ts >= {int(f.ts_from)}")
    if f.ts_to is not None:
        clauses.append(f"exif_ts <= {int(f.ts_to)} AND exif_ts > 0")
    return " AND ".join(clauses) if clauses else None


def _to_hit(session, file_id: int, score: float | None) -> ImageHit | None:
    file = session.get(File, file_id)
    image = session.scalar(select(Image).where(Image.file_id == file_id))
    if file is None:
        return None
    return ImageHit(
        file_id=file_id,
        filename=file.filename,
        score=round(score, 4) if score is not None else None,
        format=file.format,
        byte_size=file.byte_size,
        geo_country=image.geo_country if image else None,
        exif_datetime=image.exif_datetime if image else None,
        description=image.vlm_description if image else None,
    )


def find_images(semantic: str, filt: ImageFilter, k: int = 10) -> list[ImageHit]:
    """CLIP text->image search within the metadata prefilter."""
    from doctalk.models.embed import embed_image_query
    from doctalk.vector import store

    query_vector = embed_image_query(semantic)
    raw = store.search_images(query_vector, k, where=build_where(filt))
    hits: list[ImageHit] = []
    with session_scope() as session:
        for row in raw:
            hit = _to_hit(session, row["file_id"], 1.0 - float(row.get("_distance", 0.0)))
            if hit:
                hits.append(hit)
    return hits


def list_images(filt: ImageFilter, limit: int = 50) -> list[ImageHit]:
    """Pure metadata listing (no semantic ranking) straight from MySQL."""
    with session_scope() as session:
        query = select(Image, File).join(File, Image.file_id == File.id)
        if filt.format:
            query = query.where(File.format == re.sub(r"[^a-z0-9]", "", filt.format.lower()))
        if filt.min_bytes is not None:
            query = query.where(File.byte_size > filt.min_bytes)
        if filt.max_bytes is not None:
            query = query.where(File.byte_size < filt.max_bytes)
        if filt.geo_country:
            query = query.where(Image.geo_country == filt.geo_country)
        if filt.ts_from is not None:
            query = query.where(Image.exif_datetime >= datetime.fromtimestamp(filt.ts_from, timezone.utc))
        if filt.ts_to is not None:
            query = query.where(Image.exif_datetime <= datetime.fromtimestamp(filt.ts_to, timezone.utc))
        hits = []
        for image, file in session.execute(query.limit(limit)).all():
            hits.append(
                ImageHit(
                    file_id=file.id,
                    filename=file.filename,
                    score=None,
                    format=file.format,
                    byte_size=file.byte_size,
                    geo_country=image.geo_country,
                    exif_datetime=image.exif_datetime,
                    description=image.vlm_description,
                )
            )
        return hits

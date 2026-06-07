"""LanceDB text-chunk index.

Mirrors only what's needed for ANN-with-prefilter and the join back to MySQL: ``chunk_id`` (the
join key), ``file_id``/``chapter_id``/``page`` (filter scalars), and the ``vector``. Chunk text is
NOT stored here — MySQL is the source of truth. ``chapter_id`` uses -1 as a null sentinel (Lance
filter scalars are non-null ints).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from doctalk.config import get_settings

TEXT_TABLE = "text_chunks"
IMAGE_TABLE = "images"
NO_CHAPTER = -1  # null sentinel for chapter_id


@lru_cache
def _db():
    settings = get_settings()
    settings.lance_dir.mkdir(parents=True, exist_ok=True)
    import lancedb  # lazy

    return lancedb.connect(str(settings.lance_dir))


def reset_db_cache() -> None:
    """Drop the cached connection (tests that repoint lance_dir)."""
    _db.cache_clear()


def _ensure_table(dim: int):
    import pyarrow as pa

    db = _db()
    if TEXT_TABLE not in db.table_names():
        schema = pa.schema(
            [
                ("chunk_id", pa.int64()),
                ("file_id", pa.int64()),
                ("chapter_id", pa.int64()),
                ("page", pa.int64()),
                ("vector", pa.list_(pa.float32(), dim)),
            ]
        )
        db.create_table(TEXT_TABLE, schema=schema)
    return db.open_table(TEXT_TABLE)


def add_text_chunks(rows: list[dict[str, Any]]) -> None:
    """Append rows; each row needs chunk_id/file_id/chapter_id/page/vector. Table is created on
    first insert using the vector dimension found in the data."""
    if not rows:
        return
    table = _ensure_table(len(rows[0]["vector"]))
    table.add(rows)


def delete_file_text(file_id: int) -> None:
    db = _db()
    if TEXT_TABLE in db.table_names():
        db.open_table(TEXT_TABLE).delete(f"file_id = {file_id}")


def drop_text_table() -> None:
    db = _db()
    if TEXT_TABLE in db.table_names():
        db.drop_table(TEXT_TABLE)


def search_text(query_vector: list[float], k: int, file_id: int | None = None) -> list[dict]:
    """ANN search (cosine), optionally prefiltered to one file. Returns raw Lance rows including
    ``_distance``."""
    db = _db()
    if TEXT_TABLE not in db.table_names():
        return []
    query = db.open_table(TEXT_TABLE).search(query_vector).metric("cosine").limit(k)
    if file_id is not None:
        query = query.where(f"file_id = {file_id}", prefilter=True)
    return query.to_list()


# --- image index (text->image hybrid search) -------------------------------
# Mirrors the scalars hybrid queries prefilter on: format, byte_size, geo_country, exif_ts.
# geo_country uses "" and exif_ts uses 0 as null sentinels (Lance filter scalars are non-null).
NO_GEO = ""
NO_TS = 0


def _ensure_image_table(dim: int):
    import pyarrow as pa

    db = _db()
    if IMAGE_TABLE not in db.table_names():
        schema = pa.schema(
            [
                ("file_id", pa.int64()),
                ("format", pa.string()),
                ("byte_size", pa.int64()),
                ("geo_country", pa.string()),
                ("exif_ts", pa.int64()),
                ("vector", pa.list_(pa.float32(), dim)),
            ]
        )
        db.create_table(IMAGE_TABLE, schema=schema)
    return db.open_table(IMAGE_TABLE)


def add_images(rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    table = _ensure_image_table(len(rows[0]["vector"]))
    table.add(rows)


def delete_file_images(file_id: int) -> None:
    db = _db()
    if IMAGE_TABLE in db.table_names():
        db.open_table(IMAGE_TABLE).delete(f"file_id = {file_id}")


def drop_image_table() -> None:
    db = _db()
    if IMAGE_TABLE in db.table_names():
        db.drop_table(IMAGE_TABLE)


def all_image_vectors() -> dict[int, list[float]]:
    """Every image's stored CLIP vector, keyed by file_id — the input to a global recluster."""
    db = _db()
    if IMAGE_TABLE not in db.table_names():
        return {}
    return {r["file_id"]: r["vector"] for r in db.open_table(IMAGE_TABLE).to_arrow().to_pylist()}


def get_image_vector(file_id: int) -> list[float] | None:
    """One image's stored CLIP vector (used by the per-file cluster stage when it isn't already
    in scratch). Reads the small image table directly rather than an ANN query with no target."""
    return all_image_vectors().get(file_id)


def search_images(query_vector: list[float], k: int, where: str | None = None) -> list[dict]:
    """CLIP text->image ANN search with an optional metadata prefilter (a Lance SQL predicate)."""
    db = _db()
    if IMAGE_TABLE not in db.table_names():
        return []
    query = db.open_table(IMAGE_TABLE).search(query_vector).metric("cosine").limit(k)
    if where:
        query = query.where(where, prefilter=True)
    return query.to_list()

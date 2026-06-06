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

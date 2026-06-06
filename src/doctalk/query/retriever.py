"""Retrieval: embed the query, ANN-search the LanceDB index, join hits back to MySQL for text +
provenance (chapter title, file name). The vector store holds only ids/scalars; the truth store
supplies the rest."""

from __future__ import annotations

from dataclasses import dataclass

from doctalk.db import repo
from doctalk.db.models import Chapter, Chunk, File
from doctalk.db.session import session_scope


@dataclass
class Hit:
    chunk_id: int
    file: str
    chapter: str | None
    page: int
    text: str
    score: float  # cosine similarity (1 - distance); higher is closer


def retrieve(question: str, k: int = 8, file_id: int | None = None) -> list[Hit]:
    from doctalk.models.embed import embed_query
    from doctalk.vector import store

    query_vector = embed_query(question)
    raw = store.search_text(query_vector, k, file_id=file_id)

    hits: list[Hit] = []
    with session_scope() as session:
        for row in raw:
            chunk = session.get(Chunk, row["chunk_id"])
            if chunk is None:  # index/truth drift — skip; rebuild-index fixes it
                continue
            chapter = session.get(Chapter, chunk.chapter_id) if chunk.chapter_id else None
            file = session.get(File, chunk.file_id)
            hits.append(
                Hit(
                    chunk_id=chunk.id,
                    file=file.filename if file else "?",
                    chapter=chapter.title if chapter else None,
                    page=chunk.page,
                    text=chunk.text,
                    score=round(1.0 - float(row.get("_distance", 0.0)), 4),
                )
            )
    return hits


def resolve_file_id(content_hash_or_prefix: str | None) -> int | None:
    """Map a content-hash (full or unique prefix) to a file id, or None to search all files."""
    if not content_hash_or_prefix:
        return None
    from sqlalchemy import select

    with session_scope() as session:
        return session.scalar(
            select(File.id).where(File.content_hash.like(f"{content_hash_or_prefix}%"))
        )

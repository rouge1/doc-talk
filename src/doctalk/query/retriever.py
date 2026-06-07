"""Retrieval: embed the query, ANN-search the LanceDB index, join hits back to MySQL for text +
provenance (chapter title, file name). The vector store holds only ids/scalars; the truth store
supplies the rest."""

from __future__ import annotations

import math
from dataclasses import dataclass

from doctalk.config import get_settings
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
    content_hash: str | None = None  # for building a citation link to the source doc/chapter
    chapter_id: int | None = None
    rerank_score: float | None = None  # cross-encoder relevance (0-1), set when reranking ran


def _sigmoid(x: float) -> float:
    """Map a raw cross-encoder logit to 0-1 for display, overflow-safe for large |x|."""
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


def _order_by_rerank(hits: list[Hit], scores: list[float], k: int) -> list[Hit]:
    """Attach normalized rerank scores and return the top-k by them. Pure (no model)."""
    for hit, raw in zip(hits, scores):
        hit.rerank_score = round(_sigmoid(raw), 4)
    return sorted(hits, key=lambda h: h.rerank_score or 0.0, reverse=True)[:k]


def _rerank_and_order(question: str, hits: list[Hit], k: int) -> list[Hit]:
    """Re-score candidates with the cross-encoder; on any failure keep the ANN order (skip)."""
    from doctalk.models import rerank as rr

    try:
        scores = rr.rerank(question, [h.text for h in hits])
    except Exception:  # noqa: BLE001 - missing model / load failure: degrade to ANN order
        return hits[:k]
    return _order_by_rerank(hits, scores, k)


def retrieve(
    question: str, k: int = 8, file_id: int | None = None, rerank: bool | None = None
) -> list[Hit]:
    from doctalk.models.embed import embed_query
    from doctalk.vector import store

    settings = get_settings()
    use_rerank = settings.rerank_enabled if rerank is None else rerank
    # Over-fetch a candidate pool when reranking; otherwise fetch exactly k.
    fetch_k = max(k, settings.rerank_candidates) if use_rerank else k

    query_vector = embed_query(question)
    raw = store.search_text(query_vector, fetch_k, file_id=file_id)

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
                    content_hash=file.content_hash if file else None,
                    chapter_id=chunk.chapter_id,
                )
            )

    if use_rerank and hits:
        return _rerank_and_order(question, hits, k)
    return hits[:k]


def resolve_file_id(content_hash_or_prefix: str | None) -> int | None:
    """Map a content-hash (full or unique prefix) to a file id, or None to search all files."""
    if not content_hash_or_prefix:
        return None
    from sqlalchemy import select

    with session_scope() as session:
        return session.scalar(
            select(File.id).where(File.content_hash.like(f"{content_hash_or_prefix}%"))
        )

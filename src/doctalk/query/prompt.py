"""Pure prompt assembly for RAG chat (no heavy imports — unit-testable).

Hits are duck-typed: any object with ``file``, ``chapter``, ``page``, ``text`` works.
"""

from __future__ import annotations

from typing import Any

SYSTEM_PROMPT = (
    "You answer questions ONLY from the provided context excerpts taken from the user's own "
    "documents. Each excerpt is numbered [n] with its source (file, chapter, page). Base your "
    "answer strictly on these excerpts and cite the ones you use inline as [n]. If the answer is "
    "not contained in the context, say you don't find it in the corpus — do not use outside "
    "knowledge or invent details."
)


def build_messages(question: str, hits: list[Any]) -> list[dict[str, str]]:
    blocks = []
    for i, h in enumerate(hits, start=1):
        chapter = h.chapter or "n/a"
        blocks.append(f"[{i}] (file: {h.file} · chapter: {chapter} · p.{h.page})\n{h.text}")
    context = "\n\n".join(blocks)
    user = (
        f"Question: {question}\n\n"
        f"Context excerpts:\n{context}\n\n"
        "Answer using only the context above, citing excerpts as [n]. "
        "If the context does not contain the answer, say so."
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def format_citations(hits: list[Any]) -> list[dict[str, Any]]:
    # content_hash/chapter_id are optional (getattr) so this stays duck-typed: it works on any
    # object with file/chapter/page, and adds link targets when the hit carries them. An image hit
    # (kind == "image") carries a file_id + photo URL so the Sources rail can show it as a plate.
    out: list[dict[str, Any]] = []
    for i, h in enumerate(hits, start=1):
        kind = getattr(h, "kind", "passage")
        file_id = getattr(h, "file_id", None)
        out.append(
            {
                "n": i,
                "kind": kind,
                "file": h.file,
                "chapter": h.chapter,
                "page": h.page,
                "content_hash": getattr(h, "content_hash", None),
                "chapter_id": getattr(h, "chapter_id", None),
                "chunk_id": getattr(h, "chunk_id", None),
                "file_id": file_id,
                "image": f"/api/image/{file_id}" if kind == "image" and file_id else None,
            }
        )
    return out

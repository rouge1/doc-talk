"""pdf_structure — outline tree + per-page chunks via PyMuPDF.

Builds the navigable chapter hierarchy from the PDF's table of contents and emits retrieval
chunks tagged with (chapter, page) so chat answers can cite a real location. Streams page by
page so a 100 MB / multi-thousand-page PDF never loads whole. Docling table/figure extraction
is a later stage; this is PyMuPDF-only (the giant-PDF-safe path from PLAN.md).
"""

from __future__ import annotations

import fitz  # PyMuPDF

from doctalk.db import repo
from doctalk.ingest.dag import StageContext
from doctalk.ingest.stages.util import page_chapter_map, split_text

CHUNK_CHARS = 1500
CHUNK_OVERLAP = 200


def _chapter_rows(toc: list[list], total_pages: int) -> list[dict]:
    """Turn ``get_toc`` output (``[level, title, page]``, 1-based page) into insertable rows
    with parent links and section page ranges. A section spans until the next entry of the same
    or higher level (smaller/equal level number)."""
    entries = [(lvl, title, pg) for (lvl, title, pg) in toc if pg and pg > 0]
    rows: list[dict] = []
    stack: list[tuple[int, int]] = []  # (level, ord) of open ancestors
    for i, (level, title, page) in enumerate(entries):
        page_end = total_pages
        for j in range(i + 1, len(entries)):
            if entries[j][0] <= level:
                page_end = max(page, entries[j][2] - 1)
                break
        while stack and stack[-1][0] >= level:
            stack.pop()
        parent_ord = stack[-1][1] if stack else None
        rows.append(
            {
                "level": level,
                "ord": i,
                "title": (title or "").strip()[:1024] or f"(untitled {i})",
                "page_start": page,
                "page_end": page_end,
                "source": "outline",
                "parent_ord": parent_ord,
            }
        )
        stack.append((level, i))
    return rows


def run(ctx: StageContext) -> None:
    file_id = repo.get_file_id(ctx.session, ctx.content_hash)
    if file_id is None:  # pragma: no cover - defensive
        raise ValueError(f"pdf_structure: no file row for {ctx.content_hash}")

    repo.clear_chapters_for_file(ctx.session, file_id)  # idempotent re-run

    doc = fitz.open(ctx.file_path)
    try:
        total_pages = doc.page_count
        chapter_rows = _chapter_rows(doc.get_toc(simple=True), total_pages)
        chapters = repo.insert_chapters(ctx.session, file_id, chapter_rows)
        page_to_chapter = page_chapter_map(chapters, total_pages)

        chunk_rows: list[dict] = []
        ordinal = 0
        for page_index in range(total_pages):
            text = doc[page_index].get_text("text")
            page = page_index + 1  # 1-based
            for piece in split_text(text, CHUNK_CHARS, CHUNK_OVERLAP):
                chunk_rows.append(
                    {
                        "chapter_id": page_to_chapter[page],
                        "page": page,
                        "ord": ordinal,
                        "text": piece,
                        "char_count": len(piece),
                    }
                )
                ordinal += 1
        repo.insert_chunks(ctx.session, file_id, chunk_rows)
    finally:
        doc.close()

    ctx.scratch["n_chapters"] = len(chapter_rows)
    ctx.scratch["n_chunks"] = ordinal

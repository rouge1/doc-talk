"""Pure helpers shared by document stages (no I/O — easy to unit test)."""

from __future__ import annotations

from typing import Protocol


class _HasPage(Protocol):
    id: int
    ord: int
    page_start: int


def page_chapter_map(chapters: list[_HasPage], total_pages: int) -> list[int | None]:
    """Map each 1-based page to its most specific chapter id.

    TOC entries are in reading order, so ``page_start`` is non-decreasing; for a given page the
    correct chapter is the last entry whose ``page_start`` is <= that page (deepest subsection
    when several share a start page). Returns a list indexed by page number (index 0 unused).
    Runs in O(pages + chapters) via a single forward walk.
    """
    result: list[int | None] = [None] * (total_pages + 2)
    ordered = sorted(chapters, key=lambda c: (c.page_start, c.ord))
    idx = 0
    current: int | None = None
    for page in range(1, total_pages + 1):
        while idx < len(ordered) and ordered[idx].page_start <= page:
            current = ordered[idx].id
            idx += 1
        result[page] = current
    return result


def split_text(text: str, size: int, overlap: int) -> list[str]:
    """Split text into ~``size``-char windows with ``overlap`` carry-over. Citation-friendly:
    chunks stay within a page so a retrieved chunk maps to one page number."""
    text = text.strip()
    if len(text) <= size:
        return [text] if text else []
    step = max(1, size - overlap)
    return [text[i : i + size] for i in range(0, len(text), step) if text[i : i + size].strip()]

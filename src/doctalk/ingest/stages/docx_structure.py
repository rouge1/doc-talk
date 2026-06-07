"""docx_structure — outline + chunks + tables from a Word document via python-docx.

Mirrors ``pdf_structure``'s contract for a flow (page-less) format: build a chapter tree, emit
retrieval chunks tagged with (chapter, position), and extract tables to the ``figures`` table.
A .docx has no fixed pages, so the citation "page" is a **block index** — the 1-based position of
a paragraph/table in document order — which keeps the outline navigable and chunks locatable.

Headings come from real ``Heading N`` / ``Title`` paragraph styles when present; otherwise a
conservative typographic fallback (short, non-list, punctuation-free lines) recovers the visual
headings of docs that were never tagged — the same outline-else-detect path PDFs use.
"""

from __future__ import annotations

import re

from doctalk.db import repo
from doctalk.ingest.dag import StageContext
from doctalk.ingest.stages.util import split_text

CHUNK_CHARS = 1500
CHUNK_OVERLAP = 200


def _iter_blocks(doc):
    """Yield ``("para", Paragraph)`` / ``("table", Table)`` in true document order (python-docx
    exposes paragraphs and tables separately; we walk the body XML to interleave them)."""
    from docx.oxml.table import CT_Tbl
    from docx.oxml.text.paragraph import CT_P
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    for child in doc.element.body.iterchildren():
        if isinstance(child, CT_P):
            yield "para", Paragraph(child, doc)
        elif isinstance(child, CT_Tbl):
            yield "table", Table(child, doc)


def _heading_level(style_name: str | None) -> int | None:
    """A real heading style -> its level (Title := 1); otherwise None."""
    if not style_name:
        return None
    if style_name == "Title":
        return 1
    if style_name.startswith("Heading"):
        m = re.search(r"(\d+)", style_name)
        return int(m.group(1)) if m else 1
    return None


def _looks_like_heading(text: str, style_name: str | None) -> bool:
    """Conservative fallback for untagged docs: a short, list-free, punctuation-terminated-free
    line reads as a heading (e.g. "Ingredients"), not body text."""
    if not text or (style_name and "List" in style_name):
        return False
    if len(text.split()) > 7 or text[-1] in ".,:;":
        return False
    return True


def _table_md(table) -> str:
    rows = [[(c.text or "").strip().replace("\n", " ") for c in r.cells] for r in table.rows]
    rows = [r for r in rows if any(cell for cell in r)]
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |", "|" + "|".join(["---"] * width) + "|"]
    out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(out)


def run(ctx: StageContext) -> None:
    import docx

    file_id = repo.get_file_id(ctx.session, ctx.content_hash)
    if file_id is None:  # pragma: no cover - defensive
        raise ValueError(f"docx_structure: no file row for {ctx.content_hash}")

    repo.clear_chapters_for_file(ctx.session, file_id)  # clears chunks too (idempotent)
    repo.clear_figures_for_file(ctx.session, file_id)

    doc = docx.Document(ctx.file_path)

    # First pass: are there real heading styles? Decide outline-vs-detect once, like pdf_structure.
    blocks = list(_iter_blocks(doc))
    has_real_headings = any(
        kind == "para" and _heading_level(getattr(obj.style, "name", None)) is not None
        for kind, obj in blocks
    )
    source = "docx" if has_real_headings else "heading_detect"

    toc: list[tuple[int, str, int]] = []
    sections: dict[int | None, dict] = {}  # heading-ord (or None preamble) -> {start, parts}
    figure_rows: list[dict] = []
    current_ord: int | None = None

    for block_index, (kind, obj) in enumerate(blocks, start=1):
        if kind == "table":
            md = _table_md(obj)
            if md:
                figure_rows.append(
                    {"page": block_index, "kind": "table", "ord": len(figure_rows), "table_md": md}
                )
            continue

        text = (obj.text or "").strip()
        if not text:
            continue
        style_name = getattr(obj.style, "name", None)
        level = _heading_level(style_name)
        is_heading = level is not None if has_real_headings else _looks_like_heading(text, style_name)

        if is_heading:
            toc.append((level or 1, text[:1024], block_index))
            current_ord = len(toc) - 1
        else:
            sec = sections.setdefault(current_ord, {"start": block_index, "parts": []})
            sec["parts"].append(text)

    # Build the chapter tree from the recovered headings (reuses the PDF row-builder).
    from doctalk.ingest.stages.pdf_structure import _chapter_rows

    ord_to_id: dict[int, int] = {}
    if toc:
        rows = _chapter_rows([list(t) for t in toc], total_pages=len(blocks), source=source)
        chapters = repo.insert_chapters(ctx.session, file_id, rows)
        ord_to_id = {c.ord: c.id for c in chapters}

    # Chunks: one text blob per section (preamble + each heading), split and position-tagged.
    chunk_rows: list[dict] = []
    ordinal = 0
    for ord_key, sec in sections.items():
        chapter_id = ord_to_id.get(ord_key) if ord_key is not None else None
        for piece in split_text("\n".join(sec["parts"]), CHUNK_CHARS, CHUNK_OVERLAP):
            chunk_rows.append(
                {
                    "chapter_id": chapter_id,
                    "page": sec["start"],
                    "ord": ordinal,
                    "text": piece,
                    "char_count": len(piece),
                }
            )
            ordinal += 1

    repo.insert_chunks(ctx.session, file_id, chunk_rows)
    repo.insert_figures(ctx.session, file_id, figure_rows)

    ctx.scratch["chapters_source"] = source if toc else "none"
    ctx.scratch["n_chapters"] = len(toc)
    ctx.scratch["n_chunks"] = ordinal
    ctx.scratch["n_tables"] = len(figure_rows)

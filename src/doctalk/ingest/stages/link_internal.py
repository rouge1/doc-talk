"""link_internal — resolve a PDF's internal GOTO hyperlinks into the cross-reference graph.

Reads the chapters written by ``pdf_structure`` and maps each link's source and destination page
to its chapter, so a cross-reference like "see Section 4.2" resolves to a navigable target. Runs
after ``pdf_structure`` and rebuilds from the current chapters each time (a full re-ingest keeps
links and chapters consistent).
"""

from __future__ import annotations

import fitz  # PyMuPDF

from doctalk.db import repo
from doctalk.ingest.dag import StageContext
from doctalk.ingest.stages.util import page_chapter_map


def run(ctx: StageContext) -> None:
    file_id = repo.get_file_id(ctx.session, ctx.content_hash)
    if file_id is None:  # pragma: no cover - defensive
        raise ValueError(f"link_internal: no file row for {ctx.content_hash}")

    repo.clear_links_for_file(ctx.session, file_id)  # idempotent re-run

    chapters = repo.get_chapters(ctx.session, file_id)
    title_by_id = {c.id: c.title for c in chapters}

    doc = fitz.open(ctx.file_path)
    try:
        total_pages = doc.page_count
        page_to_chapter = page_chapter_map(chapters, total_pages)

        link_rows: list[dict] = []
        for page_index in range(total_pages):
            for link in doc[page_index].get_links():
                # Internal targets: GOTO carries a page directly; NAMED is a named destination
                # that PyMuPDF resolves to a `page`. URI/LAUNCH have no resolved page (-1) and
                # are skipped. (The BT spec's cross-refs are almost all NAMED.)
                if link.get("kind") not in (fitz.LINK_GOTO, fitz.LINK_NAMED):
                    continue
                dst_index = link.get("page", -1)
                if dst_index is None or dst_index < 0:
                    continue
                src_page, dst_page = page_index + 1, dst_index + 1
                dst_chapter_id = page_to_chapter[dst_page]
                link_rows.append(
                    {
                        "kind": "internal_pdf",
                        "src_page": src_page,
                        "dst_page": dst_page,
                        "src_chapter_id": page_to_chapter[src_page],
                        "dst_chapter_id": dst_chapter_id,
                        "target_label": title_by_id.get(dst_chapter_id),
                        "score": 1.0,
                    }
                )
        repo.insert_links(ctx.session, file_id, link_rows)
    finally:
        doc.close()

    ctx.scratch["n_links"] = len(link_rows)

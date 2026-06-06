"""Phase 1: heading-detection fallback for PDFs with no embedded TOC.

A flat PDF (no set_toc) with numbered, larger-font headings and a repeated large running header
should yield a navigable chapter tree tagged source='heading_detect', with the running header
suppressed and chunks mapped to the detected chapters.
"""

from __future__ import annotations

import fitz
import pytest
from sqlalchemy import select

from doctalk.db import repo
from doctalk.db.models import Chapter, Chunk
from doctalk.db.session import session_scope
from doctalk.hashing import hash_file
from doctalk.ingest.dag import run_dag
from doctalk.ingest.pipeline import pipeline_for
from doctalk.ingest.stages.heading_detect import detect_headings


@pytest.fixture
def untoc_pdf(tmp_path):
    """5 pages, NO embedded TOC. Body text at 11pt; a 16pt bold running header on every page
    (must be suppressed); numbered bold headings at 18pt / 14pt establishing a 2-level tree."""
    doc = fitz.open()
    for pno in range(5):
        page = doc.new_page()
        page.insert_text((72, 40), "DRAFT SPEC — CONFIDENTIAL", fontsize=16, fontname="hebo")
        if pno == 1:
            page.insert_text((72, 110), "1 Introduction", fontsize=18, fontname="hebo")
            page.insert_text((72, 150), "Body prose about widgets and gizmos. " * 4, fontsize=11)
        elif pno == 2:
            page.insert_text((72, 110), "1.1 Scope", fontsize=14, fontname="hebo")
            page.insert_text((72, 150), "More body prose on the key schedule here. " * 4, fontsize=11)
        else:
            page.insert_text((72, 150), "Plain running body content for this page. " * 4, fontsize=11)
    path = tmp_path / "untoc.pdf"
    doc.save(str(path))
    doc.close()
    return path


def test_detect_headings_unit(untoc_pdf):
    doc = fitz.open(str(untoc_pdf))
    entries = detect_headings(doc)
    doc.close()
    titles = [t for _, t, _ in entries]
    assert "1 Introduction" in titles and "1.1 Scope" in titles
    assert "DRAFT SPEC — CONFIDENTIAL" not in titles  # running header suppressed
    levels = {t: lvl for lvl, t, _ in entries}
    assert levels["1 Introduction"] == 1 and levels["1.1 Scope"] == 2


def test_untoc_pdf_builds_tree_via_fallback(db, untoc_pdf):
    content_hash = hash_file(untoc_pdf)
    with session_scope() as s:
        repo.upsert_file(
            s,
            content_hash=content_hash,
            path=str(untoc_pdf),
            filename=untoc_pdf.name,
            format="pdf",
            mime="application/pdf",
            byte_size=untoc_pdf.stat().st_size,
        )
    results = run_dag(content_hash, pipeline_for("pdf"), file_path=str(untoc_pdf))
    assert [r.status for r in results] == ["done", "done", "done"]

    with session_scope() as s:
        file_id = repo.get_file_id(s, content_hash)
        chapters = repo.get_chapters(s, file_id)
        assert [c.title for c in chapters] == ["1 Introduction", "1.1 Scope"]
        intro, scope = chapters
        assert all(c.source == "heading_detect" for c in chapters)
        assert intro.level == 1 and scope.level == 2 and scope.parent_id == intro.id

        # chunks on the Scope page map to the Scope chapter (citable even when inferred)
        scope_chunk = s.scalar(select(Chunk).where(Chunk.text.like("%key schedule%")))
        assert scope_chunk is not None and scope_chunk.chapter_id == scope.id

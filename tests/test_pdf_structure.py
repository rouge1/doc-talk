"""Phase 1 verification (hermetic): a synthesized PDF with a TOC + an internal GOTO link runs
through the document backbone and produces a navigable chapter tree, page-tagged chunks, and a
resolved cross-reference. Idempotency: re-running does not duplicate rows.

Uses a tiny PDF built with PyMuPDF so the test needs no external fixture.
"""

from __future__ import annotations

import fitz
import pytest
from sqlalchemy import func, select

from doctalk.db import repo
from doctalk.db.models import Chapter, Chunk, Link
from doctalk.db.session import session_scope
from doctalk.hashing import hash_file
from doctalk.ingest.dag import run_dag
from doctalk.ingest.pipeline import pipeline_for


@pytest.fixture
def sample_pdf(tmp_path):
    """4 pages: cover, Chapter One (p2), Section 1.1 (p3), tail; with a GOTO link cover->p3."""
    doc = fitz.open()
    doc.new_page()  # cover
    doc.new_page().insert_text((72, 72), "Chapter One body discussing E0 encryption in detail.")
    doc.new_page().insert_text((72, 72), "Section 1.1 elaborates on the key schedule.")
    doc.new_page().insert_text((72, 72), "Tail matter.")
    # Insert the GOTO link (cover -> page index 2 == 1-based page 3) BEFORE set_toc, which
    # rebuilds the document and would invalidate an earlier page handle.
    doc[0].insert_link({"kind": fitz.LINK_GOTO, "from": fitz.Rect(72, 100, 200, 120), "page": 2})
    doc.set_toc([[1, "Chapter One", 2], [2, "Section 1.1", 3]])
    path = tmp_path / "sample.pdf"
    doc.save(str(path))
    doc.close()
    return path


def _ingest(path):
    content_hash = hash_file(path)
    with session_scope() as s:
        repo.upsert_file(
            s,
            content_hash=content_hash,
            path=str(path),
            filename=path.name,
            format="pdf",
            mime="application/pdf",
            byte_size=path.stat().st_size,
        )
    return content_hash, run_dag(content_hash, pipeline_for("pdf"), file_path=str(path))


def test_outline_chunks_and_xref(db, sample_pdf):
    content_hash, results = _ingest(sample_pdf)
    assert [r.status for r in results] == ["done", "done", "done"]

    with session_scope() as s:
        file_id = repo.get_file_id(s, content_hash)
        chapters = repo.get_chapters(s, file_id)

        # Navigable tree: two headings, Section 1.1 nested under Chapter One.
        assert [c.title for c in chapters] == ["Chapter One", "Section 1.1"]
        ch1, sec = chapters
        assert ch1.level == 1 and sec.level == 2
        assert sec.parent_id == ch1.id
        assert ch1.page_start == 2 and sec.page_start == 3

        # Chunks are page-tagged and mapped to the right chapter for citation.
        e0 = s.scalar(select(Chunk).where(Chunk.text.like("%E0 encryption%")))
        assert e0 is not None and e0.page == 2 and e0.chapter_id == ch1.id

        # The cross-reference resolves: cover (p1) -> Section 1.1 (p3).
        link = s.scalar(select(Link).where(Link.kind == "internal_pdf"))
        assert link is not None
        assert link.src_page == 1 and link.dst_page == 3
        assert link.dst_chapter_id == sec.id and link.target_label == "Section 1.1"


def test_reingest_is_idempotent_and_no_duplicate_rows(db, sample_pdf):
    content_hash, _ = _ingest(sample_pdf)
    with session_scope() as s:
        before = {
            "chapters": s.scalar(select(func.count()).select_from(Chapter)),
            "chunks": s.scalar(select(func.count()).select_from(Chunk)),
            "links": s.scalar(select(func.count()).select_from(Link)),
        }

    # Re-drop: every stage should be skipped (ledger) and counts unchanged.
    _, results = _ingest(sample_pdf)
    assert [r.status for r in results] == ["skipped", "skipped", "skipped"]

    with session_scope() as s:
        after = {
            "chapters": s.scalar(select(func.count()).select_from(Chapter)),
            "chunks": s.scalar(select(func.count()).select_from(Chunk)),
            "links": s.scalar(select(func.count()).select_from(Link)),
        }
    assert before == after and before["chapters"] == 2 and before["links"] == 1

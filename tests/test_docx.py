"""docx_structure: headings (real styles or detected), chunks, and tables -> figures.

Builds tiny .docx files in-memory so the test is self-contained. Skipped if python-docx isn't
installed (it's a runtime dep, but the suite must collect without it).
"""

from __future__ import annotations

import pytest

from doctalk.db import repo
from doctalk.db.session import session_scope
from doctalk.hashing import hash_file
from doctalk.ingest.dag import StageContext
from doctalk.ingest.stages import docx_structure

docx = pytest.importorskip("docx")  # runtime dep; skip the module if it isn't installed


def _ingest(path) -> str:
    content_hash = hash_file(path)
    with session_scope() as s:
        repo.upsert_file(
            s, content_hash=content_hash, path=str(path), filename=path.name,
            format="docx", mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            byte_size=path.stat().st_size,
        )
    with session_scope() as s:
        ctx = StageContext(content_hash, str(path), s)
        docx_structure.run(ctx)
    return content_hash


def test_styled_headings_become_chapters_with_tables(db, tmp_path):
    d = docx.Document()
    d.add_heading("Chocolate Cake", level=0)  # Title -> level 1
    d.add_heading("Ingredients", level=1)
    d.add_paragraph("200g flour")
    d.add_paragraph("100g sugar")
    d.add_heading("Steps", level=1)
    d.add_paragraph("Mix the dry and wet ingredients, then bake until golden and fluffy.")
    t = d.add_table(rows=2, cols=2)
    t.rows[0].cells[0].text, t.rows[0].cells[1].text = "Pan", "Size"
    t.rows[1].cells[0].text, t.rows[1].cells[1].text = "Round", "9 inch"
    p = tmp_path / "cake.docx"
    d.save(str(p))

    content_hash = _ingest(p)
    with session_scope() as s:
        fid = repo.get_file_id(s, content_hash)
        chapters = repo.get_chapters(s, fid)
        chunks = repo.get_chunks(s, fid)
        figs = repo.get_figures(s, fid)

    titles = [c.title for c in chapters]
    assert "Ingredients" in titles and "Steps" in titles
    assert all(c.source == "docx" for c in chapters)
    assert chunks and any("flour" in c.text for c in chunks)
    # chunk under "Ingredients" is linked to that chapter
    ing_id = next(c.id for c in chapters if c.title == "Ingredients")
    assert any(c.chapter_id == ing_id for c in chunks)
    tables = [f for f in figs if f.kind == "table"]
    assert len(tables) == 1 and "Pan" in tables[0].table_md


def test_untagged_doc_falls_back_to_detected_headings(db, tmp_path):
    """A doc with only Normal-styled paragraphs still recovers its visual headings."""
    d = docx.Document()
    d.add_paragraph("Ingredients")  # short, no punctuation -> detected heading
    d.add_paragraph("Two cups of all-purpose flour, sifted and measured carefully for the batter.")
    d.add_paragraph("Instructions")
    d.add_paragraph("Combine everything in a bowl and bake the mixture until it is fully set.")
    p = tmp_path / "untagged.docx"
    d.save(str(p))

    content_hash = _ingest(p)
    with session_scope() as s:
        fid = repo.get_file_id(s, content_hash)
        chapters = repo.get_chapters(s, fid)

    titles = [c.title for c in chapters]
    assert "Ingredients" in titles and "Instructions" in titles
    assert all(c.source == "heading_detect" for c in chapters)


def test_reingest_is_idempotent(db, tmp_path):
    d = docx.Document()
    d.add_heading("Title", level=1)
    d.add_paragraph("Some body text that is long enough to make at least one retrieval chunk here.")
    p = tmp_path / "x.docx"
    d.save(str(p))

    content_hash = _ingest(p)
    with session_scope() as s:
        fid = repo.get_file_id(s, content_hash)
        n_first = len(repo.get_chunks(s, fid))
    _ingest(p)  # re-run clears + rewrites
    with session_scope() as s:
        fid = repo.get_file_id(s, content_hash)
        assert len(repo.get_chunks(s, fid)) == n_first

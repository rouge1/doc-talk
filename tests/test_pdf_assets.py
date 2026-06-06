"""pdf_assets: extract a ruled table (-> markdown) and an embedded raster (-> disk), idempotently.

Builds a tiny synthetic PDF (a 3x3 ruled grid + one embedded PNG) so the test is self-contained
and dependency-light — no large fixtures, no models loaded.
"""

from __future__ import annotations

import io
from pathlib import Path

import fitz
import pytest
from PIL import Image as PILImage

from doctalk.db import repo
from doctalk.db.session import session_scope
from doctalk.hashing import hash_file
from doctalk.ingest.dag import StageContext
from doctalk.ingest.stages import pdf_assets

CELLS = [["Name", "Qty", "Price"], ["Apple", "3", "1.0"], ["Pear", "2", "2.0"]]


def _make_pdf(path: Path) -> None:
    doc = fitz.open()
    page = doc.new_page(width=400, height=520)
    x0, y0, cw, ch = 50, 50, 100, 40
    for r in range(len(CELLS) + 1):  # horizontal rules
        y = y0 + r * ch
        page.draw_line((x0, y), (x0 + 3 * cw, y))
    for c in range(4):  # vertical rules
        x = x0 + c * cw
        page.draw_line((x, y0), (x, y0 + 3 * ch))
    for r, row in enumerate(CELLS):
        for c, val in enumerate(row):
            page.insert_text((x0 + c * cw + 6, y0 + r * ch + 26), val)
    buf = io.BytesIO()
    PILImage.new("RGB", (120, 120), (200, 60, 60)).save(buf, format="PNG")
    page.insert_image(fitz.Rect(50, 320, 220, 470), stream=buf.getvalue())
    doc.save(str(path))
    doc.close()


@pytest.fixture
def pdf(tmp_path):
    p = tmp_path / "table_and_figure.pdf"
    _make_pdf(p)
    return p


def _ingest_assets(pdf: Path) -> str:
    content_hash = hash_file(pdf)
    with session_scope() as s:
        repo.upsert_file(
            s,
            content_hash=content_hash,
            path=str(pdf),
            filename=pdf.name,
            format="pdf",
            mime="application/pdf",
            byte_size=pdf.stat().st_size,
        )
    with session_scope() as s:
        pdf_assets.run(StageContext(content_hash, str(pdf), s))
    return content_hash


def test_extracts_table_and_figure(db, pdf):
    content_hash = _ingest_assets(pdf)
    with session_scope() as s:
        figs = repo.get_figures(s, repo.get_file_id(s, content_hash))

    tables = [f for f in figs if f.kind == "table"]
    figures = [f for f in figs if f.kind == "figure"]

    assert tables, "expected at least one detected table"
    assert any("Apple" in (t.table_md or "") for t in tables)

    assert len(figures) == 1
    assert figures[0].image_path and Path(figures[0].image_path).is_file()
    assert figures[0].width == 120 and figures[0].height == 120


def test_reextraction_is_idempotent(db, pdf):
    content_hash = _ingest_assets(pdf)
    with session_scope() as s:
        first = len(repo.get_figures(s, repo.get_file_id(s, content_hash)))

    # Re-run the stage: rows are cleared+rewritten, the on-disk raster is not duplicated.
    with session_scope() as s:
        pdf_assets.run(StageContext(content_hash, str(pdf), s))
    with session_scope() as s:
        figs = repo.get_figures(s, repo.get_file_id(s, content_hash))

    assert len(figs) == first
    rasters = [f for f in figs if f.image_path]
    assert len(rasters) == 1 and Path(rasters[0].image_path).is_file()


def test_tiny_rasters_are_skipped(db, tmp_path):
    """An icon-sized embedded image (< figure_min_px) is not recorded as a figure."""
    p = tmp_path / "tiny.pdf"
    doc = fitz.open()
    page = doc.new_page(width=300, height=300)
    buf = io.BytesIO()
    PILImage.new("RGB", (20, 20), (0, 0, 0)).save(buf, format="PNG")
    page.insert_image(fitz.Rect(10, 10, 30, 30), stream=buf.getvalue())
    doc.save(str(p))
    doc.close()

    content_hash = _ingest_assets(p)
    with session_scope() as s:
        figs = repo.get_figures(s, repo.get_file_id(s, content_hash))
    assert not [f for f in figs if f.kind == "figure"]

"""pdf_assets — extract tables and figures from a PDF with PyMuPDF (no torch, giant-PDF-safe).

Tables are detected per page and stored as markdown (``table_md``); embedded figure rasters are
extracted to ``figures_dir/<content_hash>/`` and recorded with an ``image_path``. This is the
PyMuPDF-only path from PLAN.md ("Docling reserved for table-rich pages" is a Phase-2 upgrade for
hard tables). Streams page by page so a multi-thousand-page PDF never loads whole. Idempotent: the
file's prior figure rows and on-disk rasters are cleared before re-extraction.

A repeated raster (e.g. a header logo on every page) is extracted once — dedup is by PDF ``xref``,
and we record the first page it appears on. Rasters smaller than ``figure_min_px`` on a side are
icons/rules, not figures, and are skipped.
"""

from __future__ import annotations

import shutil

import fitz  # PyMuPDF

from doctalk.config import get_settings
from doctalk.db import repo
from doctalk.ingest.dag import StageContext


def _fmt_bbox(rect) -> str | None:
    try:
        return ",".join(str(round(v, 1)) for v in (rect.x0, rect.y0, rect.x1, rect.y1))
    except Exception:  # noqa: BLE001 - bbox is best-effort metadata
        return None


def run(ctx: StageContext) -> None:
    settings = get_settings()
    file_id = repo.get_file_id(ctx.session, ctx.content_hash)
    if file_id is None:  # pragma: no cover - defensive
        raise ValueError(f"pdf_assets: no file row for {ctx.content_hash}")

    repo.clear_figures_for_file(ctx.session, file_id)  # idempotent re-run (rows)
    out_dir = settings.figures_dir / ctx.content_hash
    shutil.rmtree(out_dir, ignore_errors=True)  # idempotent re-run (rasters)

    min_px = settings.figure_min_px
    rows: list[dict] = []
    ordinal = 0
    seen_xrefs: set[int] = set()

    doc = fitz.open(ctx.file_path)
    try:
        for page_index in range(doc.page_count):
            page = doc[page_index]
            page_no = page_index + 1  # 1-based, citation-facing

            # --- tables -> markdown ---------------------------------------
            try:
                tables = page.find_tables().tables
            except Exception:  # noqa: BLE001 - table finder is heuristic; never fail the page
                tables = []
            for table in tables:
                md = (table.to_markdown() or "").strip()
                if not md:
                    continue
                rows.append(
                    {
                        "page": page_no,
                        "kind": "table",
                        "ord": ordinal,
                        "bbox": _fmt_bbox(fitz.Rect(table.bbox)),
                        "table_md": md,
                    }
                )
                ordinal += 1

            # --- embedded figure rasters ----------------------------------
            for img in page.get_images(full=True):
                xref = img[0]
                if xref in seen_xrefs:
                    continue
                seen_xrefs.add(xref)
                try:
                    extracted = doc.extract_image(xref)
                except Exception:  # noqa: BLE001 - unsupported/broken image object
                    continue
                width, height = extracted.get("width", 0), extracted.get("height", 0)
                if width < min_px or height < min_px:
                    continue  # icon / rule, not a figure
                ext = extracted.get("ext", "png")
                out_dir.mkdir(parents=True, exist_ok=True)
                rel = f"p{page_no}_x{xref}.{ext}"
                (out_dir / rel).write_bytes(extracted["image"])
                bbox_rects = page.get_image_rects(xref)
                rows.append(
                    {
                        "page": page_no,
                        "kind": "figure",
                        "ord": ordinal,
                        "bbox": _fmt_bbox(bbox_rects[0]) if bbox_rects else None,
                        "width": width,
                        "height": height,
                        "image_path": str(out_dir / rel),
                    }
                )
                ordinal += 1
    finally:
        doc.close()

    repo.insert_figures(ctx.session, file_id, rows)
    ctx.scratch["n_tables"] = sum(1 for r in rows if r["kind"] == "table")
    ctx.scratch["n_figures"] = sum(1 for r in rows if r["kind"] == "figure")

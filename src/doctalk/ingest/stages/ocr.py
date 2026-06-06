"""ocr — read embedded text from standalone images and extracted PDF figure rasters (Tesseract).

Two entry points share one engine (``models.ocr``):
  * ``run_image``   — OCR a photo/screenshot file -> ``images.ocr_text``.
  * ``run_figures`` — OCR each figure raster a PDF produced -> ``figures.ocr_text`` (diagram labels,
                      callouts), so figure text is searchable even before the VLM describes it.

Both degrade gracefully: if the engine is unavailable the field is left as recorded (None) and the
stage still completes, so a missing ``tesseract`` binary never blocks ingest. ``ocr_text=None``
means "not yet read" (re-runnable once OCR is installed); ``""`` means "read, no text found".
"""

from __future__ import annotations

from pathlib import Path

from doctalk.db import repo
from doctalk.ingest.dag import StageContext
from doctalk.models.ocr import ocr_available, ocr_image


def run_image(ctx: StageContext) -> None:
    file_id = repo.get_file_id(ctx.session, ctx.content_hash)
    if file_id is None:  # pragma: no cover - defensive
        raise ValueError(f"ocr(image): no file row for {ctx.content_hash}")

    if not ocr_available():
        ctx.scratch["ocr"] = "unavailable"
        return
    text = ocr_image(ctx.file_path)
    repo.upsert_image(ctx.session, file_id, ocr_text=text or "")
    ctx.scratch["ocr_chars"] = len(text or "")


def run_figures(ctx: StageContext) -> None:
    file_id = repo.get_file_id(ctx.session, ctx.content_hash)
    if file_id is None:  # pragma: no cover - defensive
        raise ValueError(f"ocr(figures): no file row for {ctx.content_hash}")

    if not ocr_available():
        ctx.scratch["ocr"] = "unavailable"
        return
    total = 0
    for figure in repo.figures_needing_ocr(ctx.session, file_id):
        if not figure.image_path or not Path(figure.image_path).is_file():
            continue
        text = ocr_image(figure.image_path)
        repo.set_figure_fields(ctx.session, figure.id, ocr_text=text or "")
        total += len(text or "")
    ctx.scratch["ocr_chars"] = total

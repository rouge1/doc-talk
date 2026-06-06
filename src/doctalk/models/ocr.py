"""Optical character recognition via Tesseract (local, CPU, no GPU lease).

PLAN's stated OCR fallback (PaddleOCR/PP-Structure is the Phase-2 primary). Kept deliberately
optional: ``pytesseract`` lives in the ``ocr`` extra and needs the system ``tesseract`` binary, so
every entry point degrades gracefully — if the engine is unavailable we return ``None`` and the
caller records "no OCR" rather than failing the ingest. Availability is probed once and cached.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from doctalk.config import get_settings


@lru_cache
def ocr_available() -> bool:
    """True only if both the python binding and the tesseract binary are importable/runnable."""
    try:
        import pytesseract  # noqa: F401

        pytesseract.get_tesseract_version()
        return True
    except Exception:  # noqa: BLE001 - missing package OR missing binary OR PATH issue
        return False


def ocr_image(path: str | Path) -> str | None:
    """Return the recognized text (stripped), ``None`` if the engine is unavailable, or ``""`` if
    the engine ran but found no text. Never raises on a normal image — OCR is best-effort."""
    if not ocr_available():
        return None
    import pytesseract
    from PIL import Image

    lang = get_settings().ocr_lang
    try:
        with Image.open(path) as im:
            text = pytesseract.image_to_string(im, lang=lang)
    except Exception:  # noqa: BLE001 - corrupt raster / unsupported mode: treat as no text
        return ""
    return text.strip()

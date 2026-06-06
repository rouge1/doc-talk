"""OCR stages degrade gracefully when Tesseract is unavailable, and write text when it is.

The engine itself (the real ``tesseract`` binary) is not assumed present in CI, so availability is
monkeypatched rather than invoked.
"""

from __future__ import annotations

from doctalk.db import repo
from doctalk.db.session import session_scope
from doctalk.ingest.dag import StageContext
from doctalk.ingest.stages import ocr


def _file(content_hash: str = "h" * 64) -> str:
    with session_scope() as s:
        repo.upsert_file(
            s,
            content_hash=content_hash,
            path="/nonexistent/photo.png",
            filename="photo.png",
            format="png",
            mime="image/png",
            byte_size=10,
        )
    return content_hash


def test_image_ocr_unavailable_is_a_noop(db, monkeypatch):
    content_hash = _file()
    monkeypatch.setattr(ocr, "ocr_available", lambda: False)

    with session_scope() as s:
        ctx = StageContext(content_hash, "/nonexistent/photo.png", s)
        ocr.run_image(ctx)
    assert ctx.scratch["ocr"] == "unavailable"

    with session_scope() as s:
        img = repo.get_image(s, repo.get_file_id(s, content_hash))
    assert img is None or img.ocr_text is None  # never marked "read with no text"


def test_image_ocr_writes_text_when_available(db, monkeypatch):
    content_hash = _file()
    monkeypatch.setattr(ocr, "ocr_available", lambda: True)
    monkeypatch.setattr(ocr, "ocr_image", lambda path: "HELLO WORLD")

    with session_scope() as s:
        ocr.run_image(StageContext(content_hash, "/nonexistent/photo.png", s))
    with session_scope() as s:
        img = repo.get_image(s, repo.get_file_id(s, content_hash))
    assert img is not None and img.ocr_text == "HELLO WORLD"

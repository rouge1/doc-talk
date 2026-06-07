"""FastAPI app for the Phase 1 web UI.

Routes are thin: they read the truth store (MySQL/SQLite) and delegate to the same ``query``
functions the CLI uses (retriever, chat, hybrid). Models load lazily on first query, so the app
starts instantly; the chat route blocks for the local LLM (~a minute) and runs in FastAPI's
threadpool. Templates live alongside this module.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from doctalk.db import repo
from doctalk.db.models import Chapter, Chunk, Figure, File
from doctalk.db.session import session_scope
from doctalk.ingest.pipeline import IMAGE_FORMATS

app = FastAPI(title="doctalk")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    docs, images_count = [], 0
    with session_scope() as s:
        for f in s.scalars(select(File).order_by(File.id)):
            if f.format in IMAGE_FORMATS:
                images_count += 1
                continue
            n_ch = s.scalar(select(func.count()).select_from(Chapter).where(Chapter.file_id == f.id))
            n_ck = s.scalar(select(func.count()).select_from(Chunk).where(Chunk.file_id == f.id))
            docs.append(
                {"hash": f.content_hash, "name": f.filename, "chapters": n_ch, "chunks": n_ck}
            )
    return templates.TemplateResponse(
        request, "index.html", {"docs": docs, "images_count": images_count}
    )


@app.get("/doc/{content_hash}", response_class=HTMLResponse)
def doc(request: Request, content_hash: str):
    with session_scope() as s:
        f = repo.get_file(s, content_hash)
        if f is None:
            return HTMLResponse("document not found", status_code=404)
        name = f.filename
        chapters = [
            {"id": c.id, "title": c.title, "level": c.level, "page": c.page_start}
            for c in repo.get_chapters(s, f.id)
        ]
        # Flat docs (no outline) still surface their tables/figures here, since there are no
        # chapter pages to host them. Structured docs show assets per-chapter instead.
        assets = []
        if not chapters:
            assets = [
                {"id": fig.id, "kind": fig.kind, "page": fig.page,
                 "table_md": fig.table_md, "ocr": fig.ocr_text}
                for fig in repo.get_figures(s, f.id)
            ]
    return templates.TemplateResponse(
        request,
        "doc.html",
        {"name": name, "hash": content_hash, "chapters": chapters, "assets": assets},
    )


@app.get("/doc/{content_hash}/chapter/{chapter_id}", response_class=HTMLResponse)
def chapter(request: Request, content_hash: str, chapter_id: int):
    with session_scope() as s:
        c = s.get(Chapter, chapter_id)
        if c is None:
            return HTMLResponse("chapter not found", status_code=404)
        title, page, page_end = c.title, c.page_start, c.page_end
        chunks = [
            {"page": ck.page, "text": ck.text}
            for ck in s.scalars(
                select(Chunk).where(Chunk.chapter_id == chapter_id).order_by(Chunk.ord)
            )
        ]
        # Tables/figures whose page falls inside this section's range.
        assets = [
            {
                "id": fig.id,
                "kind": fig.kind,
                "page": fig.page,
                "table_md": fig.table_md,
                "ocr": fig.ocr_text,
                "caption": fig.caption,
            }
            for fig in s.scalars(
                select(Figure)
                .where(
                    Figure.file_id == c.file_id,
                    Figure.page >= page,
                    Figure.page <= page_end,
                )
                .order_by(Figure.ord)
            )
        ]
    return templates.TemplateResponse(
        request,
        "chapter.html",
        {"hash": content_hash, "title": title, "page": page, "chunks": chunks, "assets": assets},
    )


@app.get("/search", response_class=HTMLResponse)
def search(request: Request, q: str = ""):
    hits = []
    if q.strip():
        from doctalk.query.retriever import retrieve

        hits = [
            {"file": h.file, "chapter": h.chapter, "page": h.page, "text": h.text, "score": h.score}
            for h in retrieve(q, k=10)
        ]
    return templates.TemplateResponse(request, "search.html", {"q": q, "hits": hits})


@app.get("/chat", response_class=HTMLResponse)
def chat(request: Request, q: str = ""):
    result = None
    if q.strip():
        from doctalk.query.chat import answer

        result = answer(q, k=6)
    return templates.TemplateResponse(request, "chat.html", {"q": q, "result": result})


def _opt_float(raw: str) -> float | None:
    """Parse an optional numeric query param. Forms submit empty fields as ``""`` (not absent),
    which a ``float`` param would reject with a 422 — so we take ``str`` and coerce, treating
    blank or non-numeric input as "no filter" rather than an error."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


@app.get("/gallery", response_class=HTMLResponse)
def gallery(request: Request, q: str = "", fmt: str = "", min_kb: str = ""):
    from doctalk.query.hybrid import ImageFilter, find_images, list_images

    min_kb_val = _opt_float(min_kb)
    filt = ImageFilter(
        format=fmt or None, min_bytes=int(min_kb_val * 1024) if min_kb_val else None
    )
    hits = find_images(q, filt, k=24) if q.strip() else list_images(filt, limit=48)
    items = [
        {
            "file_id": h.file_id,
            "name": h.filename,
            "desc": h.description,
            "fmt": h.format,
            "kb": h.byte_size // 1024,
            "score": h.score,
            "when": h.exif_datetime.date().isoformat() if h.exif_datetime else None,
            "geo": h.geo_country,
        }
        for h in hits
    ]
    return templates.TemplateResponse(
        request, "gallery.html", {"q": q, "fmt": fmt, "min_kb": min_kb, "items": items}
    )


@app.get("/image/{file_id}")
def image(file_id: int):
    with session_scope() as s:
        f = s.get(File, file_id)
        if f is None or not Path(f.path).is_file():
            return HTMLResponse("image not found", status_code=404)
        path, mime = f.path, f.mime
    return FileResponse(path, media_type=mime)


@app.get("/figure/{figure_id}")
def figure(figure_id: int):
    """Serve an extracted PDF figure raster from ``figures_dir``."""
    with session_scope() as s:
        fig = s.get(Figure, figure_id)
        if fig is None or not fig.image_path or not Path(fig.image_path).is_file():
            return HTMLResponse("figure not found", status_code=404)
        path = fig.image_path
    return FileResponse(path)

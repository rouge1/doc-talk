"""FastAPI app for the Phase 1 web UI.

Routes are thin: they read the truth store (MySQL/SQLite) and delegate to the same ``query``
functions the CLI uses (retriever, chat, hybrid). Models load lazily on first query, so the app
starts instantly; the chat route blocks for the local LLM (~a minute) and runs in FastAPI's
threadpool. Templates live alongside this module.
"""

from __future__ import annotations

import re
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
        related_sections, related_images = _related(s, chapter_id)
    return templates.TemplateResponse(
        request,
        "chapter.html",
        {
            "hash": content_hash, "title": title, "page": page, "chunks": chunks, "assets": assets,
            "related_sections": related_sections, "related_images": related_images,
        },
    )


def _related(s, chapter_id: int):
    """Resolve this chapter's semantic relations into related document sections (best score per
    target chapter) and related images, both sorted by score."""
    sections: dict[int, dict] = {}
    images: dict[int, dict] = {}
    for r in repo.get_relations_for_chapter(s, chapter_id):
        if r.src_image_id is not None and r.dst_chapter_id == chapter_id:
            f = s.get(File, r.src_image_id)
            if f is not None and (r.src_image_id not in images or r.score > images[r.src_image_id]["score"]):
                images[r.src_image_id] = {"file_id": r.src_image_id, "name": f.filename, "score": r.score}
            continue
        other = r.src_chapter_id if r.dst_chapter_id == chapter_id else r.dst_chapter_id
        if other is None or other == chapter_id:
            continue
        oc = s.get(Chapter, other)
        of = s.get(File, oc.file_id) if oc else None
        if oc is None or of is None:
            continue
        if other not in sections or r.score > sections[other]["score"]:
            sections[other] = {
                "hash": of.content_hash, "chapter_id": other, "title": oc.title,
                "file": of.filename, "score": r.score,
            }
    by_score = lambda d: sorted(d.values(), key=lambda x: x["score"], reverse=True)  # noqa: E731
    return by_score(sections), by_score(images)


@app.get("/search", response_class=HTMLResponse)
def search(request: Request, q: str = ""):
    hits = []
    if q.strip():
        from doctalk.query.retriever import retrieve

        hits = [
            {
                "file": h.file, "chapter": h.chapter, "page": h.page, "text": h.text,
                "score": h.rerank_score if h.rerank_score is not None else h.score,
                "reranked": h.rerank_score is not None,
            }
            for h in retrieve(q, k=10)
        ]
    return templates.TemplateResponse(request, "search.html", {"q": q, "hits": hits})


@app.get("/chat", response_class=HTMLResponse)
def chat(request: Request, q: str = ""):
    result = None
    if q.strip():
        from doctalk.query.wikichat import answer  # wiki-first, chunk-RAG fallback

        result = answer(q, k_chunks=6)
    return templates.TemplateResponse(request, "chat.html", {"q": q, "result": result})


# --- synthesis wiki browser ------------------------------------------------
_SLUG = re.compile(r"^[a-z0-9-]+$")


@app.get("/wiki", response_class=HTMLResponse)
def wiki_index(request: Request):
    """The synthesis wiki catalog: entities grouped by type, plus queries and the review queue."""
    groups: dict[str, list[dict]] = {}
    queries: list[dict] = []
    n_claims = reviews = 0
    with session_scope() as s:
        for e in repo.get_entities(s):
            if e.status != "active":
                continue
            claims = len(repo.get_claims_for_entity(s, e.id))
            n_claims += claims
            groups.setdefault(e.type, []).append({
                "name": e.name,
                "stem": Path(e.wiki_path).stem if e.wiki_path else None,
                "sources": e.source_count,
                "claims": claims,
            })
        queries = [
            {"title": p.title, "stem": Path(p.path).stem}
            for p in repo.get_wiki_pages_by_kind(s, "query")
        ]
        reviews = len(repo.get_open_reviews(s))
    n_entities = sum(len(v) for v in groups.values())
    ordered = sorted(groups.items())  # types alphabetical; entities sorted within
    for _, items in ordered:
        items.sort(key=lambda it: it["name"].lower())
    return templates.TemplateResponse(
        request, "wiki_index.html",
        {"groups": ordered, "queries": queries, "reviews": reviews,
         "totals": {"entities": n_entities, "claims": n_claims, "queries": len(queries)}},
    )


@app.get("/wiki/review", response_class=HTMLResponse)
def wiki_review(request: Request):
    """The open entity-resolution review queue (ambiguous DEFERs awaiting a human)."""
    with session_scope() as s:
        rows = [
            {"surface": r.mention_surface, "type": r.mention_type,
             "llm": r.llm_verdict, "entity_id": r.entity_id}
            for r in repo.get_open_reviews(s)
        ]
    return templates.TemplateResponse(request, "wiki_review.html", {"rows": rows})


@app.get("/wiki/page/{stem}", response_class=HTMLResponse)
def wiki_page(request: Request, stem: str):
    """Render an on-disk wiki page (the authored markdown is the source of truth)."""
    if not _SLUG.match(stem):  # slug-only: no path traversal
        return HTMLResponse("not found", status_code=404)
    from doctalk.config import get_settings
    from doctalk.api.wikimd import render

    kinds = {"entities": "entity", "queries": "query", "concepts": "concept", "topics": "topic"}
    wiki_dir = get_settings().wiki_dir
    for sub, label in kinds.items():
        path = wiki_dir / sub / f"{stem}.md"
        if path.is_file():
            body = render(path.read_text(encoding="utf-8"))
            return templates.TemplateResponse(
                request, "wiki_page.html", {"body": body, "kind": label}
            )
    return HTMLResponse("wiki page not found", status_code=404)


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
    # Collapse near-duplicates: one card per cluster, keeping the first hit (best-scoring for a
    # search, the representative for a listing) and counting the rest as "+N similar".
    items: list[dict] = []
    by_cluster: dict[object, dict] = {}
    for h in hits:
        key = h.cluster_id if h.cluster_id is not None else f"f{h.file_id}"
        if key in by_cluster:
            by_cluster[key]["dups"] += 1
            continue
        item = {
            "file_id": h.file_id,
            "name": h.filename,
            "desc": h.description,
            "fmt": h.format,
            "kb": h.byte_size // 1024,
            "score": h.score,
            "when": h.exif_datetime.date().isoformat() if h.exif_datetime else None,
            "geo": h.geo_country,
            "dups": 0,
        }
        by_cluster[key] = item
        items.append(item)
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
